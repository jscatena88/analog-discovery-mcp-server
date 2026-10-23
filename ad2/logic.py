"""Logic analyzer: single-shot capture of DIO 0-15 with optional edge trigger."""

import ctypes
import time

from WF_SDK import device
import dwfconstants as constants

from .errors import AD2Error
from .digital import pin_count, check_pins

TRIGGER_EDGES = ("rising", "falling", "either")


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
                       "connect_device(configuration=3) gives a 16384-sample logic buffer")
    key = (divider, buffer_size)
    if session.logic_settings != key:
        if dwf.FDwfDigitalInDividerSet(dd.handle, ctypes.c_int(divider)) == 0:
            device.check_error()
        if dwf.FDwfDigitalInSampleFormatSet(dd.handle, ctypes.c_int(16)) == 0:
            device.check_error()
        if dwf.FDwfDigitalInBufferSizeSet(dd.handle, ctypes.c_int(buffer_size)) == 0:
            device.check_error()
        session.logic_settings = key
    return dd, actual, buffer_size


def capture(session, sampling_frequency=100e6, buffer_size=0, trigger_pin=None, trigger_edge="rising",
            pretrigger_samples=0, trigger_timeout_s=1.0, max_wait_s=5.0):
    """One acquisition. Returns (actual_fs, samples as 16-bit ints, trigger index or None)."""
    dd, fs, n = configure(session, sampling_frequency, buffer_size)
    dwf = _dwf()
    if max_wait_s <= 0:
        raise AD2Error("max_wait_s must be positive")
    trigger_index = None
    if trigger_pin is None:
        if dwf.FDwfDigitalInTriggerSourceSet(dd.handle, constants.trigsrcNone) == 0:
            device.check_error()
        dwf.FDwfDigitalInTriggerPositionSet(dd.handle, ctypes.c_int(n))
        dwf.FDwfDigitalInTriggerPrefillSet(dd.handle, ctypes.c_int(0))
    else:
        (trigger_pin,) = check_pins(dd, [trigger_pin], "trigger_pin")
        if trigger_edge not in TRIGGER_EDGES:
            raise AD2Error(f"trigger_edge must be one of: {', '.join(TRIGGER_EDGES)}")
        if not 0 <= pretrigger_samples < n:
            raise AD2Error(f"pretrigger_samples must be between 0 and {n - 1}")
        if trigger_timeout_s < 0:
            raise AD2Error("trigger_timeout_s must be 0 (never auto-trigger) or positive")
        mask = 1 << trigger_pin
        rise = mask if trigger_edge in ("rising", "either") else 0
        fall = mask if trigger_edge in ("falling", "either") else 0
        if dwf.FDwfDigitalInTriggerSourceSet(dd.handle, constants.trigsrcDetectorDigitalIn) == 0:
            device.check_error()
        if dwf.FDwfDigitalInTriggerSet(dd.handle, ctypes.c_int(0), ctypes.c_int(0),
                                       ctypes.c_int(rise), ctypes.c_int(fall)) == 0:
            device.check_error()
        if dwf.FDwfDigitalInTriggerPositionSet(dd.handle, ctypes.c_int(n - pretrigger_samples)) == 0:
            device.check_error()
        if dwf.FDwfDigitalInTriggerPrefillSet(dd.handle, ctypes.c_int(pretrigger_samples)) == 0:
            device.check_error()
        if dwf.FDwfDigitalInTriggerAutoTimeoutSet(dd.handle, ctypes.c_double(trigger_timeout_s)) == 0:
            device.check_error()
        trigger_index = pretrigger_samples

    if dwf.FDwfDigitalInConfigure(dd.handle, ctypes.c_bool(False), ctypes.c_bool(True)) == 0:
        device.check_error()
    deadline = time.monotonic() + max_wait_s
    status = ctypes.c_byte()
    while True:
        if dwf.FDwfDigitalInStatus(dd.handle, ctypes.c_bool(True), ctypes.byref(status)) == 0:
            device.check_error()
        if status.value == constants.DwfStateDone.value:
            break
        if time.monotonic() > deadline:
            dwf.FDwfDigitalInConfigure(dd.handle, ctypes.c_bool(False), ctypes.c_bool(False))
            raise AD2Error(
                f"Logic capture did not complete within {max_wait_s} s (trigger_pin={trigger_pin}, "
                f"edge={trigger_edge}). Check the pin toggles, raise trigger_timeout_s so the analyzer "
                "auto-triggers, or omit trigger_pin."
            )
        time.sleep(0.001)
    buffer = (ctypes.c_uint16 * n)()
    if dwf.FDwfDigitalInStatusData(dd.handle, buffer, ctypes.c_int(2 * n)) == 0:
        device.check_error()
    return fs, list(buffer), trigger_index


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
