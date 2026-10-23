"""Logic analyzer: single-shot capture, long record mode and frequency counter on DIO 0-15."""

import ctypes
import time

from WF_SDK import device
import dwfconstants as constants

from . import triggers
from .errors import AD2Error
from .digital import pin_count, check_pins

TRIGGER_EDGES = ("rising", "falling", "either")
TRIGGER_SOURCES = ("logic", "scope", "pattern", "wavegen1", "wavegen2", "pc",
                   "external1", "external2", "external3", "external4")
MAX_RECORD_SAMPLES = 4_000_000


def _dwf():
    return device.dwf


def internal_clock(dd) -> float:
    value = ctypes.c_double()
    if _dwf().FDwfDigitalInInternalClockInfo(dd.handle, ctypes.byref(value)) == 0:
        device.check_error()
    return value.value


def configure(session, sampling_frequency, buffer_size):
    """Returns (device, actual sampling frequency, samples). Reapplies settings only when they change."""
    dd = session.ensure()
    dwf = _dwf()
    clock = internal_clock(dd)
    if sampling_frequency <= 0 or sampling_frequency > clock:
        raise AD2Error(f"sampling_frequency must be between 1 and {clock:g} Hz")
    divider = max(1, int(round(clock / sampling_frequency)))
    actual = clock / divider
    max_samples = dd.digital.input.max_buffer_size or 4096
    if buffer_size < 0:
        raise AD2Error("buffer_size must be 0 (maximum) or a positive sample count")
    if buffer_size == 0:
        buffer_size = max_samples
    if buffer_size > max_samples:
        raise AD2Error(f"buffer_size {buffer_size} exceeds this configuration's maximum of {max_samples}; "
                       "connect_device(configuration=3) gives a 16384-sample logic buffer, or use record_logic")
    key = (divider, buffer_size)
    if session.logic_settings != key:
        if dwf.FDwfDigitalInAcquisitionModeSet(dd.handle, constants.acqmodeSingle) == 0:
            device.check_error()
        if dwf.FDwfDigitalInDividerSet(dd.handle, ctypes.c_int(divider)) == 0:
            device.check_error()
        if dwf.FDwfDigitalInSampleFormatSet(dd.handle, ctypes.c_int(16)) == 0:
            device.check_error()
        if dwf.FDwfDigitalInBufferSizeSet(dd.handle, ctypes.c_int(buffer_size)) == 0:
            device.check_error()
        session.logic_settings = key
    return dd, actual, buffer_size


def trigger_masks(dd, trigger_pin=None, trigger_edge="rising", rising=None, falling=None, high=None, low=None):
    """Combine the single-pin convenience arguments with the multi-pin lists into four masks."""
    if trigger_edge not in TRIGGER_EDGES:
        raise AD2Error(f"trigger_edge must be one of: {', '.join(TRIGGER_EDGES)}")
    rising = set(check_pins(dd, rising, "trigger_rising"))
    falling = set(check_pins(dd, falling, "trigger_falling"))
    high = set(check_pins(dd, high, "trigger_high"))
    low = set(check_pins(dd, low, "trigger_low"))
    if trigger_pin is not None:
        (trigger_pin,) = check_pins(dd, [trigger_pin], "trigger_pin")
        if trigger_edge in ("rising", "either"):
            rising.add(trigger_pin)
        if trigger_edge in ("falling", "either"):
            falling.add(trigger_pin)
    if high & low:
        raise AD2Error(f"pins cannot be required both high and low: {sorted(high & low)}")
    mask = lambda pins: sum(1 << p for p in pins)  # noqa: E731
    return mask(low), mask(high), mask(rising), mask(falling)


def set_trigger(dd, n, source="logic", masks=(0, 0, 0, 0), pretrigger_samples=0, timeout_s=1.0,
                count=1, length_min_s=0.0, length_max_s=0.0, slope="rising"):
    """Configure the DigitalIn trigger. Returns the trigger index inside the buffer, or None."""
    dwf = _dwf()
    h = dd.handle
    low, high, rise, fall = masks
    armed = source != "none" and (source != "logic" or any(masks))
    if not armed:
        if dwf.FDwfDigitalInTriggerSourceSet(h, constants.trigsrcNone) == 0:
            device.check_error()
        dwf.FDwfDigitalInTriggerPositionSet(h, ctypes.c_int(n))
        dwf.FDwfDigitalInTriggerPrefillSet(h, ctypes.c_int(0))
        return None
    if source not in TRIGGER_SOURCES:
        raise AD2Error(f"trigger_source must be one of: {', '.join(TRIGGER_SOURCES)}")
    if not 0 <= pretrigger_samples < n:
        raise AD2Error(f"pretrigger_samples must be between 0 and {n - 1}")
    if timeout_s < 0:
        raise AD2Error("trigger_timeout_s must be 0 (never auto-trigger) or positive")
    if count < 1:
        raise AD2Error("trigger_count must be at least 1")
    if length_min_s < 0 or length_max_s < 0 or (length_max_s and length_max_s < length_min_s):
        raise AD2Error("trigger_length_min_s / trigger_length_max_s must be zero or positive, with max >= min")
    if dwf.FDwfDigitalInTriggerSourceSet(h, triggers.SOURCES[source]) == 0:
        device.check_error()
    if source == "logic":
        if dwf.FDwfDigitalInTriggerSet(h, ctypes.c_int(low), ctypes.c_int(high), ctypes.c_int(rise),
                                       ctypes.c_int(fall)) == 0:
            device.check_error()
        if dwf.FDwfDigitalInTriggerCountSet(h, ctypes.c_int(count), ctypes.c_int(0)) == 0:
            device.check_error()
        if dwf.FDwfDigitalInTriggerLengthSet(h, ctypes.c_double(length_min_s), ctypes.c_double(length_max_s),
                                             ctypes.c_int(0)) == 0:
            device.check_error()
    else:
        if dwf.FDwfDigitalInTriggerSlopeSet(h, triggers.slope(slope)) == 0:
            device.check_error()
    if dwf.FDwfDigitalInTriggerPositionSet(h, ctypes.c_int(n - pretrigger_samples)) == 0:
        device.check_error()
    if dwf.FDwfDigitalInTriggerPrefillSet(h, ctypes.c_int(pretrigger_samples)) == 0:
        device.check_error()
    if dwf.FDwfDigitalInTriggerAutoTimeoutSet(h, ctypes.c_double(timeout_s)) == 0:
        device.check_error()
    return pretrigger_samples


def _wait_done(dd, max_wait_s, describe):
    dwf = _dwf()
    deadline = time.monotonic() + max_wait_s
    status = ctypes.c_byte()
    while True:
        if dwf.FDwfDigitalInStatus(dd.handle, ctypes.c_bool(True), ctypes.byref(status)) == 0:
            device.check_error()
        if status.value == constants.DwfStateDone.value:
            return
        if time.monotonic() > deadline:
            dwf.FDwfDigitalInConfigure(dd.handle, ctypes.c_bool(False), ctypes.c_bool(False))
            raise AD2Error(
                f"Logic capture did not complete within {max_wait_s} s ({describe}). Check the trigger "
                "condition occurs, raise trigger_timeout_s so the analyzer auto-triggers, or remove the trigger."
            )
        time.sleep(0.001)


def capture(session, sampling_frequency=100e6, buffer_size=0, trigger_pin=None, trigger_edge="rising",
            pretrigger_samples=0, trigger_timeout_s=1.0, max_wait_s=5.0, trigger_rising=None,
            trigger_falling=None, trigger_high=None, trigger_low=None, trigger_count=1,
            trigger_length_min_s=0.0, trigger_length_max_s=0.0, trigger_source="logic", trigger_slope="rising"):
    """One acquisition. Returns (actual_fs, samples as 16-bit ints, trigger index or None)."""
    dd, fs, n = configure(session, sampling_frequency, buffer_size)
    dwf = _dwf()
    if max_wait_s <= 0:
        raise AD2Error("max_wait_s must be positive")
    masks = trigger_masks(dd, trigger_pin, trigger_edge, trigger_rising, trigger_falling, trigger_high, trigger_low)
    trigger_index = set_trigger(dd, n, trigger_source, masks, pretrigger_samples, trigger_timeout_s,
                                trigger_count, trigger_length_min_s, trigger_length_max_s, trigger_slope)
    if dwf.FDwfDigitalInConfigure(dd.handle, ctypes.c_bool(True), ctypes.c_bool(True)) == 0:
        device.check_error()
    _wait_done(dd, max_wait_s, f"trigger_source='{trigger_source}', masks low/high/rise/fall={masks}")
    buffer = (ctypes.c_uint16 * n)()
    if dwf.FDwfDigitalInStatusData(dd.handle, buffer, ctypes.c_int(2 * n)) == 0:
        device.check_error()
    return fs, list(buffer), trigger_index


def record(session, sampling_frequency=1e6, duration_s=0.1, trigger_pin=None, trigger_edge="rising",
           trigger_timeout_s=1.0, max_wait_s=None, trigger_rising=None, trigger_falling=None,
           trigger_high=None, trigger_low=None, trigger_source="logic"):
    """Stream a record longer than the buffer. Returns (fs, samples, lost, corrupted)."""
    if duration_s <= 0:
        raise AD2Error("duration_s must be positive")
    dd, fs, _ = configure(session, sampling_frequency, 0)
    dwf = _dwf()
    total = int(round(duration_s * fs))
    if total < 1:
        raise AD2Error("duration_s is shorter than one sample period")
    if total > MAX_RECORD_SAMPLES:
        raise AD2Error(f"duration_s * sampling_frequency = {total} samples exceeds the {MAX_RECORD_SAMPLES} limit")
    if max_wait_s is None:
        max_wait_s = duration_s + 5.0
    masks = trigger_masks(dd, trigger_pin, trigger_edge, trigger_rising, trigger_falling, trigger_high, trigger_low)
    set_trigger(dd, total, trigger_source, masks, 0, trigger_timeout_s)
    buffer = (ctypes.c_uint16 * total)()
    collected = lost = corrupted = 0
    session.logic_settings = None
    try:
        if dwf.FDwfDigitalInAcquisitionModeSet(dd.handle, constants.acqmodeRecord) == 0:
            device.check_error()
        if dwf.FDwfDigitalInTriggerPositionSet(dd.handle, ctypes.c_int(total)) == 0:
            device.check_error()
        if dwf.FDwfDigitalInConfigure(dd.handle, ctypes.c_bool(True), ctypes.c_bool(True)) == 0:
            device.check_error()
        status = ctypes.c_byte()
        available = ctypes.c_int()
        lost_now = ctypes.c_int()
        corrupt_now = ctypes.c_int()
        deadline = time.monotonic() + max_wait_s
        while collected < total:
            if dwf.FDwfDigitalInStatus(dd.handle, ctypes.c_bool(True), ctypes.byref(status)) == 0:
                device.check_error()
            if collected == 0 and status.value in (constants.DwfStateConfig.value, constants.DwfStatePrefill.value,
                                                   constants.DwfStateArmed.value):
                if time.monotonic() > deadline:
                    raise AD2Error(f"record did not start within {max_wait_s} s (trigger_source='{trigger_source}')")
                time.sleep(0.001)
                continue
            if dwf.FDwfDigitalInStatusRecord(dd.handle, ctypes.byref(available), ctypes.byref(lost_now),
                                             ctypes.byref(corrupt_now)) == 0:
                device.check_error()
            collected += lost_now.value
            lost += lost_now.value
            corrupted += corrupt_now.value
            take = min(available.value, total - collected)
            if take > 0:
                chunk = (ctypes.c_uint16 * take).from_buffer(buffer, 2 * collected)
                if dwf.FDwfDigitalInStatusData(dd.handle, chunk, ctypes.c_int(2 * take)) == 0:
                    device.check_error()
                collected += take
            elif status.value == constants.DwfStateDone.value:
                break
            if time.monotonic() > deadline:
                raise AD2Error(f"record did not finish within {max_wait_s} s ({collected} of {total} samples)")
            if take == 0:
                time.sleep(0.001)
    finally:
        dwf.FDwfDigitalInConfigure(dd.handle, ctypes.c_bool(False), ctypes.c_bool(False))
        dwf.FDwfDigitalInAcquisitionModeSet(dd.handle, constants.acqmodeSingle)
    return fs, list(buffer)[:collected], lost, corrupted


def counter_supported(dd) -> bool:
    """The AD2 firmware has no counter (max gate time 0); newer devices do."""
    count_max, sec_max = ctypes.c_double(), ctypes.c_double()
    if _dwf().FDwfDigitalInCounterInfo(dd.handle, ctypes.byref(count_max), ctypes.byref(sec_max)) == 0:
        device.check_error()
    return sec_max.value > 0


def _rising_edges(samples, pin):
    bits = [(v >> pin) & 1 for v in samples]
    return [i for i in range(1, len(bits)) if bits[i] and not bits[i - 1]]


def _software_frequency(session, pin, gate_time_s) -> dict:
    fs, samples, _ = capture(session, 100e6, 0)
    edges = _rising_edges(samples, pin)
    if len(edges) >= 20:
        return {"pin": pin, "frequency_hz": (len(edges) - 1) * fs / (edges[-1] - edges[0]),
                "edges_counted": len(edges), "gate_time_s": len(samples) / fs, "method": "software_capture"}
    fs_slow = min(1e6, MAX_RECORD_SAMPLES / gate_time_s)
    fs, samples, lost, _ = record(session, fs_slow, gate_time_s)
    edges = _rising_edges(samples, pin)
    if len(edges) < 2:
        raise AD2Error(f"fewer than two rising edges on DIO{pin} in {gate_time_s} s; check the pin toggles "
                       "or lengthen gate_time_s")
    return {"pin": pin, "frequency_hz": (len(edges) - 1) * fs / (edges[-1] - edges[0]),
            "edges_counted": len(edges), "gate_time_s": gate_time_s, "method": "software_record",
            "samples_lost": lost}


def count_frequency(session, pin=0, gate_time_s=0.1) -> dict:
    """Frequency counter on a DIO pin: hardware where available, else a software count."""
    if not 0.001 <= gate_time_s <= 10:
        raise AD2Error("gate_time_s must be between 0.001 and 10 seconds")
    dd = session.ensure()
    (pin,) = check_pins(dd, [pin], "pin")
    if not counter_supported(dd):
        return _software_frequency(session, pin, gate_time_s)
    dwf = _dwf()
    h = dd.handle
    session.logic_settings = None
    if dwf.FDwfDigitalInTriggerSourceSet(h, constants.trigsrcDetectorDigitalIn) == 0:
        device.check_error()
    if dwf.FDwfDigitalInTriggerSet(h, ctypes.c_int(0), ctypes.c_int(0), ctypes.c_int(1 << pin), ctypes.c_int(0)) == 0:
        device.check_error()
    if dwf.FDwfDigitalInCounterSet(h, ctypes.c_double(gate_time_s)) == 0:
        device.check_error()
    if dwf.FDwfDigitalInConfigure(h, ctypes.c_bool(True), ctypes.c_bool(True)) == 0:
        device.check_error()
    count, frequency, tick = ctypes.c_double(), ctypes.c_double(), ctypes.c_int()
    try:
        dwf.FDwfDigitalInStatus(h, ctypes.c_bool(False), None)
        dwf.FDwfDigitalInCounterStatus(h, ctypes.byref(count), ctypes.byref(frequency), ctypes.byref(tick))
        first_tick = tick.value
        deadline = time.monotonic() + 2 * gate_time_s + 2.0
        while True:
            time.sleep(min(0.01, gate_time_s / 4))
            if dwf.FDwfDigitalInStatus(h, ctypes.c_bool(False), None) == 0:
                device.check_error()
            dwf.FDwfDigitalInCounterStatus(h, ctypes.byref(count), ctypes.byref(frequency), ctypes.byref(tick))
            if tick.value != first_tick:
                break
            if time.monotonic() > deadline:
                raise AD2Error("the counter produced no measurement; check the pin toggles")
    finally:
        dwf.FDwfDigitalInCounterSet(h, ctypes.c_double(0))
        dwf.FDwfDigitalInConfigure(h, ctypes.c_bool(False), ctypes.c_bool(False))
    return {"pin": pin, "frequency_hz": frequency.value, "edges_counted": count.value, "gate_time_s": gate_time_s,
            "method": "hardware"}


def summarize_pin(samples, pin: int, fs: float, max_edges: int) -> dict:
    """Levels, transition count, duty, frequency estimate and the first `max_edges` edges of one pin."""
    bits = [(v >> pin) & 1 for v in samples]
    n = len(bits)
    edges = [i for i in range(1, n) if bits[i] != bits[i - 1]]
    rising = [i for i in edges if bits[i]]
    frequency = None
    if len(rising) >= 2:
        frequency = (len(rising) - 1) * fs / (rising[-1] - rising[0])
    return {
        "initial_level": bits[0] if n else None,
        "final_level": bits[-1] if n else None,
        "transitions": len(edges),
        "high_fraction": round(sum(bits) / n, 4) if n else None,
        "frequency_hz_estimate": frequency,
        "edges": [[i / fs, bits[i]] for i in edges[:max_edges]],
        "edges_truncated": len(edges) > max_edges,
    }


def write_logic_csv(path, fs: float, samples, count: int) -> str:
    """CSV with time, the raw 16-bit value and one column per pin."""
    from .export import write_rows
    header = ["time_s", "value_hex"] + [f"dio{p}" for p in range(count)]
    rows = ([f"{i / fs:.9g}", f"0x{v:04x}"] + [str((v >> p) & 1) for p in range(count)]
            for i, v in enumerate(samples))
    return write_rows(path, "logic", header, rows)
