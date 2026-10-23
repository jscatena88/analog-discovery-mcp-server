"""Pattern generator: clocks, pulses, custom bit streams and noise on DIO 0-15."""

import ctypes
import math

from WF_SDK import device
import dwfconstants as constants

from .errors import AD2Error

KINDS = ("clock", "pulse", "custom", "random")
IDLE_STATES = {
    "initial": constants.DwfDigitalOutIdleInit,
    "low": constants.DwfDigitalOutIdleLow,
    "high": constants.DwfDigitalOutIdleHigh,
    "high_impedance": constants.DwfDigitalOutIdleZet,
}


def _dwf():
    return device.dwf


def channel_count(dd) -> int:
    return dd.digital.output.channel_count or 16


def internal_clock(dd) -> float:
    value = ctypes.c_double()
    if _dwf().FDwfDigitalOutInternalClockInfo(dd.handle, ctypes.byref(value)) == 0:
        device.check_error()
    return value.value


def _counter_limit(dd, channel) -> int:
    value = ctypes.c_uint()
    if _dwf().FDwfDigitalOutCounterInfo(dd.handle, ctypes.c_int(channel), None, ctypes.byref(value)) == 0:
        device.check_error()
    return value.value or 32768


def parse_bits(data) -> list[int]:
    """Accept "1011 0010", "0b1011", or a list of 0/1/bool; returns a list of ints, first bit sent first."""
    if data is None:
        return []
    if isinstance(data, str):
        text = data.strip()
        if text.lower().startswith("0b"):
            text = text[2:]
        text = text.replace(" ", "").replace("_", "")
        if set(text) - {"0", "1"}:
            raise AD2Error("data string may only contain 0 and 1 (spaces and underscores are ignored)")
        return [int(ch) for ch in text]
    bits = []
    for item in data:
        if item in (0, 1, False, True):
            bits.append(int(item))
        else:
            raise AD2Error(f"data list may only contain 0 and 1, got {item!r}")
    return bits


def generate(session, channel, kind="clock", frequency=1e3, duty_cycle=50.0, data=None,
             idle="initial", run_time_s=0.0, wait_s=0.0, repeat=0) -> dict:
    dd = session.ensure()
    dwf = _dwf()
    count = channel_count(dd)
    if isinstance(channel, bool) or not isinstance(channel, int) or not 0 <= channel < count:
        raise AD2Error(f"channel must be a DIO pin number between 0 and {count - 1}")
    if kind not in KINDS:
        raise AD2Error(f"kind must be one of: {', '.join(KINDS)}")
    if frequency <= 0:
        raise AD2Error("frequency must be positive")
    if idle not in IDLE_STATES:
        raise AD2Error(f"idle must be one of: {', '.join(IDLE_STATES)}")
    if run_time_s < 0 or wait_s < 0 or repeat < 0:
        raise AD2Error("run_time_s, wait_s and repeat must be zero or positive")
    clock = internal_clock(dd)
    handle, ch = dd.handle, ctypes.c_int(channel)
    result = {"channel": channel, "kind": kind, "idle": idle,
              "run_time_s": run_time_s, "wait_s": wait_s, "repeat": repeat, "running": True}

    if kind in ("clock", "pulse"):
        if kind == "clock":
            duty_cycle = 50.0
        if not 0 <= duty_cycle <= 100:
            raise AD2Error("duty_cycle must be a percentage between 0 and 100")
        if frequency > clock / 2:
            raise AD2Error(f"frequency must be at most {clock / 2:g} Hz for clock/pulse output")
        limit = _counter_limit(dd, channel)
        divider = max(1, math.ceil(clock / frequency / limit))
        steps = int(round(clock / frequency / divider))
        if steps < 2:
            raise AD2Error(f"frequency {frequency:g} Hz cannot be produced from the {clock:g} Hz clock")
        high = int(round(steps * duty_cycle / 100))
        low = steps - high
        if dwf.FDwfDigitalOutTypeSet(handle, ch, constants.DwfDigitalOutTypePulse) == 0:
            device.check_error()
        if dwf.FDwfDigitalOutDividerSet(handle, ch, ctypes.c_int(divider)) == 0:
            device.check_error()
        if dwf.FDwfDigitalOutCounterSet(handle, ch, ctypes.c_int(low), ctypes.c_int(high)) == 0:
            device.check_error()
        result.update(frequency_hz=clock / divider / steps, duty_cycle_percent=100.0 * high / steps)

    elif kind == "custom":
        bits = parse_bits(data)
        if not bits:
            raise AD2Error("data is required for kind='custom': a string like \"10110010\" or a list of 0/1")
        max_bits = dd.digital.output.max_buffer_size or 1024
        if len(bits) > max_bits:
            raise AD2Error(f"data has {len(bits)} bits; this configuration allows at most {max_bits}")
        divider = int(clock / frequency)
        if divider < 1:
            raise AD2Error(f"frequency (bit rate) must be at most {clock:g} Hz")
        packed = (ctypes.c_ubyte * ((len(bits) + 7) // 8))()
        for index, bit in enumerate(bits):
            if bit:
                packed[index >> 3] |= 1 << (index & 7)
        if dwf.FDwfDigitalOutTypeSet(handle, ch, constants.DwfDigitalOutTypeCustom) == 0:
            device.check_error()
        if dwf.FDwfDigitalOutDividerSet(handle, ch, ctypes.c_int(divider)) == 0:
            device.check_error()
        if dwf.FDwfDigitalOutDataSet(handle, ch, ctypes.byref(packed), ctypes.c_int(len(bits))) == 0:
            device.check_error()
        bit_rate = clock / divider
        result.update(bit_rate_hz=bit_rate, bits=len(bits), pattern_period_s=len(bits) / bit_rate)

    else:  # random
        divider = int(clock / frequency)
        if divider < 1:
            raise AD2Error(f"frequency (bit rate) must be at most {clock:g} Hz")
        if dwf.FDwfDigitalOutTypeSet(handle, ch, constants.DwfDigitalOutTypeRandom) == 0:
            device.check_error()
        if dwf.FDwfDigitalOutDividerSet(handle, ch, ctypes.c_int(divider)) == 0:
            device.check_error()
        if dwf.FDwfDigitalOutCounterSet(handle, ch, ctypes.c_int(1), ctypes.c_int(1)) == 0:
            device.check_error()
        result.update(bit_rate_hz=clock / divider)

    if dwf.FDwfDigitalOutIdleSet(handle, ch, IDLE_STATES[idle]) == 0:
        device.check_error()
    if dwf.FDwfDigitalOutEnableSet(handle, ch, ctypes.c_int(1)) == 0:
        device.check_error()
    # Wait / run / repeat apply to the whole pattern generator, not per channel.
    if dwf.FDwfDigitalOutWaitSet(handle, ctypes.c_double(wait_s)) == 0:
        device.check_error()
    if dwf.FDwfDigitalOutRunSet(handle, ctypes.c_double(run_time_s)) == 0:
        device.check_error()
    if dwf.FDwfDigitalOutRepeatSet(handle, ctypes.c_int(repeat)) == 0:
        device.check_error()
    if dwf.FDwfDigitalOutConfigure(handle, ctypes.c_int(1)) == 0:
        device.check_error()
    session.pattern_running.add(channel)
    result["running_channels"] = sorted(session.pattern_running)
    return result


def stop(session, channel=None) -> dict:
    dd = session.ensure()
    dwf = _dwf()
    if channel is None:
        stopped = sorted(session.pattern_running)
        if dwf.FDwfDigitalOutReset(dd.handle) == 0:
            device.check_error()
        session.pattern_running.clear()
    else:
        count = channel_count(dd)
        if isinstance(channel, bool) or not isinstance(channel, int) or not 0 <= channel < count:
            raise AD2Error(f"channel must be a DIO pin number between 0 and {count - 1}, or omitted for all")
        if dwf.FDwfDigitalOutEnableSet(dd.handle, ctypes.c_int(channel), ctypes.c_int(0)) == 0:
            device.check_error()
        session.pattern_running.discard(channel)
        if dwf.FDwfDigitalOutConfigure(dd.handle, ctypes.c_int(1 if session.pattern_running else 0)) == 0:
            device.check_error()
        stopped = [channel]
    return {"stopped_channels": stopped, "running_channels": sorted(session.pattern_running)}
