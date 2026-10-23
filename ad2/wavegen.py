"""Arbitrary waveform generator (W1 / W2): standard shapes, custom sample data, frequency sweeps."""

import ctypes

from WF_SDK import device, wavegen as wf_wavegen
import dwfconstants as constants

from .errors import AD2Error

FUNCTIONS = {
    "custom": wf_wavegen.function.custom,
    "sine": wf_wavegen.function.sine,
    "square": wf_wavegen.function.square,
    "triangle": wf_wavegen.function.triangle,
    "ramp_up": wf_wavegen.function.ramp_up,
    "ramp_down": wf_wavegen.function.ramp_down,
    "dc": wf_wavegen.function.dc,
    "noise": wf_wavegen.function.noise,
    "pulse": wf_wavegen.function.pulse,
    "trapezium": wf_wavegen.function.trapezium,
    "sine_power": wf_wavegen.function.sine_power,
}


def _channel_count(dd) -> int:
    return dd.analog.output.channel_count or 2


def _max_amplitude(dd, channel) -> float:
    rows = dd.analog.output.max_amplitude
    try:
        return float(rows[channel - 1][0])
    except (IndexError, TypeError):
        return 5.0


def _max_samples(dd, channel) -> int:
    rows = dd.analog.output.max_buffer_size
    try:
        return int(rows[channel - 1][0]) or 4096
    except (IndexError, TypeError):
        return 4096


def _check_channel(dd, channel) -> None:
    count = _channel_count(dd)
    if isinstance(channel, bool) or not isinstance(channel, int) or not 1 <= channel <= count:
        raise AD2Error(f"channel must be between 1 and {count}")


def _check_level(dd, channel, amplitude, offset) -> None:
    if amplitude < 0:
        raise AD2Error("amplitude must be zero or positive; use offset for polarity")
    limit = _max_amplitude(dd, channel)
    if amplitude + abs(offset) > limit + 1e-9:
        raise AD2Error(f"amplitude + |offset| must not exceed {limit} V (the output swings offset ± amplitude)")


def _node(dd, channel, node, enable: bool) -> None:
    if device.dwf.FDwfAnalogOutNodeEnableSet(dd.handle, ctypes.c_int(channel - 1), node,
                                             ctypes.c_int(1 if enable else 0)) == 0:
        device.check_error()


def parse_samples(data, max_samples: int) -> list[float]:
    if data is None:
        raise AD2Error("data is required for function='custom': a list of samples between -1 and 1")
    samples = []
    for item in data:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise AD2Error(f"data must contain numbers between -1 and 1, got {item!r}")
        if not -1 <= item <= 1:
            raise AD2Error("data samples must be between -1 and 1 (they are scaled by amplitude and shifted by offset)")
        samples.append(float(item))
    if not samples:
        raise AD2Error("data must contain at least one sample")
    if len(samples) > max_samples:
        raise AD2Error(f"data has {len(samples)} samples; this channel holds at most {max_samples}")
    return samples


def generate(session, channel, function, frequency, amplitude, offset=0.0, symmetry=50.0,
             run_time_s=0.0, wait_s=0.0, repeat=0, data=None, phase_deg=0.0, start_all_channels=False) -> dict:
    dd = session.ensure()
    _check_channel(dd, channel)
    key = str(function).lower()
    if key not in FUNCTIONS:
        raise AD2Error(f"function must be one of: {', '.join(FUNCTIONS)}")
    if key != "dc" and frequency <= 0:
        raise AD2Error("frequency must be positive")
    _check_level(dd, channel, amplitude, offset)
    if not 0 <= symmetry <= 100:
        raise AD2Error("symmetry must be a percentage between 0 and 100")
    if run_time_s < 0 or wait_s < 0 or repeat < 0:
        raise AD2Error("run_time_s, wait_s and repeat must be zero or positive")
    samples = []
    if key == "custom":
        samples = parse_samples(data, _max_samples(dd, channel))
    elif data:
        raise AD2Error("data is only used with function='custom'")
    # Leave no modulation behind from an earlier sweep on this channel.
    _node(dd, channel, constants.AnalogOutNodeFM, False)
    _node(dd, channel, constants.AnalogOutNodeAM, False)
    if device.dwf.FDwfAnalogOutNodePhaseSet(dd.handle, ctypes.c_int(channel - 1), constants.AnalogOutNodeCarrier,
                                            ctypes.c_double(phase_deg)) == 0:
        device.check_error()
    wf_wavegen.generate(dd, channel, FUNCTIONS[key], offset, frequency if key != "dc" else 1e3,
                        amplitude, symmetry, wait_s, run_time_s, repeat, samples)
    session.wavegen_running.add(channel)
    if start_all_channels:
        # Restart every configured channel together so their phases line up.
        if device.dwf.FDwfAnalogOutConfigure(dd.handle, ctypes.c_int(-1), ctypes.c_int(1)) == 0:
            device.check_error()
    result = {
        "channel": channel,
        "function": key,
        "frequency_hz": frequency,
        "amplitude_v": amplitude,
        "offset_v": offset,
        "symmetry_percent": symmetry,
        "phase_deg": phase_deg,
        "run_time_s": run_time_s,
        "wait_s": wait_s,
        "repeat": repeat,
        "running": True,
        "running_channels": sorted(session.wavegen_running),
    }
    if samples:
        result["custom_samples"] = len(samples)
        result["sample_rate_hz"] = frequency * len(samples)
    return result


def sweep(session, channel, start_frequency, stop_frequency, sweep_time_s, amplitude, offset=0.0,
          function="sine", repeat=0) -> dict:
    """Linear frequency sweep using the FM modulation node: carrier at the mid frequency, deviation
    ramped over sweep_time_s. repeat=0 sweeps forever; otherwise the output stops after `repeat` sweeps."""
    dd = session.ensure()
    _check_channel(dd, channel)
    key = str(function).lower()
    if key not in FUNCTIONS or key in ("custom", "dc", "noise"):
        raise AD2Error("function must be a periodic shape: sine, square, triangle, ramp_up, ramp_down, pulse, trapezium, sine_power")
    if start_frequency <= 0 or stop_frequency <= 0:
        raise AD2Error("start_frequency and stop_frequency must be positive")
    if start_frequency == stop_frequency:
        raise AD2Error("start_frequency and stop_frequency must differ (use generate_waveform for a fixed tone)")
    if not 1e-3 <= sweep_time_s <= 60:
        raise AD2Error("sweep_time_s must be between 0.001 and 60 seconds")
    if repeat < 0:
        raise AD2Error("repeat must be zero (forever) or positive")
    _check_level(dd, channel, amplitude, offset)
    carrier = (start_frequency + stop_frequency) / 2
    deviation_percent = abs(stop_frequency - start_frequency) / (start_frequency + stop_frequency) * 100
    ramp = wf_wavegen.function.ramp_up if stop_frequency > start_frequency else wf_wavegen.function.ramp_down
    dwf = device.dwf
    handle, ch = dd.handle, ctypes.c_int(channel - 1)
    carrier_node, fm_node = constants.AnalogOutNodeCarrier, constants.AnalogOutNodeFM
    _node(dd, channel, constants.AnalogOutNodeAM, False)
    steps = (
        (dwf.FDwfAnalogOutNodeEnableSet, carrier_node, ctypes.c_int(1)),
        (dwf.FDwfAnalogOutNodeFunctionSet, carrier_node, FUNCTIONS[key]),
        (dwf.FDwfAnalogOutNodeFrequencySet, carrier_node, ctypes.c_double(carrier)),
        (dwf.FDwfAnalogOutNodeAmplitudeSet, carrier_node, ctypes.c_double(amplitude)),
        (dwf.FDwfAnalogOutNodeOffsetSet, carrier_node, ctypes.c_double(offset)),
        (dwf.FDwfAnalogOutNodeSymmetrySet, carrier_node, ctypes.c_double(50)),
        (dwf.FDwfAnalogOutNodePhaseSet, carrier_node, ctypes.c_double(0)),
        (dwf.FDwfAnalogOutNodeEnableSet, fm_node, ctypes.c_int(1)),
        (dwf.FDwfAnalogOutNodeFunctionSet, fm_node, ramp),
        (dwf.FDwfAnalogOutNodeFrequencySet, fm_node, ctypes.c_double(1 / sweep_time_s)),
        (dwf.FDwfAnalogOutNodeAmplitudeSet, fm_node, ctypes.c_double(deviation_percent)),
        (dwf.FDwfAnalogOutNodeOffsetSet, fm_node, ctypes.c_double(0)),
        (dwf.FDwfAnalogOutNodeSymmetrySet, fm_node, ctypes.c_double(100)),
        (dwf.FDwfAnalogOutNodePhaseSet, fm_node, ctypes.c_double(0)),
    )
    for fn, node, value in steps:
        if fn(handle, ch, node, value) == 0:
            device.check_error()
    if dwf.FDwfAnalogOutRunSet(handle, ch, ctypes.c_double(sweep_time_s if repeat else 0)) == 0:
        device.check_error()
    if dwf.FDwfAnalogOutWaitSet(handle, ch, ctypes.c_double(0)) == 0:
        device.check_error()
    if dwf.FDwfAnalogOutRepeatSet(handle, ch, ctypes.c_int(repeat)) == 0:
        device.check_error()
    if dwf.FDwfAnalogOutConfigure(handle, ch, ctypes.c_int(1)) == 0:
        device.check_error()
    session.wavegen_running.add(channel)
    return {
        "channel": channel,
        "function": key,
        "start_frequency_hz": start_frequency,
        "stop_frequency_hz": stop_frequency,
        "sweep_time_s": sweep_time_s,
        "carrier_frequency_hz": carrier,
        "deviation_percent": deviation_percent,
        "amplitude_v": amplitude,
        "offset_v": offset,
        "repeat": repeat,
        "running": True,
        "running_channels": sorted(session.wavegen_running),
    }


def stop(session, channel: int = 0) -> dict:
    dd = session.ensure()
    count = _channel_count(dd)
    if not 0 <= channel <= count:
        raise AD2Error(f"channel must be 0 (all) or between 1 and {count}")
    wf_wavegen.close(dd, channel)
    if channel == 0:
        stopped = sorted(session.wavegen_running) or list(range(1, count + 1))
        session.wavegen_running.clear()
    else:
        stopped = [channel]
        session.wavegen_running.discard(channel)
    return {"stopped_channels": stopped, "running_channels": sorted(session.wavegen_running)}
