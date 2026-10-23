"""UART, SPI and I2C tools against the fake libdwf."""

import pytest

from ad2 import bytesutil
from conftest import call_tool


# --------------------------------------------------------------------------- byte helpers

def test_parse_bytes_forms():
    assert bytesutil.parse_bytes("01 ab FF") == b"\x01\xab\xff"
    assert bytesutil.parse_bytes("01abff") == b"\x01\xab\xff"
    assert bytesutil.parse_bytes("0x01, 0xAB") == b"\x01\xab"
    assert bytesutil.parse_bytes([1, 171, 255]) == b"\x01\xab\xff"
    assert bytesutil.parse_bytes(None) == b""
    for bad in ("zz", "123", [256], [1.5]):
        with pytest.raises(Exception):
            bytesutil.parse_bytes(bad)
    assert bytesutil.format_bytes(b"\x01\xab") == {"hex": "01 ab", "bytes": [1, 171], "length": 2}


# --------------------------------------------------------------------------- UART

def test_uart_send_loops_back(server, fake_dwf):
    out = call_tool(server, "uart_send", data="Hello\r\n", tx_pin=0, rx_pin=8, baud_rate=115200)
    assert out["sent"]["text"] == "Hello\r\n" and out["sent"]["length"] == 7
    assert out["received"]["text"] == "Hello\r\n"
    assert out["received"]["hex"] == "48 65 6c 6c 6f 0d 0a"
    assert out["stop_reason"] == "idle" and out["parity_errors"] == 0
    assert fake_dwf.uart_tx_log == [b"Hello\r\n"]
    (rate,) = fake_dwf.calls_to("FDwfDigitalUartRateSet")
    assert rate[1].value == 115200
    assert fake_dwf.calls_to("FDwfDigitalUartTxSet")[0][1].value == 0
    assert fake_dwf.calls_to("FDwfDigitalUartRxSet")[0][1].value == 8


def test_uart_send_hex_and_settings_reused(server, fake_dwf):
    call_tool(server, "uart_send", data="01 02 ff", data_format="hex", receive_timeout_s=0)
    call_tool(server, "uart_send", data="aa", data_format="hex", receive_timeout_s=0)
    assert fake_dwf.uart_tx_log == [b"\x01\x02\xff", b"\xaa"]
    assert len(fake_dwf.calls_to("FDwfDigitalUartRateSet")) == 1
    call_tool(server, "uart_send", data="x", baud_rate=19200, receive_timeout_s=0)
    assert len(fake_dwf.calls_to("FDwfDigitalUartRateSet")) == 2


def test_uart_receive_returns_buffered_bytes(server, fake_dwf):
    call_tool(server, "uart_send", data="ping", receive_timeout_s=0)
    out = call_tool(server, "uart_receive", timeout_s=1.0, idle_timeout_s=0.05)
    assert out["received"]["text"] == "ping" and out["stop_reason"] == "idle"
    out = call_tool(server, "uart_receive", timeout_s=0.05, idle_timeout_s=0.02)
    assert out["received"]["length"] == 0 and out["stop_reason"] == "timeout"


def test_uart_receive_only_never_initializes_tx(server, fake_dwf):
    call_tool(server, "uart_receive", tx_pin=5, rx_pin=8, timeout_s=0.02, idle_timeout_s=0)
    assert not fake_dwf.calls_to("FDwfDigitalUartTx")       # Tx(None) init is skipped
    assert fake_dwf.calls_to("FDwfDigitalUartRx")[0][1] is None  # Rx init happened


def test_uart_rejects_bad_settings(server, fake_dwf):
    with pytest.raises(RuntimeError, match="parity must be"):
        call_tool(server, "uart_send", data="x", parity="strong")
    with pytest.raises(RuntimeError, match="different pins"):
        call_tool(server, "uart_send", data="x", tx_pin=3, rx_pin=3)
    with pytest.raises(RuntimeError, match="stop_bits"):
        call_tool(server, "uart_send", data="x", stop_bits=3)
    with pytest.raises(RuntimeError, match="data_format"):
        call_tool(server, "uart_send", data="x", data_format="base64")
    with pytest.raises(RuntimeError, match="hex bytes"):
        call_tool(server, "uart_send", data="hello", data_format="hex")
    assert not fake_dwf.calls_to("FDwfDigitalUartRateSet")


# --------------------------------------------------------------------------- SPI

def test_spi_transfer_loops_back(server, fake_dwf):
    out = call_tool(server, "spi_transfer", write="a5 01 ff", clock_pin=1, mosi_pin=0, miso_pin=8,
                    select_pin=2, frequency=2e6, mode=3)
    assert out["read"]["hex"] == "a5 01 ff" and out["words"] == 3
    assert out["mode"] == 3 and out["frequency_hz"] == 2e6
    (freq,) = fake_dwf.calls_to("FDwfDigitalSpiFrequencySet")
    assert freq[1].value == 2e6
    data_pins = {a[1].value: a[2].value for a in fake_dwf.calls_to("FDwfDigitalSpiDataSet")}
    assert data_pins == {0: 0, 1: 8}
    (sel,) = fake_dwf.calls_to("FDwfDigitalSpiSelectSet")
    assert (sel[1].value, sel[2].value) == (2, 1)
    (wr,) = fake_dwf.calls_to("FDwfDigitalSpiWriteRead")
    assert wr[2].value == 8 and wr[4].value == 3


def test_spi_read_pads_with_fill_byte(server, fake_dwf):
    out = call_tool(server, "spi_transfer", write=[0x9F], read_count=4, fill_byte=0xFF)
    assert out["written"]["hex"] == "9f ff ff ff"
    assert out["read"]["hex"] == "9f ff ff ff"
    call_tool(server, "spi_transfer", write=[1])
    assert len(fake_dwf.calls_to("FDwfDigitalSpiFrequencySet")) == 1   # settings reused


def test_spi_wide_words_are_packed_into_bytes(server, fake_dwf):
    out = call_tool(server, "spi_transfer", write=[0x1234, 0xBEEF], read_count=3, bits_per_word=16, fill_byte=0xFF)
    assert out["written"]["hex"] == "12 34 be ef ff ff"
    assert out["written_words"] == [0x1234, 0xBEEF, 0xFFFF]
    assert out["read_words"] == [0x1234, 0xBEEF, 0xFFFF] and out["words"] == 3
    (wr,) = fake_dwf.calls_to("FDwfDigitalSpiWriteRead")
    assert wr[2].value == 8 and wr[4].value == 6                 # engine sees 6 eight-bit words
    out = call_tool(server, "spi_transfer", write=[0x01020304], bits_per_word=32, msb_first=False)
    assert out["written"]["hex"] == "04 03 02 01" and out["read_words"] == [0x01020304]
    out = call_tool(server, "spi_transfer", write=[0xABCDEF], bits_per_word=24)
    assert out["written"]["hex"] == "ab cd ef"
    with pytest.raises(RuntimeError, match="between 0 and 65535"):
        call_tool(server, "spi_transfer", write=[0x10000], bits_per_word=16)
    with pytest.raises(RuntimeError, match="list of integers"):
        call_tool(server, "spi_transfer", write="12 34", bits_per_word=16)


def test_spi_rejects_bad_arguments(server, fake_dwf):
    with pytest.raises(RuntimeError, match="all be different"):
        call_tool(server, "spi_transfer", write=[1], clock_pin=1, mosi_pin=1)
    with pytest.raises(RuntimeError, match="does not fit in 4-bit"):
        call_tool(server, "spi_transfer", write=[0x1F], bits_per_word=4)
    with pytest.raises(RuntimeError, match="16, 24 or 32"):
        call_tool(server, "spi_transfer", write=[1], bits_per_word=12)
    with pytest.raises(RuntimeError, match="nothing to transfer"):
        call_tool(server, "spi_transfer")
    with pytest.raises(RuntimeError, match="mode must be"):
        call_tool(server, "spi_transfer", write=[1], mode=4)
    assert not fake_dwf.calls_to("FDwfDigitalSpiWriteRead")


# --------------------------------------------------------------------------- I2C

def test_i2c_scan_finds_fake_slave(server):
    out = call_tool(server, "i2c_scan", scl_pin=3, sda_pin=4)
    assert out == {"scanned": "0x08-0x77", "found": ["0x1d"], "count": 1, "method": "engine"}


def test_i2c_write_read_and_write_read(server, fake_dwf):
    out = call_tool(server, "i2c_write", address=0x1D, data="10 20", scl_pin=3, sda_pin=4)
    assert out["acknowledged"] is True and out["address"] == "0x1d"
    assert fake_dwf.i2c_writes == [(0x1D, b"\x10\x20")]
    out = call_tool(server, "i2c_read", address=0x1D, count=4, scl_pin=3, sda_pin=4)
    assert out["read"]["hex"] == "00 01 02 03"
    out = call_tool(server, "i2c_write_read", address=0x1D, data=[5], count=3, scl_pin=3, sda_pin=4)
    assert out["read"]["bytes"] == [5, 6, 7] and out["written"]["bytes"] == [5]
    assert len(fake_dwf.calls_to("FDwfDigitalI2cRateSet")) == 1


def test_i2c_nak_is_reported_not_raised(server):
    out = call_tool(server, "i2c_write", address=0x20, data=[1])
    assert out["acknowledged"] is False and out["nak_index"] == 1
    assert "no device answered" in out["note"]
    out = call_tool(server, "i2c_read", address=0x20, count=2)
    assert out["acknowledged"] is False and out["read"]["length"] == 0


def test_i2c_bus_not_free_gives_wiring_hint(server, fake_dwf):
    fake_dwf.i2c_bus_free = 0
    with pytest.raises(RuntimeError, match="pull-up"):
        call_tool(server, "i2c_scan", method="engine")
    with pytest.raises(RuntimeError, match="DIO4/DIO3"):
        call_tool(server, "i2c_write", address=0x1D, data=[1], scl_pin=3, sda_pin=4, method="engine")
    assert not fake_dwf.calls_to("FDwfDigitalI2cWrite")
    # auto mode bit-bangs instead; with the lines really low it reports the bus error with the same hint
    out = call_tool(server, "i2c_write", address=0x1D, data=[1], scl_pin=3, sda_pin=4)
    assert out["bus_error"] is True and "pull-up" in out["note"] and out["method"] == "bitbang"


def test_i2c_retries_with_clock_stretch_detection_off(server, fake_dwf):
    fake_dwf.i2c_stretch_faults = True
    out = call_tool(server, "i2c_scan", scl_pin=3, sda_pin=4)
    assert out["found"] == ["0x1d"] and "bus_error" not in out
    assert "disabled" in out["clock_stretching"]
    assert fake_dwf.i2c_stretch_detect == 0
    out = call_tool(server, "i2c_write_read", address=0x1D, data=[2], count=2, scl_pin=3, sda_pin=4)
    assert out["acknowledged"] is True and out["read"]["bytes"] == [2, 3]
    # a fresh session configures stretch detection on again and the read path also falls back
    call_tool(server, "disconnect_device")
    fake_dwf.i2c_stretch_detect = 1
    out = call_tool(server, "i2c_read", address=0x1D, count=1, scl_pin=3, sda_pin=4)
    assert out["acknowledged"] is True and "clock_stretching" in out


def test_i2c_scan_flags_stuck_sda(server, fake_dwf):
    fake_dwf.i2c_devices = {a: bytearray(1) for a in range(0x80)}     # everything "acknowledges"
    out = call_tool(server, "i2c_scan", scl_pin=3, sda_pin=4, method="engine")
    assert out["found"] == [] and "SDA is stuck low" in out["note"]


def test_i2c_pin_change_resets_engine(server, fake_dwf):
    call_tool(server, "i2c_scan", scl_pin=3, sda_pin=4, first=0x1D, last=0x1D)
    resets = len(fake_dwf.calls_to("FDwfDigitalI2cReset"))
    call_tool(server, "i2c_scan", scl_pin=3, sda_pin=4, rate=400e3, first=0x1D, last=0x1D)   # rate only: no reset
    assert len(fake_dwf.calls_to("FDwfDigitalI2cReset")) == resets
    call_tool(server, "i2c_scan", scl_pin=4, sda_pin=3, first=0x1D, last=0x1D)              # pins swapped: reset
    assert len(fake_dwf.calls_to("FDwfDigitalI2cReset")) == resets + 1


def test_i2c_bitbang_talks_to_simulated_slave(server, fake_dwf):
    from conftest import FakeI2cSlave
    fake_dwf.i2c_slave = FakeI2cSlave(scl=15, sda=7, address=0x68, registers=bytearray([0x19 if i == 0x75 else i for i in range(0x80)]))
    out = call_tool(server, "i2c_scan", scl_pin=15, sda_pin=7, method="bitbang")
    assert out["found"] == ["0x68"] and out["method"] == "bitbang"
    out = call_tool(server, "i2c_write_read", address=0x68, data="75", count=1, scl_pin=15, sda_pin=7, method="bitbang")
    assert out["acknowledged"] and out["read"]["hex"] == "19"
    out = call_tool(server, "i2c_write_read", address=0x68, data="3b", count=3, scl_pin=15, sda_pin=7, method="bitbang")
    assert out["read"]["bytes"] == [0x3B, 0x3C, 0x3D]
    out = call_tool(server, "i2c_write", address=0x69, data=[1], scl_pin=15, sda_pin=7, method="bitbang")
    assert out["acknowledged"] is False and out["nak_index"] == 1
    assert not (fake_dwf.dio_output_enable & ((1 << 15) | (1 << 7)))      # lines released afterwards


def test_i2c_auto_falls_back_to_bitbang_on_engine_bus_error(server, fake_dwf):
    from conftest import FakeI2cSlave
    fake_dwf.i2c_slave = FakeI2cSlave(scl=15, sda=7, address=0x68)
    fake_dwf.i2c_bus_free = 0                                            # engine says the bus is not free
    out = call_tool(server, "i2c_scan", scl_pin=15, sda_pin=7)
    assert out["found"] == ["0x68"] and out["method"] == "bitbang" and "bit-banged instead" in out["note_method"]
    fake_dwf.i2c_bus_free = 1
    fake_dwf.i2c_devices = {a: bytearray(1) for a in range(0x80)}        # engine phantom-ACKs everything
    out = call_tool(server, "i2c_scan", scl_pin=15, sda_pin=7)
    assert out["found"] == ["0x68"] and out["method"] == "bitbang"
    out = call_tool(server, "i2c_scan", scl_pin=15, sda_pin=7, method="engine")
    assert out["found"] == [] and "SDA is stuck low" in out["note"]


def test_i2c_rejects_bad_arguments(server):
    with pytest.raises(RuntimeError, match="7-bit"):
        call_tool(server, "i2c_write", address=0x80, data=[1])
    with pytest.raises(RuntimeError, match="different pins"):
        call_tool(server, "i2c_scan", scl_pin=2, sda_pin=2)
    with pytest.raises(RuntimeError, match="at least one byte"):
        call_tool(server, "i2c_write_read", address=0x1D, data=[], count=1)


def test_i2c_spy_reassembles_messages(server, fake_dwf):
    fake_dwf.i2c_spy_fragments = [
        (1, 0, bytes([0x1D << 1, 0x05]), 0),          # start, write to 0x1d, register 5
        (2, 0, bytes([(0x1D << 1) | 1, 0xAA]), 0),    # restart, read
        (0, 1, bytes([0xBB]), 0),                     # more data, stop
        (1, 1, bytes([0x20 << 1]), 1),                # start, 0x20 NAKs the address, stop
    ]
    out = call_tool(server, "i2c_spy", duration_s=0.05)
    assert out["count"] == 3
    first, second, third = out["messages"]
    assert first == {"restart": False, "address": "0x1d", "direction": "write", "data": [5], "nak": 0,
                     "stop": False, "data_hex": "05"}
    assert second["restart"] is True and second["direction"] == "read"
    assert second["data"] == [0xAA, 0xBB] and second["stop"] is True
    assert third["address"] == "0x20" and third["nak"] == 1
    assert fake_dwf.calls_to("FDwfDigitalI2cSpyStart") and fake_dwf.calls_to("FDwfDigitalI2cReset")


def test_disconnect_resets_protocol_engines(server, fake_dwf):
    call_tool(server, "uart_send", data="x", receive_timeout_s=0)
    call_tool(server, "disconnect_device")
    for name in ("FDwfDigitalUartReset", "FDwfDigitalSpiReset", "FDwfDigitalI2cReset"):
        assert fake_dwf.calls_to(name), name
