"""Oscilloscope: single-shot capture, long record mode, voltmeter and frequency counter."""

import ctypes
import time

from WF_SDK import device, scope as wf_scope
import dwfconstants as constants

from . import triggers
from .errors import AD2Error

TRIGGER_SOURCES = ("none", "ch1", "ch2", "wavegen1", "wavegen2", "external1", "external2", "external3",
                   "external4", "pc", "pattern", "logic")
TRIGGER_TYPES = {
    "edge": constants.trigtypeEdge,
    "pulse": constants.trigtypePulse,
    "transition": constants.trigtypeTransition,
    "window": constants.trigtypeWindow,
}
LENGTH_CONDITIONS = {
    "less": constants.triglenLess,
    "more": constants.triglenMore,
    "timeout": constants.triglenTimeout,
}
FILTERS = {
    "decimate": constants.filterDecimate,
    "average": constants.filterAverage,
    "min_max": constants.filterMinMax,
}
DEFAULTS = dict(sampling_frequency=20e6, buffer_size=0, range_volts=5.0, offset_volts=0.0)
MAX_RECORD_SAMPLES = 2_000_000


def _dwf():
    return wf_scope.dwf


def configure(session, sampling_frequency, buffer_size, range_volts, offset_volts,
              filter_mode="decimate", attenuation=1.0):
    """Apply scope settings only when they differ from what is already in effect."""
    dd = session.ensure()
    if sampling_frequency <= 0:
        raise AD2Error("sampling_frequency must be positive")
    if buffer_size < 0:
        raise AD2Error("buffer_size must be 0 (maximum) or a positive sample count")
    if range_volts <= 0:
        raise AD2Error("range_volts must be positive")
    if filter_mode not in FILTERS:
        raise AD2Error(f"filter must be one of: {', '.join(FILTERS)}")
    if attenuation <= 0:
        raise AD2Error("attenuation must be positive (1 for a direct connection, 10 for a 10x probe)")
    key = (float(sampling_frequency), int(buffer_size), float(range_volts), float(offset_volts),
           filter_mode, float(attenuation))
    if session.scope_settings != key:
        dwf = _dwf()
        if dwf.FDwfAnalogInChannelAttenuationSet(dd.handle, ctypes.c_int(-1), ctypes.c_double(attenuation)) == 0:
            device.check_error()
        wf_scope.open(dd, key[0], key[1], key[3], key[2])
        if dwf.FDwfAnalogInChannelFilterSet(dd.handle, ctypes.c_int(-1), FILTERS[filter_mode]) == 0:
            device.check_error()
        if dwf.FDwfAnalogInAcquisitionModeSet(dd.handle, constants.acqmodeSingle) == 0:
            device.check_error()
        session.scope_settings = key
    return dd


def _check_channels(dd, channels):
    count = dd.analog.input.channel_count or 2
    channels = list(channels) if channels else list(range(1, count + 1))
    for ch in channels:
        if isinstance(ch, bool) or not isinstance(ch, int) or not 1 <= ch <= count:
            raise AD2Error(f"channels must be between 1 and {count}, got {ch}")
    return channels


def set_trigger(dd, source="none", level=0.0, edge="rising", timeout_s=1.0, trigger_type="edge",
                length_s=0.0, length_condition="more", holdoff_s=0.0, hysteresis_v=None):
    """Full trigger setup. For ch1/ch2 sources the type, level, hysteresis and length apply;
    for every source the auto-trigger timeout and hold-off apply."""
    if source not in TRIGGER_SOURCES:
        raise AD2Error(f"trigger_source must be one of: {', '.join(TRIGGER_SOURCES)}")
    if trigger_type not in TRIGGER_TYPES:
        raise AD2Error(f"trigger_type must be one of: {', '.join(TRIGGER_TYPES)}")
    if length_condition not in LENGTH_CONDITIONS:
        raise AD2Error(f"trigger_length_condition must be one of: {', '.join(LENGTH_CONDITIONS)}")
    if timeout_s < 0 or holdoff_s < 0 or length_s < 0:
        raise AD2Error("trigger_timeout_s, trigger_holdoff_s and trigger_length_s must be zero or positive")
    if hysteresis_v is not None and hysteresis_v < 0:
        raise AD2Error("trigger_hysteresis_v must be zero or positive")
    dwf = _dwf()
    h = dd.handle
    if source == "none":
        if dwf.FDwfAnalogInTriggerSourceSet(h, constants.trigsrcNone) == 0:
            device.check_error()
        return
    if dwf.FDwfAnalogInTriggerAutoTimeoutSet(h, ctypes.c_double(timeout_s)) == 0:
        device.check_error()
    if dwf.FDwfAnalogInTriggerHoldOffSet(h, ctypes.c_double(holdoff_s)) == 0:
        device.check_error()
    if source in ("ch1", "ch2"):
        src = constants.trigsrcDetectorAnalogIn
        if dwf.FDwfAnalogInTriggerChannelSet(h, ctypes.c_int(int(source[2]) - 1)) == 0:
            device.check_error()
        if dwf.FDwfAnalogInTriggerTypeSet(h, TRIGGER_TYPES[trigger_type]) == 0:
            device.check_error()
        if dwf.FDwfAnalogInTriggerLevelSet(h, ctypes.c_double(level)) == 0:
            device.check_error()
        if hysteresis_v is not None:
            if dwf.FDwfAnalogInTriggerHysteresisSet(h, ctypes.c_double(hysteresis_v)) == 0:
                device.check_error()
        if dwf.FDwfAnalogInTriggerConditionSet(h, triggers.slope(edge)) == 0:
            device.check_error()
        if trigger_type != "edge":
            if dwf.FDwfAnalogInTriggerLengthSet(h, ctypes.c_double(length_s)) == 0:
                device.check_error()
            if dwf.FDwfAnalogInTriggerLengthConditionSet(h, LENGTH_CONDITIONS[length_condition]) == 0:
                device.check_error()
    else:
        src = triggers.SOURCES[source]
        if dwf.FDwfAnalogInTriggerConditionSet(h, triggers.slope(edge)) == 0:
            device.check_error()
    if dwf.FDwfAnalogInTriggerSourceSet(h, src) == 0:
        device.check_error()


def _wait_done(dd, max_wait_s, describe):
    dwf = _dwf()
    deadline = time.monotonic() + max_wait_s
    status = ctypes.c_byte()
    while True:
        if dwf.FDwfAnalogInStatus(dd.handle, ctypes.c_bool(True), ctypes.byref(status)) == 0:
            device.check_error()
        if status.value == constants.DwfStateDone.value:
            return
        if time.monotonic() > deadline:
            dwf.FDwfAnalogInConfigure(dd.handle, ctypes.c_bool(False), ctypes.c_bool(False))
            raise AD2Error(
                f"Acquisition did not complete within {max_wait_s} s ({describe}). Check the signal reaches "
                "the trigger condition, raise trigger_timeout_s so the scope auto-triggers, or use "
                "trigger_source='none'."
            )
        time.sleep(0.001)


def capture(session, channels=None, sampling_frequency=20e6, buffer_size=0, range_volts=5.0,
            offset_volts=0.0, trigger_source="none", trigger_level=0.0, trigger_edge="rising",
            trigger_timeout_s=1.0, max_wait_s=5.0, trigger_type="edge", trigger_length_s=0.0,
            trigger_length_condition="more", trigger_holdoff_s=0.0, trigger_hysteresis_v=None,
            filter_mode="decimate", attenuation=1.0):
    """One acquisition, read back for every requested channel. Returns (actual_fs, {channel: samples})."""
    dd = configure(session, sampling_frequency, buffer_size, range_volts, offset_volts, filter_mode, attenuation)
    channels = _check_channels(dd, channels)
    if max_wait_s <= 0:
        raise AD2Error("max_wait_s must be positive")
    set_trigger(dd, trigger_source, trigger_level, trigger_edge, trigger_timeout_s, trigger_type,
                trigger_length_s, trigger_length_condition, trigger_holdoff_s, trigger_hysteresis_v)
    dwf = _dwf()
    # reconfigure=True pushes the trigger settings to the device before arming; without it,
    # external and cross-instrument trigger sources never fire on the AD2.
    if dwf.FDwfAnalogInConfigure(dd.handle, ctypes.c_bool(True), ctypes.c_bool(True)) == 0:
        device.check_error()
    _wait_done(dd, max_wait_s, f"trigger_source='{trigger_source}', type={trigger_type}, level={trigger_level} V")
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


def record(session, channels=None, sampling_frequency=100e3, duration_s=1.0, range_volts=5.0,
           offset_volts=0.0, trigger_source="none", trigger_level=0.0, trigger_edge="rising",
           trigger_timeout_s=1.0, max_wait_s=None, filter_mode="decimate", attenuation=1.0):
    """Stream a record longer than the buffer. Returns (fs, {channel: samples}, lost, corrupted)."""
    if duration_s <= 0:
        raise AD2Error("duration_s must be positive")
    dd = configure(session, sampling_frequency, 0, range_volts, offset_volts, filter_mode, attenuation)
    channels = _check_channels(dd, channels)
    dwf = _dwf()
    actual = ctypes.c_double()
    dwf.FDwfAnalogInFrequencyGet(dd.handle, ctypes.byref(actual))
    fs = actual.value or float(sampling_frequency)
    total = int(round(duration_s * fs))
    if total < 1:
        raise AD2Error("duration_s is shorter than one sample period")
    if total > MAX_RECORD_SAMPLES:
        raise AD2Error(f"duration_s * sampling_frequency = {total} samples exceeds the {MAX_RECORD_SAMPLES} "
                       "limit per channel; lower the sample rate or duration")
    if max_wait_s is None:
        max_wait_s = duration_s + 5.0
    set_trigger(dd, trigger_source, trigger_level, trigger_edge, trigger_timeout_s)
    buffers = {ch: (ctypes.c_double * total)() for ch in channels}
    collected = lost = corrupted = 0
    session.scope_settings = None  # record mode changes the acquisition mode; force a reconfigure next time
    try:
        if dwf.FDwfAnalogInAcquisitionModeSet(dd.handle, constants.acqmodeRecord) == 0:
            device.check_error()
        if dwf.FDwfAnalogInRecordLengthSet(dd.handle, ctypes.c_double(total / fs)) == 0:
            device.check_error()
        if dwf.FDwfAnalogInConfigure(dd.handle, ctypes.c_bool(True), ctypes.c_bool(True)) == 0:
            device.check_error()
        status = ctypes.c_byte()
        available = ctypes.c_int()
        lost_now = ctypes.c_int()
        corrupt_now = ctypes.c_int()
        deadline = time.monotonic() + max_wait_s
        while collected < total:
            if dwf.FDwfAnalogInStatus(dd.handle, ctypes.c_bool(True), ctypes.byref(status)) == 0:
                device.check_error()
            if collected == 0 and status.value in (constants.DwfStateConfig.value, constants.DwfStatePrefill.value,
                                                   constants.DwfStateArmed.value):
                if time.monotonic() > deadline:
                    raise AD2Error(f"record did not start within {max_wait_s} s (trigger_source='{trigger_source}')")
                time.sleep(0.001)
                continue
            if dwf.FDwfAnalogInStatusRecord(dd.handle, ctypes.byref(available), ctypes.byref(lost_now),
                                            ctypes.byref(corrupt_now)) == 0:
                device.check_error()
            collected += lost_now.value
            lost += lost_now.value
            corrupted += corrupt_now.value
            take = min(available.value, total - collected)
            if take > 0:
                for ch in channels:
                    chunk = (ctypes.c_double * take).from_buffer(buffers[ch], ctypes.sizeof(ctypes.c_double) * collected)
                    if dwf.FDwfAnalogInStatusData(dd.handle, ctypes.c_int(ch - 1), chunk, ctypes.c_int(take)) == 0:
                        device.check_error()
                collected += take
            elif status.value == constants.DwfStateDone.value:
                break
            if time.monotonic() > deadline:
                raise AD2Error(f"record did not finish within {max_wait_s} s ({collected} of {total} samples)")
            if take == 0:
                time.sleep(0.001)
    finally:
        dwf.FDwfAnalogInConfigure(dd.handle, ctypes.c_bool(False), ctypes.c_bool(False))
        dwf.FDwfAnalogInAcquisitionModeSet(dd.handle, constants.acqmodeSingle)
    data = {ch: list(buffers[ch])[:collected] for ch in channels}
    return fs, data, lost, corrupted


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


def counter_supported(dd) -> bool:
    """The AD2 firmware has no counter (max gate time 0); newer devices do."""
    count_max, sec_max = ctypes.c_double(), ctypes.c_double()
    if _dwf().FDwfAnalogInCounterInfo(dd.handle, ctypes.byref(count_max), ctypes.byref(sec_max)) == 0:
        device.check_error()
    return sec_max.value > 0


def rising_crossings(samples, level: float, hysteresis: float | None):
    """Indices where the signal rises through `level` with hysteresis (default 5 % of the swing)."""
    if not samples:
        return []
    if hysteresis is None:
        hysteresis = 0.05 * (max(samples) - min(samples))
    above = None
    crossings = []
    for i, v in enumerate(samples):
        d = v - level
        if above is None:
            if abs(d) > hysteresis:
                above = d > 0
        elif not above and d > hysteresis:
            above = True
            crossings.append(i)
        elif above and d < -hysteresis:
            above = False
    return crossings


def _frequency_from_crossings(crossings, fs):
    if len(crossings) < 2:
        return None
    return (len(crossings) - 1) * fs / (crossings[-1] - crossings[0])


def _software_frequency(session, channel, gate_time_s, level, hysteresis_v, range_volts, offset_volts) -> dict:
    """Fast capture first (good above ~50 kHz); otherwise a record over the gate time."""
    fs, data = capture(session, [channel], 100e6, 0, range_volts, offset_volts)
    crossings = rising_crossings(data[channel], level, hysteresis_v)
    if len(crossings) >= 20:
        return {"channel": channel, "frequency_hz": _frequency_from_crossings(crossings, fs),
                "edges_counted": len(crossings), "gate_time_s": len(data[channel]) / fs,
                "trigger_level_v": level, "method": "software_capture"}
    fs_slow = min(1e6, MAX_RECORD_SAMPLES / gate_time_s)
    fs, data, lost, _ = record(session, [channel], fs_slow, gate_time_s, range_volts, offset_volts)
    crossings = rising_crossings(data[channel], level, hysteresis_v)
    frequency = _frequency_from_crossings(crossings, fs)
    if frequency is None:
        raise AD2Error(f"fewer than two level crossings on channel {channel} in {gate_time_s} s; check the "
                       "signal crosses trigger_level or lengthen gate_time_s")
    return {"channel": channel, "frequency_hz": frequency, "edges_counted": len(crossings),
            "gate_time_s": gate_time_s, "trigger_level_v": level, "method": "software_record",
            "samples_lost": lost}


def count_frequency(session, channel=1, gate_time_s=0.1, level=0.0, hysteresis_v=None, range_volts=5.0,
                    offset_volts=0.0) -> dict:
    """Frequency counter: the hardware counter where the device has one, else a software count."""
    if not 0.001 <= gate_time_s <= 10:
        raise AD2Error("gate_time_s must be between 0.001 and 10 seconds")
    dd = configure(session, 100e6, 0, range_volts, offset_volts)
    (channel,) = _check_channels(dd, [channel])
    if not counter_supported(dd):
        return _software_frequency(session, channel, gate_time_s, level, hysteresis_v, range_volts, offset_volts)
    dwf = _dwf()
    set_trigger(dd, f"ch{channel}", level, "rising", 0.0, "edge", hysteresis_v=hysteresis_v)
    session.scope_settings = None  # counter mode leaves the scope configured differently
    if dwf.FDwfAnalogInCounterSet(dd.handle, ctypes.c_double(gate_time_s)) == 0:
        device.check_error()
    if dwf.FDwfAnalogInConfigure(dd.handle, ctypes.c_bool(True), ctypes.c_bool(True)) == 0:
        device.check_error()
    count, frequency, tick = ctypes.c_double(), ctypes.c_double(), ctypes.c_int()
    try:
        dwf.FDwfAnalogInStatus(dd.handle, ctypes.c_bool(False), None)
        dwf.FDwfAnalogInCounterStatus(dd.handle, ctypes.byref(count), ctypes.byref(frequency), ctypes.byref(tick))
        first_tick = tick.value
        deadline = time.monotonic() + 2 * gate_time_s + 2.0
        while True:
            time.sleep(min(0.01, gate_time_s / 4))
            if dwf.FDwfAnalogInStatus(dd.handle, ctypes.c_bool(False), None) == 0:
                device.check_error()
            dwf.FDwfAnalogInCounterStatus(dd.handle, ctypes.byref(count), ctypes.byref(frequency), ctypes.byref(tick))
            if tick.value != first_tick:
                break
            if time.monotonic() > deadline:
                raise AD2Error("the counter produced no measurement; check the signal crosses the trigger level")
    finally:
        dwf.FDwfAnalogInCounterSet(dd.handle, ctypes.c_double(0))
        dwf.FDwfAnalogInConfigure(dd.handle, ctypes.c_bool(False), ctypes.c_bool(False))
    return {"channel": channel, "frequency_hz": frequency.value, "edges_counted": count.value,
            "gate_time_s": gate_time_s, "trigger_level_v": level, "method": "hardware"}
