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


def configure(session, scl_pin, sda_pin, rate, check_bus=True, clock_stretching=True):
    """Set pins, rate and clock-stretch detection (only when changed); optionally verify the bus is
    idle (both lines high)."""
    dd = session.ensure()
    dwf = _dwf()
    (scl_pin,) = check_pins(dd, [scl_pin], "scl_pin")
    (sda_pin,) = check_pins(dd, [sda_pin], "sda_pin")
    if scl_pin == sda_pin:
        raise AD2Error("scl_pin and sda_pin must be different pins")
    if rate <= 0:
        raise AD2Error("rate must be positive (typically 100e3 or 400e3)")
    key = (scl_pin, sda_pin, float(rate), bool(clock_stretching))
    if session.i2c_settings != key:
        previous = session.i2c_settings
        if previous is None or previous[:2] != key[:2]:
            # Changing pins without a reset leaves the engine driving the old SDA pin low, which
            # then reads as an ACK on every address of the new assignment.
            if dwf.FDwfDigitalI2cReset(dd.handle) == 0:
                device.check_error()
        if dwf.FDwfDigitalI2cRateSet(dd.handle, ctypes.c_double(rate)) == 0:
            device.check_error()
        if dwf.FDwfDigitalI2cSclSet(dd.handle, ctypes.c_int(scl_pin)) == 0:
            device.check_error()
        if dwf.FDwfDigitalI2cSdaSet(dd.handle, ctypes.c_int(sda_pin)) == 0:
            device.check_error()
        if dwf.FDwfDigitalI2cStretchSet(dd.handle, ctypes.c_int(1 if clock_stretching else 0)) == 0:
            device.check_error()
        session.i2c_settings = key
    if check_bus:
        free = ctypes.c_int()
        if dwf.FDwfDigitalI2cClear(dd.handle, ctypes.byref(free)) == 0:
            device.check_error()
        if free.value == 0:
            raise AD2Error(f"I2C bus is not free: SDA or SCL (DIO{sda_pin}/DIO{scl_pin}) is held low. {PULLUP_HINT}")
    return dd


METHODS = ("auto", "engine", "bitbang")


def _check_method(method):
    if method not in METHODS:
        raise AD2Error(f"method must be one of: {', '.join(METHODS)}")


def _bitbang_setup(session, scl_pin, sda_pin):
    from . import i2c_bitbang
    dd = session.ensure()
    (scl_pin,) = check_pins(dd, [scl_pin], "scl_pin")
    (sda_pin,) = check_pins(dd, [sda_pin], "sda_pin")
    if scl_pin == sda_pin:
        raise AD2Error("scl_pin and sda_pin must be different pins")
    if session.i2c_settings is not None:
        _dwf().FDwfDigitalI2cReset(dd.handle)       # make sure the engine is not holding the pins
        session.i2c_settings = None
    return i2c_bitbang, dd, scl_pin, sda_pin


def _run(session, scl_pin, sda_pin, rate, operation, bitbang_operation, method="auto", check_bus=True):
    """operation(dd) -> (nak, extra) uses the engine; bitbang_operation(module, dd, scl, sda) -> (nak, extra)
    bit-bangs. auto: engine first, then (on a bus error, with stretch detection off too) bit-bang.
    The WaveForms I2C engine has been seen to report bus errors or phantom ACKs on buses that a
    bit-banged master talks to without trouble."""
    _check_method(method)
    if method == "bitbang":
        module, dd, scl, sda = _bitbang_setup(session, scl_pin, sda_pin)
        nak, extra = bitbang_operation(module, dd, scl, sda)
        return _result(nak, dict(extra or {}, method="bitbang"))
    try:
        dd = configure(session, scl_pin, sda_pin, rate, check_bus)
    except AD2Error:
        if method != "auto":
            raise
        module, dd, scl, sda = _bitbang_setup(session, scl_pin, sda_pin)
        nak, extra = bitbang_operation(module, dd, scl, sda)
        return _result(nak, dict(extra or {}, method="bitbang",
                                 note_method="engine reported the bus not free; bit-banged instead"))
    nak, extra = operation(dd)
    extra = dict(extra or {}, method="engine")
    if nak < 0:
        configure(session, scl_pin, sda_pin, rate, check_bus=False, clock_stretching=False)
        nak, extra = operation(dd)
        extra = dict(extra or {}, method="engine", clock_stretching="disabled after a bus error with stretch detection on")
    if nak < 0 and method == "auto":
        module, dd, scl, sda = _bitbang_setup(session, scl_pin, sda_pin)
        nak, extra = bitbang_operation(module, dd, scl, sda)
        extra = dict(extra or {}, method="bitbang", note_method="engine reported a bus error; bit-banged instead")
    return _result(nak, extra)


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


def write(session, address, data, scl_pin, sda_pin, rate, method="auto") -> dict:
    address = _check_address(address)
    payload = parse_bytes(data)
    base = {"address": f"0x{address:02x}", "written": format_bytes(payload)}

    def operation(dd):
        buffer = (ctypes.c_ubyte * max(1, len(payload)))(*payload)
        nak = ctypes.c_int()
        if _dwf().FDwfDigitalI2cWrite(dd.handle, ctypes.c_int(address << 1), buffer, ctypes.c_int(len(payload)),
                                      ctypes.byref(nak)) == 0:
            device.check_error()
        return nak.value, base

    def bitbang(module, dd, scl, sda):
        return module.write(dd, scl, sda, address, payload), base

    return _run(session, scl_pin, sda_pin, rate, operation, bitbang, method)


def read(session, address, count, scl_pin, sda_pin, rate, method="auto") -> dict:
    address = _check_address(address)
    if not 1 <= count <= 4096:
        raise AD2Error("count must be between 1 and 4096 bytes")

    def operation(dd):
        buffer = (ctypes.c_ubyte * count)()
        nak = ctypes.c_int()
        if _dwf().FDwfDigitalI2cRead(dd.handle, ctypes.c_int(address << 1), buffer, ctypes.c_int(count),
                                     ctypes.byref(nak)) == 0:
            device.check_error()
        data = bytes(buffer) if nak.value == 0 else b""
        return nak.value, {"address": f"0x{address:02x}", "read": format_bytes(data)}

    def bitbang(module, dd, scl, sda):
        nak, data = module.read(dd, scl, sda, address, count)
        return nak, {"address": f"0x{address:02x}", "read": format_bytes(data)}

    return _run(session, scl_pin, sda_pin, rate, operation, bitbang, method)


def write_read(session, address, data, count, scl_pin, sda_pin, rate, method="auto") -> dict:
    """Write (e.g. a register number) then repeated-start read, without releasing the bus."""
    address = _check_address(address)
    payload = parse_bytes(data)
    if not payload:
        raise AD2Error("data must contain at least one byte to write (for example the register address)")
    if not 1 <= count <= 4096:
        raise AD2Error("count must be between 1 and 4096 bytes")

    def operation(dd):
        tx = (ctypes.c_ubyte * len(payload))(*payload)
        rx = (ctypes.c_ubyte * count)()
        nak = ctypes.c_int()
        if _dwf().FDwfDigitalI2cWriteRead(dd.handle, ctypes.c_int(address << 1), tx, ctypes.c_int(len(payload)),
                                          rx, ctypes.c_int(count), ctypes.byref(nak)) == 0:
            device.check_error()
        data_read = bytes(rx) if nak.value == 0 else b""
        return nak.value, {"address": f"0x{address:02x}", "written": format_bytes(payload),
                           "read": format_bytes(data_read)}

    def bitbang(module, dd, scl, sda):
        nak, data_read = module.write_read(dd, scl, sda, address, payload, count)
        return nak, {"address": f"0x{address:02x}", "written": format_bytes(payload), "read": format_bytes(data_read)}

    return _run(session, scl_pin, sda_pin, rate, operation, bitbang, method)


def _probe(dd, address) -> int:
    nak = ctypes.c_int()
    if _dwf().FDwfDigitalI2cWrite(dd.handle, ctypes.c_int(address << 1), None, ctypes.c_int(0),
                                  ctypes.byref(nak)) == 0:
        device.check_error()
    return nak.value


def _stuck(found, first, last) -> bool:
    return len(found) >= 8 and len(found) >= 0.9 * (last - first + 1)


def _bitbang_scan(session, scl_pin, sda_pin, first, last) -> dict:
    module, dd, scl, sda = _bitbang_setup(session, scl_pin, sda_pin)
    found, bus_error = module.scan(dd, scl, sda, first, last)
    out = {"scanned": f"0x{first:02x}-0x{last:02x}", "found": [f"0x{a:02x}" for a in found], "count": len(found),
           "method": "bitbang"}
    if bus_error:
        out["found"], out["count"], out["bus_error"] = [], 0, -1
        out["note"] = f"SDA or SCL (DIO{sda}/DIO{scl}) does not idle high. {PULLUP_HINT}"
    elif _stuck(found, first, last):
        out["found"], out["count"] = [], 0
        out["note"] = (f"{len(found)} of {last - first + 1} addresses acknowledged: SDA is stuck low (shorted, "
                       "driven by something, or SCL/SDA swapped), not a bus full of devices")
    return out


def scan(session, scl_pin, sda_pin, rate, first=0x08, last=0x77, method="auto") -> dict:
    """Probe each address with a zero-length write; devices that ACK are listed. auto: the engine
    first, bit-bang when the engine reports a bus error or phantom ACKs on every address."""
    _check_method(method)
    if not 0 <= first <= last <= 0x7F:
        raise AD2Error("first and last must satisfy 0 <= first <= last <= 0x7F")
    if method == "bitbang":
        return _bitbang_scan(session, scl_pin, sda_pin, first, last)
    try:
        dd = configure(session, scl_pin, sda_pin, rate)
    except AD2Error:
        if method == "auto":
            out = _bitbang_scan(session, scl_pin, sda_pin, first, last)
            out["note_method"] = "engine reported the bus not free; bit-banged instead"
            return out
        raise
    found = []
    bus_error = None
    note = None
    for address in range(first, last + 1):
        nak = _probe(dd, address)
        if nak < 0 and note is None:
            # retry with clock-stretch detection off before declaring a bus error
            configure(session, scl_pin, sda_pin, rate, check_bus=False, clock_stretching=False)
            nak = _probe(dd, address)
            note = "clock-stretch detection disabled after a bus error; the bus itself is fine"
        if nak == 0:
            found.append(f"0x{address:02x}")
        elif nak < 0:
            bus_error = nak
            break
    out = {"scanned": f"0x{first:02x}-0x{last:02x}", "found": found, "count": len(found), "method": "engine"}
    if note:
        out["clock_stretching"] = note
    if bus_error is not None or _stuck(found, first, last):
        if method == "auto":
            why = "a bus error" if bus_error is not None else "phantom ACKs on every address"
            out = _bitbang_scan(session, scl_pin, sda_pin, first, last)
            out["note_method"] = f"engine reported {why}; bit-banged instead"
            return out
        if bus_error is not None:
            out["bus_error"] = bus_error
            out["note"] = f"scan aborted by a bus error ({bus_error}). {PULLUP_HINT}"
        else:
            out["found"], out["count"] = [], 0
            out["note"] = (f"{len(found)} of {last - first + 1} addresses acknowledged: SDA is stuck low (shorted, "
                           "driven by something, or SCL/SDA swapped), not a bus full of devices")
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
