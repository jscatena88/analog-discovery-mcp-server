"""Pure-Python summaries of captured samples (no numpy dependency)."""

import math

# Below this peak-to-peak swing a signal is treated as flat: a floating or quiet scope
# input shows a few millivolts of noise, and counting its crossings gives nonsense.
FREQUENCY_NOISE_FLOOR_VPP = 0.02


def round_sig(value, digits=6):
    if value is None or value == 0 or not math.isfinite(value):
        return value
    return round(value, digits - 1 - int(math.floor(math.log10(abs(value)))))


def estimate_frequency(samples, sampling_frequency) -> float | None:
    """Frequency from rising mean-crossings with hysteresis; None if the signal is flat
    (under FREQUENCY_NOISE_FLOOR_VPP) or completes fewer than two cycles."""
    n = len(samples)
    if n < 4:
        return None
    mean = sum(samples) / n
    vpp = max(samples) - min(samples)
    if vpp < FREQUENCY_NOISE_FLOOR_VPP:
        return None
    hysteresis = 0.05 * vpp
    above = None  # unknown until the signal clearly leaves the hysteresis band
    crossings = []
    for i, v in enumerate(samples):
        d = v - mean
        if above is None:
            if abs(d) > hysteresis:
                above = d > 0
        elif not above and d > hysteresis:
            above = True
            crossings.append(i)
        elif above and d < -hysteresis:
            above = False
    if len(crossings) < 2:
        return None
    periods = len(crossings) - 1
    return periods * sampling_frequency / (crossings[-1] - crossings[0])


def summarize(samples, sampling_frequency) -> dict:
    n = len(samples)
    if n == 0:
        return {"samples": 0}
    mean = sum(samples) / n
    rms = math.sqrt(sum(v * v for v in samples) / n)
    ac_rms = math.sqrt(sum((v - mean) ** 2 for v in samples) / n)
    return {
        "samples": n,
        "min_v": round_sig(min(samples)),
        "max_v": round_sig(max(samples)),
        "mean_v": round_sig(mean),
        "peak_to_peak_v": round_sig(max(samples) - min(samples)),
        "rms_v": round_sig(rms),
        "ac_rms_v": round_sig(ac_rms),
        "frequency_hz_estimate": round_sig(estimate_frequency(samples, sampling_frequency)),
    }


def decimate(samples, points: int) -> list:
    """Evenly strided subset of at most `points` samples (first sample always kept)."""
    n = len(samples)
    if points <= 0 or n <= points:
        return list(samples)
    stride = math.ceil(n / points)
    return list(samples[::stride])


def time_axis(n: int, sampling_frequency: float, points: int) -> list:
    return decimate([i / sampling_frequency for i in range(n)], points)
