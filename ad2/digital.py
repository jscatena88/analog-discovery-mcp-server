"""Static digital I/O on DIO 0-15 (read levels, drive pins high/low, release to input)."""

import ctypes

from WF_SDK import device

from .errors import AD2Error


def _dwf():
    return device.dwf


def pin_count(dd) -> int:
    return min(dd.digital.input.channel_count, dd.digital.output.channel_count) or 16


def check_pins(dd, pins, name: str) -> list[int]:
    count = pin_count(dd)
    out = []
    for pin in pins or []:
        if isinstance(pin, bool) or not isinstance(pin, int) or not 0 <= pin < count:
            raise AD2Error(f"{name} must contain DIO pin numbers between 0 and {count - 1}, got {pin!r}")
        out.append(pin)
    return out


def _mask(pins) -> int:
    return sum(1 << p for p in pins)


def _get_u32(fn, dd) -> int:
    value = ctypes.c_uint32()
    if fn(dd.handle, ctypes.byref(value)) == 0:
        device.check_error()
    return value.value


def read(dd) -> dict:
    """Current level of every pin (outputs read back what they drive) plus the direction map."""
    dwf = _dwf()
    if dwf.FDwfDigitalIOStatus(dd.handle) == 0:
        device.check_error()
    levels = _get_u32(dwf.FDwfDigitalIOInputStatus, dd)
    output_enable = _get_u32(dwf.FDwfDigitalIOOutputEnableGet, dd)
    count = pin_count(dd)
    return {
        "value": levels,
        "value_hex": f"0x{levels:04x}",
        "levels": {f"dio{p}": (levels >> p) & 1 for p in range(count)},
        "high_pins": [p for p in range(count) if (levels >> p) & 1],
        "output_pins": [p for p in range(count) if (output_enable >> p) & 1],
    }


def write(dd, high=None, low=None, inputs=None) -> dict:
    """Drive `high` pins to 1 and `low` pins to 0; release `inputs` pins to high impedance."""
    high = check_pins(dd, high, "high")
    low = check_pins(dd, low, "low")
    inputs = check_pins(dd, inputs, "inputs")
    overlap = (set(high) & set(low)) | (set(high) & set(inputs)) | (set(low) & set(inputs))
    if overlap:
        raise AD2Error(f"pins listed in more than one of high/low/inputs: {sorted(overlap)}")
    if not (high or low or inputs):
        raise AD2Error("nothing to do: list pins in high, low or inputs")
    dwf = _dwf()
    full = (1 << pin_count(dd)) - 1
    output_enable = _get_u32(dwf.FDwfDigitalIOOutputEnableGet, dd)
    output_value = _get_u32(dwf.FDwfDigitalIOOutputGet, dd)
    output_enable = (output_enable | _mask(high) | _mask(low)) & ~_mask(inputs) & full
    output_value = (output_value | _mask(high)) & ~_mask(low) & full
    if dwf.FDwfDigitalIOOutputEnableSet(dd.handle, ctypes.c_int(output_enable)) == 0:
        device.check_error()
    if dwf.FDwfDigitalIOOutputSet(dd.handle, ctypes.c_int(output_value)) == 0:
        device.check_error()
    if dwf.FDwfDigitalIOConfigure(dd.handle) == 0:
        device.check_error()
    return read(dd)


def reset(dd) -> None:
    """Release every pin to input."""
    if _dwf().FDwfDigitalIOReset(dd.handle) == 0:
        device.check_error()
