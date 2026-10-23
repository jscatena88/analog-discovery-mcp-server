"""FFT spectrum of a scope capture: peaks, THD, noise floor, decimated preview."""

import math

import numpy as np

from .errors import AD2Error
from .export import write_rows

WINDOWS = ("hann", "blackman", "flattop", "rectangular")


def _window(name: str, n: int) -> np.ndarray:
    if name == "hann":
        return np.hanning(n)
    if name == "blackman":
        return np.blackman(n)
    if name == "rectangular":
        return np.ones(n)
    if name == "flattop":
        k = np.arange(n)
        a = (0.21557895, 0.41663158, 0.277263158, 0.083578947, 0.006947368)
        return sum(((-1) ** i) * a[i] * np.cos(2 * math.pi * i * k / (n - 1)) for i in range(5))
    raise AD2Error(f"window must be one of: {', '.join(WINDOWS)}")


def compute(samples, sampling_frequency: float, window: str = "hann"):
    """Returns (frequencies, amplitude in volts peak per bin). Window-gain corrected."""
    x = np.asarray(samples, dtype=float)
    n = x.size
    if n < 16:
        raise AD2Error("need at least 16 samples for a spectrum")
    w = _window(window, n)
    spectrum = np.fft.rfft(x * w) / w.sum()
    amplitude = np.abs(spectrum) * 2
    amplitude[0] /= 2
    if n % 2 == 0:
        amplitude[-1] /= 2
    frequencies = np.fft.rfftfreq(n, 1 / sampling_frequency)
    return frequencies, amplitude


def to_dbv(amplitude: np.ndarray) -> np.ndarray:
    return 20 * np.log10(np.maximum(amplitude, 1e-12))


def find_peaks(frequencies, amplitude, max_peaks: int, min_dbv_above_floor: float = 12.0, skip_dc_bins: int = 2):
    """Local maxima at least `min_dbv_above_floor` dB above the median level, strongest first.
    Frequency is refined by parabolic interpolation on the log magnitude."""
    dbv = to_dbv(amplitude)
    floor = float(np.median(dbv[skip_dc_bins:]))
    threshold = floor + min_dbv_above_floor
    candidates = []
    for i in range(max(skip_dc_bins, 1), len(dbv) - 1):
        if dbv[i] > threshold and dbv[i] >= dbv[i - 1] and dbv[i] > dbv[i + 1]:
            candidates.append(i)
    candidates.sort(key=lambda i: dbv[i], reverse=True)
    bin_width = frequencies[1] - frequencies[0] if len(frequencies) > 1 else 0
    peaks = []
    for i in candidates:
        if any(abs(frequencies[i] - p["frequency_hz"]) < 3 * bin_width for p in peaks):
            continue
        y0, y1, y2 = dbv[i - 1], dbv[i], dbv[i + 1]
        denominator = y0 - 2 * y1 + y2
        delta = 0.5 * (y0 - y2) / denominator if denominator else 0.0
        delta = max(-0.5, min(0.5, delta))
        peaks.append({
            "frequency_hz": float(frequencies[i] + delta * bin_width),
            "amplitude_v": float(amplitude[i]),
            "dbv": float(dbv[i]),
            "bin": i,
        })
        if len(peaks) >= max_peaks:
            break
    return peaks, floor


def thd(frequencies, amplitude, fundamental_bin: int, harmonics: int = 10):
    """Total harmonic distortion using the bins nearest to each harmonic (±1 bin search)."""
    f0 = frequencies[fundamental_bin]
    if f0 <= 0:
        return None, []
    fundamental = amplitude[fundamental_bin]
    found = []
    power = 0.0
    bin_width = frequencies[1] - frequencies[0]
    for k in range(2, harmonics + 1):
        target = k * f0
        if target >= frequencies[-1]:
            break
        center = int(round(target / bin_width))
        lo, hi = max(1, center - 1), min(len(amplitude) - 1, center + 2)
        j = lo + int(np.argmax(amplitude[lo:hi]))
        power += float(amplitude[j]) ** 2
        found.append({"harmonic": k, "frequency_hz": float(frequencies[j]), "amplitude_v": float(amplitude[j]),
                      "dbc": float(20 * math.log10(max(amplitude[j], 1e-12) / fundamental))})
    ratio = math.sqrt(power) / fundamental if fundamental > 0 else None
    return ratio, found


def preview(frequencies, amplitude, points: int):
    """Max-hold bucketing so narrow peaks survive decimation."""
    dbv = to_dbv(amplitude)
    n = len(dbv)
    if points <= 0 or n <= points:
        return {"frequency_hz": [float(f) for f in frequencies], "dbv": [round(float(v), 2) for v in dbv]}
    edges = np.linspace(0, n, points + 1).astype(int)
    out_f, out_v = [], []
    for a, b in zip(edges, edges[1:]):
        if b <= a:
            continue
        j = a + int(np.argmax(dbv[a:b]))
        out_f.append(float(frequencies[j]))
        out_v.append(round(float(dbv[j]), 2))
    return {"frequency_hz": out_f, "dbv": out_v}


def analyze(samples, sampling_frequency, window="hann", max_peaks=10, preview_points=200,
            csv_path=None, save_csv=True) -> dict:
    if max_peaks < 1:
        raise AD2Error("max_peaks must be at least 1")
    frequencies, amplitude = compute(samples, sampling_frequency, window)
    peaks, floor = find_peaks(frequencies, amplitude, max_peaks)
    result = {
        "samples": len(samples),
        "sampling_frequency_hz": sampling_frequency,
        "resolution_hz": float(frequencies[1]) if len(frequencies) > 1 else None,
        "max_frequency_hz": float(frequencies[-1]),
        "window": window,
        "noise_floor_dbv": round(floor, 2),
        "dc_v": float(amplitude[0]),
        "peaks": [{k: v for k, v in p.items() if k != "bin"} for p in peaks],
        "preview": preview(frequencies, amplitude, preview_points),
    }
    if peaks:
        fundamental = peaks[0]
        ratio, harmonics = thd(frequencies, amplitude, fundamental["bin"])
        result["fundamental"] = {k: v for k, v in fundamental.items() if k != "bin"}
        result["thd_percent"] = round(ratio * 100, 4) if ratio is not None else None
        result["thd_db"] = round(20 * math.log10(ratio), 2) if ratio else None
        result["harmonics"] = harmonics
    if save_csv:
        dbv = to_dbv(amplitude)
        rows = ([f"{f:.6g}", f"{a:.6g}", f"{d:.3f}"] for f, a, d in zip(frequencies, amplitude, dbv))
        result["csv_path"] = write_rows(csv_path, "spectrum", ["frequency_hz", "amplitude_v", "dbv"], rows)
    return result
