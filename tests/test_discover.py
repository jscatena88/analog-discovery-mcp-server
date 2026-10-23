"""SWD identification and board-discovery tools against the fake libdwf."""

import pytest

from ad2 import discover
from conftest import FakeTap, call_tool


def uart_bits(text, baud, fs, idle_before=200, idle_after=200):
    """Sampled 8N1 line for `text` at `baud`, idle high around it."""
    bit = fs / baud
    levels = [1] * idle_before
    for byte in text.encode():
        frame = [0] + [(byte >> k) & 1 for k in range(8)] + [1]
        for i, level in enumerate(frame):
            levels += [level] * (int(round((i + 1) * bit)) - int(round(i * bit)))
    return levels + [1] * idle_after


@pytest.fixture
def board(fake_dwf):
    """A fake board: 1 MHz clock on DIO0, DIO1 high, UART 'Hello\\r\\n' on DIO3 at 115200, SW-DP on (5, 6),
    JTAG TAP on (8, 9, 10, 11)."""
    fs = 1e6
    serial = uart_bits("Hello\r\n" * 3, 115200, fs)
    def pattern(i):
        value = ((i // 50) & 1) | 0b10
        if i < len(serial):
            value |= serial[i] << 3
        else:
            value |= 1 << 3
        return value
    fake_dwf.logic_pattern = pattern
    fake_dwf.swd_target = (5, 6)
    fake_dwf.tap = FakeTap(8, 9, 10, 11)
    fake_dwf.dio_external = 1 << 11     # TDO idle high where nothing drives it
    return fake_dwf


# --------------------------------------------------------------------------- SWD

def test_swd_idcode_decodes_dpidr(server, board):
    out = call_tool(server, "swd_idcode", swclk_pin=5, swdio_pin=6)
    assert out["responded"] is True and out["dpidr"] == "0x2ba01477"
    assert out["designer"] == "ARM" and out["version"] == 1 and "Cortex-M3/M4" in out["part_hint"]
    assert board.calls_to("FDwfDigitalSwdClear")
    out = call_tool(server, "swd_idcode", swclk_pin=6, swdio_pin=5)
    assert out["responded"] is False and "no SWD response" in out["note"]


def test_swd_identify_reads_ap_and_restores(server, board):
    out = call_tool(server, "swd_identify", swclk_pin=5, swdio_pin=6)
    assert out["debug_powered_up"] is True
    ap = out["ap0"]
    assert ap["idr"] == "0x24770011" and ap["class"] == "MEM-AP" and ap["type"].startswith("AHB-AP")
    assert ap["rom_table_base"] == "0xe00ff000" and ap["rom_table_present"] is True
    assert "Cortex-M" in ap["hint"]
    assert board.swd_writes[-1] == ("dp", 4, 0)            # CTRL/STAT restored
    assert ("dp", 0, 0x1E) in board.swd_writes            # ABORT


def test_swd_raw_register_access(server, board):
    call_tool(server, "swd_idcode", swclk_pin=5, swdio_pin=6)
    out = call_tool(server, "swd_read_register", port="dp", address=0, swclk_pin=5, swdio_pin=6)
    assert out["value"] == "0x2ba01477" and out["ack"] == "OK"
    out = call_tool(server, "swd_write_register", port="dp", address=8, value=0xF0, swclk_pin=5, swdio_pin=6)
    assert out["ack"] == "OK" and board.swd_dp["select"] == 0xF0
    with pytest.raises(RuntimeError, match="port must be"):
        call_tool(server, "swd_read_register", port="xp", address=0)
    with pytest.raises(RuntimeError, match="address must be"):
        call_tool(server, "swd_read_register", port="dp", address=2, swclk_pin=5, swdio_pin=6)


# --------------------------------------------------------------------------- observation

def test_classify_pin_kinds():
    fs = 1e6
    clock = [(i // 50) & 1 for i in range(20000)]
    assert discover.classify_pin(clock, fs)["kind"] == "clock"
    assert discover.classify_pin([1] * 1000, fs)["kind"] == "static_high"
    assert discover.classify_pin([0] * 1000, fs)["kind"] == "static_low"
    serial = uart_bits("Hello world\r\n", 115200, fs)
    info = discover.classify_pin(serial, fs)
    assert info["kind"] == "uart_candidate" and info["baud_hint"] == 115200
    few = [0] * 500 + [1] * 500
    assert discover.classify_pin(few, fs)["kind"] == "few_edges"
    # a single one-sample glitch (as a streamed record can contain) must not change the verdict
    glitched = serial[:]
    glitched[300] ^= 1
    info = discover.classify_pin(glitched, fs)
    assert info["kind"] == "uart_candidate" and info["baud_hint"] == 115200
    # jittered run lengths (17/18 samples per bit at 2 MS/s) still quantize against the baud period
    jittered = uart_bits("bench uart\r\n" * 4, 115200, 2e6)
    assert discover.classify_pin(jittered, 2e6)["kind"] == "uart_candidate"


def test_observe_pins_reports_quiet_and_active(server, board):
    out = call_tool(server, "observe_pins", duration_s=0.02, sampling_frequency=1e6)
    assert out["pins"]["dio0"]["kind"] == "clock"
    assert out["pins"]["dio1"]["kind"] == "static_high"
    assert out["pins"]["dio3"]["kind"] == "uart_candidate" and out["pins"]["dio3"]["baud_hint"] == 115200
    assert 0 not in out["quiet_pins"] and 1 in out["quiet_pins"] and 5 in out["quiet_pins"]
    assert out["by_kind"]["clock"] == [0]
    assert "quiet" in out["advice"]


def test_detect_uart_decodes_text(server, board):
    out = call_tool(server, "detect_uart", duration_s=0.02, sampling_frequency=1e6)
    assert out["candidates"], out
    best = out["candidates"][0]
    assert best["pin"] == 3 and best["baud"] == 115200 and best["confidence"] == "high"
    assert best["sample_text"].startswith("Hello\r\n") and best["framing_errors"] == 0
    assert "uart_receive(rx_pin=3, baud_rate=115200)" in best["next_step"]
    out = call_tool(server, "detect_uart", pins=[0, 1], duration_s=0.02)
    assert out["candidates"] == [] and "no UART-like frames" in out["note"]


def test_decode_uart_handles_framing_errors():
    fs = 1e6
    bits = uart_bits("A", 9600, fs)
    data, errors = discover.decode_uart(bits, fs, 9600)
    assert data == b"A" and errors == 0
    broken = bits[:]
    # corrupt the stop bit of the frame
    bit = fs / 9600
    stop = int(round(200 + bit * 9.5))
    broken[stop] = 0
    data, errors = discover.decode_uart(broken, fs, 9600)
    assert data == b"" and errors == 1


# --------------------------------------------------------------------------- searches

def test_find_swd_locates_pair(server, board):
    out = call_tool(server, "find_swd", candidate_pins=[4, 5, 6, 7], exclude_pins=[4])
    assert out["pairs_tried"] == 6 and len(out["found"]) == 1
    hit = out["found"][0]
    assert (hit["swclk"], hit["swdio"]) == (5, 6) and hit["dpidr"] == "0x2ba01477"
    assert "swd_identify(swclk_pin=5, swdio_pin=6)" in out["next_step"]
    with pytest.raises(RuntimeError, match="at least two"):
        call_tool(server, "find_swd", candidate_pins=[5])
    out = call_tool(server, "find_swd", candidate_pins=[5, 6, 12])
    assert out["skipped_pins"] == [12] and out["pairs_tried"] == 2
    with pytest.raises(RuntimeError, match="DIO 0-9"):
        call_tool(server, "swd_idcode", swclk_pin=11, swdio_pin=5)


def test_find_jtag_locates_tap_by_idcode(server, board):
    out = call_tool(server, "find_jtag", candidate_pins=[7, 8, 9, 10, 11, 12])
    assert len(out["found"]) == 1
    hit = out["found"][0]
    assert hit["method"] == "idcode"
    assert (hit["tck"], hit["tms"], hit["tdo"], hit["tdi"]) == (8, 9, 11, 10)
    assert hit["idcode"] == "0x4ba00477" and hit["manufacturer"] == "ARM"
    assert "tck_pin=8, tms_pin=9, tdi_pin=10, tdo_pin=11" in out["next_step"]


def test_find_jtag_falls_back_to_bypass(server, board):
    board.tap.idcode = 0                         # reset-default register reads as zeros: no valid IDCODE
    out = call_tool(server, "find_jtag", candidate_pins=[8, 9, 10, 11])
    assert len(out["found"]) == 1
    hit = out["found"][0]
    assert hit["method"] == "bypass"
    assert (hit["tck"], hit["tms"], hit["tdi"], hit["tdo"]) == (8, 9, 10, 11)
    assert hit["dr_delay_bits"] == 32 and "32-bit" in hit["note"]


def test_find_jtag_reports_nothing_on_quiet_pins(server, board):
    out = call_tool(server, "find_jtag", candidate_pins=[12, 13, 14], max_time_s=30)
    assert out["found"] == [] and "no TAP found" in out["next_step"]
    assert out["combinations_tried"] == 6 + 0          # idcode pass only has 3-pin permutations; bypass pass needs 4 pins


def test_find_i2c_locates_bus(server, board):
    board.i2c_target_pins = (7, 15)
    out = call_tool(server, "find_i2c", candidate_pins=[7, 15])
    assert out["pairs_tried"] == 2
    assert out["found"] == [{"scl": 7, "sda": 15, "addresses": ["0x1d"], "method": "engine"}]
    assert "scl_pin=7, sda_pin=15" in out["next_step"]
    board.i2c_bus_free = 0                     # engine says not free; the bit-bang fallback finds the lines low too
    out = call_tool(server, "find_i2c", candidate_pins=[7, 15])
    assert out["found"] == []
