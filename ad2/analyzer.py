"""Network (Bode) and impedance analyzer built on the WaveForms AnalogImpedance engine.

Both drive W1 and measure scope channel 1 (reference, across the stimulus) and channel 2.
Wiring for a transfer function: W1 and 1+ on the DUT input, 2+ on the DUT output, 1-/2- to GND.
Wiring for impedance (mode series_reference): W1 and 1+ on the DUT, 2+ at the DUT/reference
resistor junction, the resistor's other end to GND.
"""

import ctypes
import math
import time

from WF_SDK import device
import dwfconstants as constants

from .errors import AD2Error
from .export import write_rows

MODES = {"series_reference": 0, "shunt_reference": 1}
MEASURES = {
    "impedance_ohm": constants.DwfAnalogImpedanceImpedance,
    "phase_deg": constants.DwfAnalogImpedanceImpedancePhase,
    "resistance_ohm": constants.DwfAnalogImpedanceResistance,
    "reactance_ohm": constants.DwfAnalogImpedanceReactance,
    "series_capacitance_f": constants.DwfAnalogImpedanceSeriesCapacitance,
    "parallel_capacitance_f": constants.DwfAnalogImpedanceParallelCapacitance,
    "series_inductance_h": constants.DwfAnalogImpedanceSeriesInductance,
    "parallel_inductance_h": constants.DwfAnalogImpedanceParallelInductance,
    "dissipation_factor": constants.DwfAnalogImpedanceDissipation,
    "quality_factor": constants.DwfAnalogImpedanceQuality,
}
WARNINGS = {1: "clipped low", 2: "clipped high", 3: "clipped"}


def _dwf():
    return device.dwf


def frequency_points(start, stop, steps, log_scale) -> list[float]:
    if start <= 0 or stop <= 0:
        raise AD2Error("start and stop frequencies must be positive")
    if steps < 1 or steps > 1000:
        raise AD2Error("steps must be between 1 and 1000")
    if steps == 1 or start == stop:
        return [float(start)]
    if start >= stop:
        raise AD2Error("start must be lower than stop")
    if log_scale:
        ratio = (stop / start) ** (1 / (steps - 1))
        return [start * ratio ** i for i in range(steps)]
    step = (stop - start) / (steps - 1)
    return [start + step * i for i in range(steps)]


def _validate(dd, frequencies, amplitude, offset, min_periods, max_wait_s):
    max_frequency = None
    rows = dd.analog.output.max_frequency
    if rows and rows[0]:
        max_frequency = rows[0][0]
    if max_frequency and max(frequencies) > max_frequency:
        raise AD2Error(f"stop frequency exceeds the generator's {max_frequency:g} Hz limit")
    if not 0 < amplitude <= 5:
        raise AD2Error("amplitude must be between 0 and 5 V")
    if abs(offset) + amplitude > 5:
        raise AD2Error("amplitude + |offset| must not exceed 5 V")
    if not 1 <= min_periods <= 1000:
        raise AD2Error("min_periods must be between 1 and 1000")
    if max_wait_s <= 0:
        raise AD2Error("max_wait_s must be positive")


def _sweep(session, frequencies, amplitude, offset, reference_ohm, mode, min_periods, max_wait_s, collect):
    """Run the engine over `frequencies`, calling collect(frequency) when each point is done."""
    dd = session.ensure()
    dwf = _dwf()
    _validate(dd, frequencies, amplitude, offset, min_periods, max_wait_s)
    if reference_ohm <= 0:
        raise AD2Error("reference_ohm must be positive")
    if mode not in MODES:
        raise AD2Error(f"mode must be one of: {', '.join(MODES)}")
    handle = dd.handle
    # The engine needs the scope channels enabled, and "dynamic" auto-configure (3) so that
    # frequency changes take effect while it is running; without it every point measures
    # with a stale frequency and the results are nonsense.
    from . import scope as _scope
    _scope.configure(session, 20e6, 0, 5.0, 0.0)
    if dwf.FDwfDeviceAutoConfigureSet(handle, ctypes.c_int(3)) == 0:
        device.check_error()
    if dwf.FDwfAnalogImpedanceReset(handle) == 0:
        device.check_error()
    for call in ((dwf.FDwfAnalogImpedanceModeSet, ctypes.c_int(MODES[mode])),
                 (dwf.FDwfAnalogImpedanceReferenceSet, ctypes.c_double(reference_ohm)),
                 (dwf.FDwfAnalogImpedanceAmplitudeSet, ctypes.c_double(amplitude)),
                 (dwf.FDwfAnalogImpedanceOffsetSet, ctypes.c_double(offset)),
                 (dwf.FDwfAnalogImpedancePeriodSet, ctypes.c_int(min_periods)),
                 (dwf.FDwfAnalogImpedanceFrequencySet, ctypes.c_double(frequencies[0]))):
        if call[0](handle, call[1]) == 0:
            device.check_error()
    # The engine takes over W1 and both scope channels.
    session.scope_settings = None
    session.wavegen_running.discard(1)
    if dwf.FDwfAnalogImpedanceConfigure(handle, ctypes.c_int(1)) == 0:
        device.check_error()
    status = ctypes.c_byte()
    try:
        for frequency in frequencies:
            if dwf.FDwfAnalogImpedanceFrequencySet(handle, ctypes.c_double(frequency)) == 0:
                device.check_error()
            dwf.FDwfAnalogImpedanceStatus(handle, None)  # drop the measurement made before the change
            deadline = time.monotonic() + max_wait_s + min_periods / frequency
            while True:
                if dwf.FDwfAnalogImpedanceStatus(handle, ctypes.byref(status)) == 0:
                    device.check_error()
                if status.value == constants.DwfStateDone.value:
                    break
                if time.monotonic() > deadline:
                    raise AD2Error(f"analyzer did not finish the {frequency:g} Hz point within the time limit")
                time.sleep(0.001)
            collect(frequency)
    finally:
        dwf.FDwfAnalogImpedanceConfigure(handle, ctypes.c_int(0))
        dwf.FDwfDeviceAutoConfigureSet(handle, ctypes.c_int(1))
        session.scope_settings = None


def _warnings(dd) -> list[str]:
    out = []
    flag = ctypes.c_int()
    for channel in (0, 1):
        _dwf().FDwfAnalogImpedanceStatusWarning(dd.handle, ctypes.c_int(channel), ctypes.byref(flag))
        if flag.value:
            out.append(f"ch{channel + 1} {WARNINGS.get(flag.value, flag.value)}")
    return out


def bode(session, start, stop, steps, amplitude, offset, log_scale, min_periods, max_wait_s,
         csv_path=None, save_csv=True) -> dict:
    """Gain (dB) and phase (degrees) of channel 2 relative to channel 1 across a frequency sweep."""
    frequencies = frequency_points(start, stop, steps, log_scale)
    dd = session.ensure()
    dwf = _dwf()
    points = []

    def collect(frequency):
        gain_ref = ctypes.c_double()
        gain = ctypes.c_double()
        phase = ctypes.c_double()
        if dwf.FDwfAnalogImpedanceStatusInput(dd.handle, ctypes.c_int(0), ctypes.byref(gain_ref), None) == 0:
            device.check_error()
        if dwf.FDwfAnalogImpedanceStatusInput(dd.handle, ctypes.c_int(1), ctypes.byref(gain), ctypes.byref(phase)) == 0:
            device.check_error()
        # The engine reports C1/C2 and the phase of C1 relative to C2; invert both for the usual H = C2/C1.
        magnitude = 1 / gain.value if gain.value else float("inf")
        points.append({
            "frequency_hz": round(frequency, 6),
            "gain_db": round(20 * math.log10(magnitude), 3) if magnitude > 0 and math.isfinite(magnitude) else None,
            "phase_deg": round(-math.degrees(phase.value), 2),
            "input_level": round(gain_ref.value, 4),
            "warnings": _warnings(dd),
        })

    _sweep(session, frequencies, amplitude, offset, 1e3, "series_reference", min_periods, max_wait_s, collect)
    result = {"points": points, "steps": len(points), "amplitude_v": amplitude, "offset_v": offset,
              "summary": summarize_bode(points)}
    if save_csv:
        rows = ([p["frequency_hz"], p["gain_db"], p["phase_deg"], p["input_level"]] for p in points)
        result["csv_path"] = write_rows(csv_path, "bode", ["frequency_hz", "gain_db", "phase_deg", "input_level"], rows)
    return result


def summarize_bode(points) -> dict:
    valid = [p for p in points if p["gain_db"] is not None]
    if not valid:
        return {}
    peak = max(valid, key=lambda p: p["gain_db"])
    low, high = valid[0], valid[-1]
    summary = {"max_gain_db": peak["gain_db"], "max_gain_at_hz": peak["frequency_hz"],
               "gain_at_start_db": low["gain_db"], "gain_at_stop_db": high["gain_db"]}
    threshold = peak["gain_db"] - 3
    cutoffs = []
    for a, b in zip(valid, valid[1:]):
        if (a["gain_db"] - threshold) * (b["gain_db"] - threshold) < 0:
            # interpolate the crossing on a log-frequency axis
            fa, fb = math.log(a["frequency_hz"]), math.log(b["frequency_hz"])
            t = (threshold - a["gain_db"]) / (b["gain_db"] - a["gain_db"])
            cutoffs.append(round(math.exp(fa + t * (fb - fa)), 3))
    summary["cutoff_hz_minus3db"] = cutoffs
    if len(valid) >= 3:
        flat = all(abs(p["gain_db"] - peak["gain_db"]) < 1 for p in valid)
        if flat:
            kind = "flat"
        elif low["gain_db"] < threshold <= high["gain_db"] + 1 and peak is not low:
            kind = "high_pass"
        elif high["gain_db"] < threshold <= low["gain_db"] + 1 and peak is not high:
            kind = "low_pass"
        elif low["gain_db"] < threshold and high["gain_db"] < threshold:
            kind = "band_pass"
        elif peak["gain_db"] - min(p["gain_db"] for p in valid) > 3 and peak in (low, high):
            kind = "band_stop" if min(p["gain_db"] for p in valid) < threshold else "flat"
        else:
            kind = "other"
        summary["response"] = kind
    return summary


def impedance(session, start, stop, steps, reference_ohm, mode, amplitude, offset, log_scale, min_periods,
              max_wait_s, csv_path=None, save_csv=True) -> dict:
    frequencies = frequency_points(start, stop, steps, log_scale)
    dd = session.ensure()
    dwf = _dwf()
    points = []

    def collect(frequency):
        point = {"frequency_hz": round(frequency, 6)}
        value = ctypes.c_double()
        for key, const in MEASURES.items():
            if dwf.FDwfAnalogImpedanceStatusMeasure(dd.handle, const, ctypes.byref(value)) == 0:
                device.check_error()
            v = value.value
            if key == "phase_deg":
                v = math.degrees(v)
            point[key] = v if math.isfinite(v) else None
        point["warnings"] = _warnings(dd)
        points.append(point)

    _sweep(session, frequencies, amplitude, offset, reference_ohm, mode, min_periods, max_wait_s, collect)
    result = {"points": points, "steps": len(points), "reference_ohm": reference_ohm, "mode": mode,
              "amplitude_v": amplitude, "summary": summarize_impedance(points)}
    if save_csv:
        keys = ["frequency_hz"] + list(MEASURES)
        rows = ([p.get(k) for k in keys] for p in points)
        result["csv_path"] = write_rows(csv_path, "impedance", keys, rows)
    return result


def _median(values):
    values = sorted(v for v in values if v is not None and math.isfinite(v))
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def summarize_impedance(points) -> dict:
    if not points:
        return {}
    reactances = [p["reactance_ohm"] for p in points if p["reactance_ohm"] is not None]
    resistances = [p["resistance_ohm"] for p in points if p["resistance_ohm"] is not None]
    magnitudes = [p["impedance_ohm"] for p in points if p["impedance_ohm"] is not None]
    summary = {"median_resistance_ohm": _median(resistances), "min_impedance_ohm": min(magnitudes, default=None),
               "max_impedance_ohm": max(magnitudes, default=None)}
    capacitive = [p for p in points if p["reactance_ohm"] is not None and p["reactance_ohm"] < 0
                  and abs(p["reactance_ohm"]) > abs(p["resistance_ohm"] or 0)]
    inductive = [p for p in points if p["reactance_ohm"] is not None and p["reactance_ohm"] > 0
                 and p["reactance_ohm"] > abs(p["resistance_ohm"] or 0)]
    if len(capacitive) > len(inductive) and len(capacitive) >= max(1, len(points) // 3):
        summary["dut_type"] = "capacitive"
        summary["series_capacitance_f"] = _median([p["series_capacitance_f"] for p in capacitive])
    elif len(inductive) > len(capacitive) and len(inductive) >= max(1, len(points) // 3):
        summary["dut_type"] = "inductive"
        summary["series_inductance_h"] = _median([p["series_inductance_h"] for p in inductive])
    else:
        summary["dut_type"] = "resistive"
    return summary
