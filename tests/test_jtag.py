"""JTAG bit-bang tools against the fake libdwf with a simulated TAP."""

import pytest

from ad2 import jtag
from conftest import FakeTap, call_tool

PINS = dict(tck_pin=4, tms_pin=5, tdi_pin=6, tdo_pin=7)


@pytest.fixture
def tap(fake_dwf):
    fake_dwf.tap = FakeTap(4, 5, 6, 7)
    return fake_dwf.tap


def test_tms_paths():
    assert jtag.tms_path("reset", "idle") == [0]
    assert jtag.tms_path("idle", "shift_dr") == [1, 0, 0]
    assert jtag.tms_path("idle", "shift_ir") == [1, 1, 0, 0]
    assert jtag.tms_path("exit1_dr", "idle") == [1, 0]
    assert jtag.tms_path("shift_dr", "shift_ir") == [1, 1, 1, 1, 0, 0]
    assert jtag.tms_path("idle", "idle") == []
    with pytest.raises(Exception, match="state must be"):
        jtag.tms_path("idle", "limbo")


def test_idcode_of_simulated_arm_tap(server, tap):
    out = call_tool(server, "jtag_idcode", **PINS)
    assert out["count"] == 1
    dev = out["devices"][0]
    assert dev == {"index": 0, "has_idcode": True, "idcode": "0x4ba00477", "manufacturer_id": "0x23b",
                   "manufacturer": "ARM", "part_number": "0xba00", "version": 4}
    assert out["pins"] == {"tck": 4, "tms": 5, "tdi": 6, "tdo": 7}
    assert tap.state == "idle"


def test_idcode_without_target_reports_wiring_hint(server, fake_dwf):
    fake_dwf.dio_external = 1 << 7                        # TDO pulled high, nothing driving it
    out = call_tool(server, "jtag_idcode", **PINS)
    assert out["count"] == 0 and "no device answered" in out["note"]


def test_count_devices_via_bypass(server, tap):
    out = call_tool(server, "jtag_count_devices", **PINS)
    assert out["count"] == 1 and out["method"] == "bypass"


def test_ir_and_dr_scans(server, tap):
    out = call_tool(server, "jtag_scan", register="ir", length=4, value="0xE", **PINS)
    assert out["shifted_out"] == "0x1"                     # IR capture value 0b0001
    assert out["tap_state"] == "idle" and tap.ir == 0b1110
    out = call_tool(server, "jtag_scan", register="dr", length=32, value=0, **PINS)
    assert out["shifted_out"] == "0x4ba00477"
    out = call_tool(server, "jtag_scan", register="ir", length=4, value=0xF, end_state="pause_ir", **PINS)
    assert out["tap_state"] == "pause_ir"
    out = call_tool(server, "jtag_scan", register="dr", length=8, value="0b10100101", **PINS)
    assert out["shifted_out_bits"][1:] == [1, 0, 1, 0, 0, 1, 0]   # BYPASS: one-bit delay
    with pytest.raises(RuntimeError, match="register must be"):
        call_tool(server, "jtag_scan", register="boundary", length=1, **PINS)
    with pytest.raises(RuntimeError, match="does not fit"):
        call_tool(server, "jtag_scan", register="dr", length=4, value=16, **PINS)
    with pytest.raises(RuntimeError, match="end_state"):
        call_tool(server, "jtag_scan", register="dr", length=4, end_state="shift_dr", **PINS)


def test_tap_state_walk_and_release(server, tap, fake_dwf):
    out = call_tool(server, "jtag_tap_state", state="shift_dr", **PINS)
    assert out["from"] == "reset" and out["tms_sequence"] == [0, 1, 0, 0]     # port setup leaves the TAP in reset
    assert out["state"] == "shift_dr" and tap.state == "shift_dr"
    out = call_tool(server, "jtag_tap_state", state="idle", **PINS)
    assert out["tms_sequence"] == [1, 1, 0]
    out = call_tool(server, "jtag_tap_state", state="reset", **PINS)
    assert out["tms_sequence"] == [1, 1, 1, 1, 1] and tap.state == "reset"
    assert call_tool(server, "jtag_release") == {"released": True}
    assert fake_dwf.dio_output_enable & 0b11110000 == 0
    assert call_tool(server, "jtag_release") == {"released": False}


def test_pin_validation(server):
    with pytest.raises(RuntimeError, match="all be different"):
        call_tool(server, "jtag_idcode", tck_pin=1, tms_pin=1)
    with pytest.raises(RuntimeError, match="max_devices"):
        call_tool(server, "jtag_idcode", max_devices=0)
