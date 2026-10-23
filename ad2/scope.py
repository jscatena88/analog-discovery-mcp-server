"""Oscilloscope: single-shot capture with optional trigger, plus voltmeter readings."""

import ctypes
import time

from WF_SDK import device, scope as wf_scope
import dwfconstants as constants

from .errors import AD2Error

TRIGGER_SOURCES = {
    "none": None,
    "ch1": ("analog", 1),
    "ch2": ("analog", 2),
    "wavegen1": ("wavegen", 1),
    "wavegen2": ("wavegen", 2),
    "external1": ("external", 1),
    "external2": ("external", 2),
    "external3": ("external", 3),
    "external4": ("external", 4),
}
TRIGGER_EDGES = ("rising", "falling")
DEFAULTS = dict(sampling_frequency=20e6, buffer_size=0, range_volts=5.0, offset_volts=0.0)


def configure(session, sampling_frequency, buffer_size, range_volts, offset_volts):
    """Apply scope settings only when they differ from what is already in effect."""
    dd = session.ensure()
    if sampling_frequency <= 0:
        raise AD2Error("sampling_frequency must be positive")
    if buffer_size < 0:
        raise AD2Error("buffer_size must be 0 (maximum) or a positive sample count")
    if range_volts <= 0:
        raise AD2Error("range_volts must be positive")
    key = (float(sampling_frequency), int(buffer_size), float(range_volts), float(offset_volts))
    if session.scope_settings != key:
        wf_scope.open(dd, key[0], key[1], key[3], key[2])
        session.scope_settings = key
    return dd


def _check_channels(dd, channels):
    count = dd.analog.input.channel_count or 2
    channels = list(channels) if channels else list(range(1, count + 1))
    for ch in channels:
        if not isinstance(ch, int) or not 1 <= ch <= count:
            raise AD2Error(f"channels must be between 1 and {count}, got {ch}")
    return channels


def _set_trigger(dd, source, level, edge, timeout_s):
    if source not in TRIGGER_SOURCES:
        raise AD2Error(f"trigger_source must be one of: {', '.join(TRIGGER_SOURCES)}")
    if edge not in TRIGGER_EDGES:
        raise AD2Error(f"trigger_edge must be one of: {', '.join(TRIGGER_EDGES)}")
    if timeout_s < 0:
        raise AD2Error("trigger_timeout_s must be 0 (wait indefinitely, bounded by max_wait_s) or positive")
    spec = TRIGGER_SOURCES[source]
    if spec is None:
        wf_scope.trigger(dd, False)
        return
    kind, index = spec
    rising = edge == "rising"
    if kind == "analog":
        wf_scope.trigger(dd, True, wf_scope.trigger_source.analog, index, timeout_s, rising, level)
    elif kind == "wavegen":
        wf_scope.trigger(dd, True, wf_scope.trigger_source.wavegen[index], 1, timeout_s, rising, level)
    else:
        wf_scope.trigger(dd, True, wf_scope.trigger_source.external[index], 1, timeout_s, rising, level)


def capture(session, channels=None, sampling_frequency=20e6, buffer_size=0, range_volts=5.0,
            offset_volts=0.0, trigger_source="none", trigger_level=0.0, trigger_edge="rising",
            trigger_timeout_s=1.0, max_wait_s=5.0):
    """One acquisition, read back for every requested channel. Returns (actual_fs, {channel: samples})."""
    dd = configure(session, sampling_frequency, buffer_size, range_volts, offset_volts)
    channels = _check_channels(dd, channels)
    if max_wait_s <= 0:
        raise AD2Error("max_wait_s must be positive")
    _set_trigger(dd, trigger_source, trigger_level, trigger_edge, trigger_timeout_s)

    dwf = wf_scope.dwf
    # reconfigure=True pushes the trigger settings to the device before arming; without it,
    # external and cross-instrument trigger sources never fire on the AD2.
    if dwf.FDwfAnalogInConfigure(dd.handle, ctypes.c_bool(True), ctypes.c_bool(True)) == 0:
        device.check_error()
    deadline = time.monotonic() + max_wait_s
    status = ctypes.c_byte()
    while True:
        if dwf.FDwfAnalogInStatus(dd.handle, ctypes.c_bool(True), ctypes.byref(status)) == 0:
            device.check_error()
        if status.value == constants.DwfStateDone.value:
            break
        if time.monotonic() > deadline:
            dwf.FDwfAnalogInConfigure(dd.handle, ctypes.c_bool(False), ctypes.c_bool(False))
            raise AD2Error(
                f"Acquisition did not complete within {max_wait_s} s (trigger_source='{trigger_source}', "
                f"level={trigger_level} V). Check the signal reaches the trigger level, raise "
                "trigger_timeout_s so the scope auto-triggers, or use trigger_source='none'."
            )
        time.sleep(0.001)

    n = wf_scope.data.buffer_size
    actual = ctypes.c_double()
    dwf.FDwfAnalogInFrequencyGet(dd.handle, ctypes.byref(actual))
    fs = actual.value or float(sampling_frequency)
    data = {}
    for ch in channels:
        buffer = (ctypes.c_double * n)()
        if dwf.FDwfAnalogInStatusData(dd.handle, ctypes.c_int(ch - 1), buffer, ctypes.c_int(n)) == 0:
            device.check_error()
        data[ch] = list(buffer)
    return fs, data


def measure(session, channel: int, samples: int = 16) -> dict:
    """Instantaneous voltmeter reading, averaged over `samples` quick reads."""
    if session.scope_settings is None:
        configure(session, **DEFAULTS)
    dd = session.device_data
    (channel,) = _check_channels(dd, [channel])
    if samples < 1:
        raise AD2Error("samples must be at least 1")
    readings = [wf_scope.measure(dd, channel) for _ in range(samples)]
    return {
        "channel": channel,
        "voltage_v": sum(readings) / len(readings),
        "min_v": min(readings),
        "max_v": max(readings),
        "samples": len(readings),
    }
