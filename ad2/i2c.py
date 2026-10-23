"""I2C master (write, read, write-then-read, address scan) and bus spy on two DIO pins."""

import ctypes
import time

from WF_SDK import device

from .bytesutil import format_bytes, parse_bytes
from .digital import check_pins
from .errors import AD2Error

PULLUP_HINT = ("The AD2 has no built-in pull-ups: SDA and SCL each need a pull-up resistor "
               "(typically 2.2 k to 10 k to 3.3 V) and the slave must be powered.")


def _dwf():
    return device.dwf


def _check_address(address) -> int:
    if isinstance(address, bool) or not isinstance(address, int) or not 0 <= address <= 0x7F:
        raise AD2Error("address must be a 7-bit I2C address between 0x00 and 0x7F (do not pre-shift it)")
    return address


def configure(session, scl_pin, sda_pin, rate, check_bus=True):
    """Set pins and rate (only when changed); optionally verify the bus is idle (both lines high)."""
    dd = session.ensure()
    dwf = _dwf()
    (scl_pin,) = check_pins(dd, [scl_pin], "scl_pin")
    (sda_pin,) = check_pins(dd, [sda_pin], "sda_pin")
    if scl_pin == sda_pin:
        raise AD2Error("scl_pin and sda_pin must be different pins")
    if rate <= 0:
        raise AD2Error("rate must be positive (typically 100e3 or 400e3)")
    key = (scl_pin, sda_pin, float(rate))
    if session.i2c_settings != key:
        if dwf.FDwfDigitalI2cRateSet(dd.handle, ctypes.c_double(rate)) == 0:
            device.check_error()
        if dwf.FDwfDigitalI2cSclSet(dd.handle, ctypes.c_int(scl_pin)) == 0:
            device.check_error()
        if dwf.FDwfDigitalI2cSdaSet(dd.handle, ctypes.c_int(sda_pin)) == 0:
            device.check_error()
        session.i2c_settings = key
    if check_bus:
        free = ctypes.c_int()
        if dwf.FDwfDigitalI2cClear(dd.handle, ctypes.byref(free)) == 0:
            device.check_error()
        if free.value == 0:
            raise AD2Error(f"I2C bus is not free: SDA or SCL (DIO{sda_pin}/DIO{scl_pin}) is held low. {PULLUP_HINT}")
    return dd


def _result(nak: int, extra=None) -> dict:
    """iNak: 0 = every byte acknowledged, n > 0 = NAK on the n-th transferred byte (1 = address), < 0 = bus error."""
    out = {"acknowledged": nak == 0, "nak_index": nak if nak > 0 else None, "bus_error": nak < 0}
    if nak == 1:
        out["note"] = "address not acknowledged: no device answered at this address"
    elif nak > 1:
        out["note"] = f"NAK after {nak - 1} data byte(s)"
    elif nak < 0:
        out["note"] = f"bus error ({nak}). {PULLUP_HINT}"
    if extra:
        out.update(extra)
    return out


def write(session, address, data, scl_pin, sda_pin, rate) -> dict:
    dd = configure(session, scl_pin, sda_pin, rate)
    address = _check_address(address)
    payload = parse_bytes(data)
    buffer = (ctypes.c_ubyte * max(1, len(payload)))(*payload)
    nak = ctypes.c_int()
    if _dwf().FDwfDigitalI2cWrite(dd.handle, ctypes.c_int(address << 1), buffer, ctypes.c_int(len(payload)),
                                  ctypes.byref(nak)) == 0:
        device.check_error()
    return _result(nak.value, {"address": f"0x{address:02x}", "written": format_bytes(payload)})


def read(session, address, count, scl_pin, sda_pin, rate) -> dict:
    dd = configure(session, scl_pin, sda_pin, rate)
    address = _check_address(address)
    if not 1 <= count <= 4096:
        raise AD2Error("count must be between 1 and 4096 bytes")
    buffer = (ctypes.c_ubyte * count)()
    nak = ctypes.c_int()
    if _dwf().FDwfDigitalI2cRead(dd.handle, ctypes.c_int(address << 1), buffer, ctypes.c_int(count),
                                 ctypes.byref(nak)) == 0:
        device.check_error()
    data = bytes(buffer) if nak.value == 0 else b""
    return _result(nak.value, {"address": f"0x{address:02x}", "read": format_bytes(data)})


def write_read(session, address, data, count, scl_pin, sda_pin, rate) -> dict:
    """Write (e.g. a register number) then repeated-start read, without releasing the bus."""
    dd = configure(session, scl_pin, sda_pin, rate)
    address = _check_address(address)
    payload = parse_bytes(data)
    if not payload:
        raise AD2Error("data must contain at least one byte to write (for example the register address)")
    if not 1 <= count <= 4096:
        raise AD2Error("count must be between 1 and 4096 bytes")
    tx = (ctypes.c_ubyte * len(payload))(*payload)
    rx = (ctypes.c_ubyte * count)()
    nak = ctypes.c_int()
    if _dwf().FDwfDigitalI2cWriteRead(dd.handle, ctypes.c_int(address << 1), tx, ctypes.c_int(len(payload)),
                                      rx, ctypes.c_int(count), ctypes.byref(nak)) == 0:
        device.check_error()
    data_read = bytes(rx) if nak.value == 0 else b""
    return _result(nak.value, {"address": f"0x{address:02x}", "written": format_bytes(payload),
                               "read": format_bytes(data_read)})


def scan(session, scl_pin, sda_pin, rate, first=0x08, last=0x77) -> dict:
    """Probe each address with a zero-length write; devices that ACK are listed."""
    dd = configure(session, scl_pin, sda_pin, rate)
    if not 0 <= first <= last <= 0x7F:
        raise AD2Error("first and last must satisfy 0 <= first <= last <= 0x7F")
    found = []
    bus_error = None
    for address in range(first, last + 1):
        nak = ctypes.c_int()
        if _dwf().FDwfDigitalI2cWrite(dd.handle, ctypes.c_int(address << 1), None, ctypes.c_int(0),
                                      ctypes.byref(nak)) == 0:
            device.check_error()
        if nak.value == 0:
            found.append(f"0x{address:02x}")
        elif nak.value < 0:
            bus_error = nak.value
            break
    out = {"scanned": f"0x{first:02x}-0x{last:02x}", "found": found, "count": len(found)}
    if bus_error is not None:
        out["bus_error"] = bus_error
        out["note"] = f"scan aborted by a bus error ({bus_error}). {PULLUP_HINT}"
    return out


def spy(session, scl_pin, sda_pin, rate, duration_s=2.0, max_messages=100) -> dict:
    """Passively decode traffic on the bus for `duration_s` seconds."""
    if not 0 < duration_s <= 60:
        raise AD2Error("duration_s must be between 0 and 60 seconds")
    if max_messages < 1:
        raise AD2Error("max_messages must be at least 1")
    dd = configure(session, scl_pin, sda_pin, rate, check_bus=False)
    dwf = _dwf()
    if dwf.FDwfDigitalI2cSpyStart(dd.handle) == 0:
        device.check_error()
    messages = []
    current = None
    chunk = 64
    data = (ctypes.c_ubyte * chunk)()
    start = ctypes.c_int()
    stop = ctypes.c_int()
    count = ctypes.c_int()
    nak = ctypes.c_int()
    truncated = False
    deadline = time.monotonic() + duration_s
    try:
        while time.monotonic() < deadline and not truncated:
            count.value = chunk
            if dwf.FDwfDigitalI2cSpyStatus(dd.handle, ctypes.byref(start), ctypes.byref(stop), ctypes.byref(data),
                                           ctypes.byref(count), ctypes.byref(nak)) == 0:
                device.check_error()
            fragment = list(data[:count.value])
            if start.value:
                if current is not None:
                    messages.append(current)
                current = {"restart": start.value == 2, "address": None, "direction": None, "data": [],
                           "nak": 0, "stop": False}
                if fragment:
                    current["address"] = f"0x{fragment[0] >> 1:02x}"
                    current["direction"] = "read" if fragment[0] & 1 else "write"
                    fragment = fragment[1:]
            if current is None and (fragment or stop.value or nak.value):
                current = {"restart": False, "address": None, "direction": None, "data": [], "nak": 0, "stop": False}
            if current is not None:
                current["data"].extend(fragment)
                if nak.value:
                    current["nak"] = nak.value
                if stop.value:
                    current["stop"] = True
                    messages.append(current)
                    current = None
            if len(messages) >= max_messages:
                truncated = True
            if not fragment and not start.value and not stop.value:
                time.sleep(0.001)
    finally:
        dwf.FDwfDigitalI2cReset(dd.handle)
        session.i2c_settings = None
    if current is not None:
        messages.append(current)
    for message in messages:
        message["data_hex"] = " ".join(f"{b:02x}" for b in message["data"])
    return {"duration_s": duration_s, "messages": messages[:max_messages], "count": len(messages),
            "truncated": truncated}
