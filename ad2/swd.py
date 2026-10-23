"""SWD (Serial Wire Debug) identification using the WaveForms SWD engine.

The engine does packet-level DP/AP transfers; the JTAG-to-SWD switch and the dormant-state
wake-up are bit-banged beforehand because the engine has no raw bit mode. Everything the
identify tool does is read-only apart from debug-domain power-up, which it undoes afterwards.
"""

import ctypes
import time

from WF_SDK import device

from .digital import check_pins
from .errors import AD2Error

ACK = {1: "OK", 2: "WAIT", 4: "FAULT"}
DP_PARTS = {
    0xBA01: "SWJ-DP / SW-DP v1 (Cortex-M3/M4 class, e.g. STM32F1/F2/F4)",
    0xBA02: "SW-DP v2 (Cortex-M7/M33 class)",
    0xBB11: "SW-DP (Cortex-M0)",
    0xBC11: "SW-DP (Cortex-M0+)",
    0xBE12: "SW-DP (Cortex-M23/M33 class)",
}
AP_TYPES = {0: "JTAG-AP", 1: "AHB-AP (Cortex-M system bus)", 2: "APB-AP", 4: "AXI-AP", 5: "AHB5-AP", 6: "APB4-AP", 7: "AXI5-AP"}
AP_CLASSES = {0: "none", 1: "COM-AP", 8: "MEM-AP"}


def _dwf():
    return device.dwf


# --------------------------------------------------------------------------- raw bit sequences

class _BitBang:
    """Drive SWCLK/SWDIO through the static I/O for the mode-switch sequences."""

    def __init__(self, dd, clk, io):
        self.dd, self.clk, self.io = dd, clk, io
        dwf = _dwf()
        enable, value = ctypes.c_uint32(), ctypes.c_uint32()
        dwf.FDwfDigitalIOOutputEnableGet(dd.handle, ctypes.byref(enable))
        dwf.FDwfDigitalIOOutputGet(dd.handle, ctypes.byref(value))
        self.enable = enable.value | (1 << clk) | (1 << io)
        self.value = value.value & ~(1 << clk)
        if dwf.FDwfDigitalIOOutputEnableSet(dd.handle, ctypes.c_int(self.enable)) == 0:
            device.check_error()
        self._write(self.value)

    def _write(self, value):
        self.value = value
        if _dwf().FDwfDigitalIOOutputSet(self.dd.handle, ctypes.c_int(value)) == 0:
            device.check_error()

    def bits(self, bits):
        for bit in bits:
            base = (self.value & ~((1 << self.clk) | (1 << self.io))) | (bit << self.io)
            self._write(base)
            self._write(base | (1 << self.clk))
        self._write(self.value & ~(1 << self.clk))

    def release(self):
        self.enable &= ~((1 << self.clk) | (1 << self.io))
        if _dwf().FDwfDigitalIOOutputEnableSet(self.dd.handle, ctypes.c_int(self.enable)) == 0:
            device.check_error()


def _lsb_first(value: int, length: int):
    return [(value >> i) & 1 for i in range(length)]


def switch_to_swd(dd, clk, io, dormant_wakeup=False):
    """Line reset, JTAG-to-SWD select (0xE79E), line reset, idle. Optionally the ADIv5.2 dormant
    wake-up (selection alert + SWD activation code) first, for DPv2 parts that start dormant."""
    bb = _BitBang(dd, clk, io)
    try:
        if dormant_wakeup:
            bb.bits([1] * 8)
            alert = 0x19BC0EA2E3DDAFE986852D956209F392
            bb.bits(_lsb_first(alert, 128))
            bb.bits([0] * 4)
            bb.bits(_lsb_first(0x1A, 8))
        bb.bits([1] * 56)
        bb.bits(_lsb_first(0xE79E, 16))
        bb.bits([1] * 56)
        bb.bits([0] * 8)
    finally:
        bb.release()


# --------------------------------------------------------------------------- engine transfers

MAX_ENGINE_PIN = 9   # the AD2's SWD engine rejects DIO 10-15 for both SWCLK and SWDIO


def configure(session, swclk_pin, swdio_pin, rate):
    dd = session.ensure()
    (swclk_pin,) = check_pins(dd, [swclk_pin], "swclk_pin")
    (swdio_pin,) = check_pins(dd, [swdio_pin], "swdio_pin")
    if swclk_pin == swdio_pin:
        raise AD2Error("swclk_pin and swdio_pin must be different pins")
    if swclk_pin > MAX_ENGINE_PIN or swdio_pin > MAX_ENGINE_PIN:
        raise AD2Error(f"the SWD engine only works on DIO 0-{MAX_ENGINE_PIN}; move SWCLK/SWDIO to those pins")
    if not 1e3 <= rate <= 25e6:
        raise AD2Error("rate must be between 1e3 and 25e6 Hz")
    key = (swclk_pin, swdio_pin, float(rate))
    dwf = _dwf()
    if session.swd_settings != key:
        if dwf.FDwfDigitalSwdReset(dd.handle) == 0:
            device.check_error()
        if dwf.FDwfDigitalSwdRateSet(dd.handle, ctypes.c_double(rate)) == 0:
            device.check_error()
        if dwf.FDwfDigitalSwdCkSet(dd.handle, ctypes.c_int(swclk_pin)) == 0:
            device.check_error()
        if dwf.FDwfDigitalSwdIoSet(dd.handle, ctypes.c_int(swdio_pin)) == 0:
            device.check_error()
        session.swd_settings = key
    return dd


def line_reset(dd):
    """Engine-side line reset (>= 50 clocks high) followed by idle clocks."""
    if _dwf().FDwfDigitalSwdClear(dd.handle, ctypes.c_int(56), ctypes.c_int(8)) == 0:
        device.check_error()


def read(dd, ap: bool, address: int, retries=8):
    """One DP/AP read. Returns (ack, value, parity_ok). Retries on WAIT."""
    if address not in (0x0, 0x4, 0x8, 0xC):
        raise AD2Error("address must be 0x0, 0x4, 0x8 or 0xC")
    ack, value, crc = ctypes.c_int(), ctypes.c_uint(), ctypes.c_int()
    for _ in range(retries):
        if _dwf().FDwfDigitalSwdRead(dd.handle, ctypes.c_int(1 if ap else 0), ctypes.c_int(address >> 2),
                                     ctypes.byref(ack), ctypes.byref(value), ctypes.byref(crc)) == 0:
            device.check_error()
        if ack.value != 2:
            break
        time.sleep(0.001)
    return ack.value, value.value, crc.value == 0


def write(dd, ap: bool, address: int, value: int, retries=8):
    if address not in (0x0, 0x4, 0x8, 0xC):
        raise AD2Error("address must be 0x0, 0x4, 0x8 or 0xC")
    ack = ctypes.c_int()
    for _ in range(retries):
        if _dwf().FDwfDigitalSwdWrite(dd.handle, ctypes.c_int(1 if ap else 0), ctypes.c_int(address >> 2),
                                      ctypes.byref(ack), ctypes.c_uint(value & 0xFFFFFFFF)) == 0:
            device.check_error()
        if ack.value != 2:
            break
        time.sleep(0.001)
    return ack.value


def _ack_name(ack):
    return ACK.get(ack, f"no response ({ack})")


# --------------------------------------------------------------------------- identification

def decode_dpidr(code: int) -> dict:
    designer = (code >> 1) & 0x7FF
    part = (code >> 20) & 0xFF
    out = {
        "dpidr": f"0x{code:08x}",
        "designer_id": f"0x{designer:03x}",
        "designer": "ARM" if designer == 0x23B else "unknown",
        "version": (code >> 12) & 0xF,
        "min_dp": bool((code >> 16) & 1),
        "part_number": f"0x{part:02x}",
        "revision": (code >> 28) & 0xF,
    }
    full_part = (code >> 12) & 0xFFFF
    out["part_hint"] = DP_PARTS.get(full_part, "")
    if not out["part_hint"]:
        out.pop("part_hint")
    return out


def decode_ap_idr(idr: int) -> dict:
    cls = (idr >> 13) & 0xF
    ap_type = idr & 0xF
    designer = (idr >> 17) & 0x7FF
    return {
        "idr": f"0x{idr:08x}",
        "revision": (idr >> 28) & 0xF,
        "designer": "ARM" if designer == 0x23B else f"0x{designer:03x}",
        "class": AP_CLASSES.get(cls, str(cls)),
        "variant": (idr >> 4) & 0xF,
        "type": AP_TYPES.get(ap_type, str(ap_type)),
    }


def idcode(session, swclk_pin, swdio_pin, rate=1e6, dormant_wakeup=False) -> dict:
    """Switch the target to SWD and read the DP IDCODE (DPIDR). Read-only."""
    dd = configure(session, swclk_pin, swdio_pin, rate)
    switch_to_swd(dd, swclk_pin, swdio_pin, dormant_wakeup)
    line_reset(dd)
    ack, value, parity_ok = read(dd, False, 0x0)
    result = {"pins": {"swclk": swclk_pin, "swdio": swdio_pin}, "ack": _ack_name(ack), "responded": ack == 1}
    if ack == 1 and value not in (0, 0xFFFFFFFF) and value & 1:
        result.update(decode_dpidr(value))
        result["parity_ok"] = parity_ok
    elif ack == 1:
        result["note"] = f"ACK OK but implausible IDCODE 0x{value:08x}: check SWDIO wiring"
        result["responded"] = False
    else:
        result["note"] = ("no SWD response. Check SWCLK/SWDIO assignment, target power, that the debug port is "
                          "not disabled, and try dormant_wakeup=true for DPv2 targets that start dormant")
    return result


def identify(session, swclk_pin, swdio_pin, rate=1e6, dormant_wakeup=False, restore=True) -> dict:
    """DP IDCODE, then power up the debug domain, read AP0 IDR and ROM table base, power down again."""
    result = idcode(session, swclk_pin, swdio_pin, rate, dormant_wakeup)
    if not result["responded"]:
        return result
    dd = session.device_data
    steps = result.setdefault("steps", [])
    try:
        ack = write(dd, False, 0x0, 0x1E)                                   # ABORT: clear sticky flags
        steps.append(f"ABORT: {_ack_name(ack)}")
        ack = write(dd, False, 0x8, 0x00000000)                             # SELECT: DP bank 0
        ack = write(dd, False, 0x4, 0x50000000)                             # CTRL/STAT: power-up requests
        steps.append(f"CTRL/STAT power-up request: {_ack_name(ack)}")
        powered = False
        for _ in range(20):
            ack, status, _ = read(dd, False, 0x4)
            if ack == 1 and (status & 0xA0000000) == 0xA0000000:
                powered = True
                break
            time.sleep(0.001)
        result["debug_powered_up"] = powered
        result["ctrl_stat"] = f"0x{status:08x}"
        if powered:
            write(dd, False, 0x8, 0x000000F0)                               # SELECT: AP 0, bank 0xF
            read(dd, True, 0xC)                                             # AP IDR (posted)
            ack, idr, _ = read(dd, False, 0xC)                              # RDBUFF
            if ack == 1 and idr not in (0, 0xFFFFFFFF):
                result["ap0"] = decode_ap_idr(idr)
                read(dd, True, 0x8)                                         # AP BASE (posted)
                ack, base, _ = read(dd, False, 0xC)
                if ack == 1:
                    result["ap0"]["rom_table_base"] = f"0x{base & 0xFFFFF000:08x}"
                    result["ap0"]["rom_table_present"] = bool(base & 1)
                    if (base & 0xFFFFF000) == 0xE00FF000:
                        result["ap0"]["hint"] = "ROM table at 0xE00FF000: Cortex-M core"
            else:
                result["ap0"] = {"error": f"AP IDR read: {_ack_name(ack)}"}
    finally:
        if restore:
            write(dd, False, 0x8, 0x00000000)
            write(dd, False, 0x4, 0x00000000)                               # drop power-up requests
            steps.append("CTRL/STAT restored to 0")
    return result


def register_read(session, swclk_pin, swdio_pin, port, address, rate=1e6) -> dict:
    dd = configure(session, swclk_pin, swdio_pin, rate)
    ack, value, parity_ok = read(dd, port == "ap", address)
    return {"port": port, "address": f"0x{address:x}", "ack": _ack_name(ack), "value": f"0x{value:08x}",
            "parity_ok": parity_ok}


def register_write(session, swclk_pin, swdio_pin, port, address, value, rate=1e6) -> dict:
    dd = configure(session, swclk_pin, swdio_pin, rate)
    ack = write(dd, port == "ap", address, value)
    return {"port": port, "address": f"0x{address:x}", "ack": _ack_name(ack), "value": f"0x{value & 0xFFFFFFFF:08x}"}


def reset_engine(session) -> dict:
    if session.swd_settings is None:
        return {"released": False}
    if _dwf().FDwfDigitalSwdReset(session.device_data.handle) == 0:
        device.check_error()
    session.swd_settings = None
    return {"released": True}
