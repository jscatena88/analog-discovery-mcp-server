"""Arbitrary waveform generator (W1 / W2) using the standard function shapes."""

from WF_SDK import wavegen as wf_wavegen

from .errors import AD2Error

FUNCTIONS = {
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


def generate(session, channel, function, frequency, amplitude, offset=0.0, symmetry=50.0,
             run_time_s=0.0, wait_s=0.0, repeat=0) -> dict:
    dd = session.ensure()
    count = _channel_count(dd)
    if not 1 <= channel <= count:
        raise AD2Error(f"channel must be between 1 and {count}")
    key = str(function).lower()
    if key not in FUNCTIONS:
        raise AD2Error(f"function must be one of: {', '.join(FUNCTIONS)}")
    if key != "dc" and frequency <= 0:
        raise AD2Error("frequency must be positive")
    if amplitude < 0:
        raise AD2Error("amplitude must be zero or positive; use offset for polarity")
    limit = _max_amplitude(dd, channel)
    if amplitude + abs(offset) > limit + 1e-9:
        raise AD2Error(f"amplitude + |offset| must not exceed {limit} V (the output swings offset ± amplitude)")
    if not 0 <= symmetry <= 100:
        raise AD2Error("symmetry must be a percentage between 0 and 100")
    if run_time_s < 0 or wait_s < 0 or repeat < 0:
        raise AD2Error("run_time_s, wait_s and repeat must be zero or positive")
    wf_wavegen.generate(dd, channel, FUNCTIONS[key], offset, frequency if key != "dc" else 1e3,
                        amplitude, symmetry, wait_s, run_time_s, repeat)
    session.wavegen_running.add(channel)
    return {
        "channel": channel,
        "function": key,
        "frequency_hz": frequency,
        "amplitude_v": amplitude,
        "offset_v": offset,
        "symmetry_percent": symmetry,
        "run_time_s": run_time_s,
        "wait_s": wait_s,
        "repeat": repeat,
        "running": True,
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
