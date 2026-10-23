import math

from ad2 import analysis, export


def sine(freq, fs, n, amplitude=1.0, offset=0.0):
    return [offset + amplitude * math.sin(2 * math.pi * freq * i / fs) for i in range(n)]


def test_summarize_sine():
    fs = 1e6
    stats = analysis.summarize(sine(1e3, fs, 8000, amplitude=2.0, offset=0.5), fs)  # 8 whole periods
    assert stats["samples"] == 8000
    assert abs(stats["peak_to_peak_v"] - 4.0) < 0.01
    assert abs(stats["mean_v"] - 0.5) < 0.01
    assert abs(stats["ac_rms_v"] - 2.0 / math.sqrt(2)) < 0.01
    assert abs(stats["frequency_hz_estimate"] - 1000) < 5


def test_frequency_none_for_dc():
    stats = analysis.summarize([3.3] * 1000, 1e6)
    assert stats["frequency_hz_estimate"] is None
    assert stats["mean_v"] == 3.3


def test_frequency_none_for_millivolt_noise():
    import random
    rng = random.Random(1)
    noise = [0.004 * rng.uniform(-1, 1) for _ in range(8192)]
    assert analysis.estimate_frequency(noise, 1e6) is None


def test_frequency_ignores_noise_below_hysteresis():
    fs = 1e6
    samples = [math.sin(2 * math.pi * 100 * i / fs) + 0.02 * math.sin(2 * math.pi * 50e3 * i / fs)
               for i in range(50000)]  # 5 periods of the 100 Hz fundamental
    assert abs(analysis.estimate_frequency(samples, fs) - 100) < 1


def test_decimate_keeps_first_and_bounds_length():
    samples = list(range(1000))
    out = analysis.decimate(samples, 100)
    assert out[0] == 0
    assert len(out) <= 100
    assert analysis.decimate(samples, 0) == samples
    assert analysis.decimate([1, 2, 3], 10) == [1, 2, 3]


def test_write_capture_csv(tmp_path, monkeypatch):
    monkeypatch.setenv("AD2_CAPTURE_DIR", str(tmp_path))
    path = export.write_capture_csv(None, 1e6, {1: [0.0, 1.0, 2.0], 2: [3.0, 4.0, 5.0]})
    assert path.startswith(str(tmp_path))
    lines = open(path).read().splitlines()
    assert lines[0] == "time_s,ch1_v,ch2_v"
    assert lines[2] == "1e-06,1,4"
    explicit = export.write_capture_csv(str(tmp_path / "sub" / "x.csv"), 1e6, {1: [0.5]})
    assert explicit.endswith("sub/x.csv")
