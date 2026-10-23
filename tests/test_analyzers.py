"""Spectrum, network/impedance analyzer and generator sweep/custom tools against the fake libdwf."""

import math
import os

import numpy as np
import pytest

from ad2 import analyzer, spectrum
from conftest import call_tool

FC = 1 / (2 * math.pi * 1e3 * 0.22e-6)   # 723 Hz corner of the fake RC


# --------------------------------------------------------------------------- spectrum maths

def test_spectrum_amplitude_of_clean_sine():
    fs, n = 100e3, 4096
    samples = 0.5 + 1.2 * np.sin(2 * np.pi * 5e3 * np.arange(n) / fs)    # 204.8 periods: not bin-centred
    for window, tolerance in (("flattop", 0.01), ("hann", 0.2), ("rectangular", 0.5)):
        freqs, amp = spectrum.compute(samples, fs, window)
        peaks, _ = spectrum.find_peaks(freqs, amp, 3)
        assert abs(peaks[0]["frequency_hz"] - 5e3) < 30, window
        assert abs(peaks[0]["amplitude_v"] - 1.2) < tolerance, window
    freqs, amp = spectrum.compute(samples, fs, "flattop")
    assert abs(amp[0] - 0.5) < 0.01                                       # DC bin


def test_spectrum_thd_of_square_wave():
    fs, n = 1e6, 8192
    samples = np.sign(np.sin(2 * np.pi * 10e3 * np.arange(n) / fs))
    out = spectrum.analyze(samples, fs, "hann", 5, 50, save_csv=False)
    assert abs(out["fundamental"]["frequency_hz"] - 10e3) < 200
    assert 40 < out["thd_percent"] < 50                                   # ideal square wave: 43 %
    assert out["harmonics"][0]["harmonic"] == 2 and out["harmonics"][1]["harmonic"] == 3
    assert out["harmonics"][1]["dbc"] > -12                               # 3rd harmonic at -9.5 dBc
    assert len(out["preview"]["dbv"]) <= 50


def test_capture_spectrum_tool(server):
    out = call_tool(server, "capture_spectrum", channel=1, sampling_frequency=1e6, window="flattop",
                    max_peaks=3, preview_points=40)
    assert out["channel"] == 1 and out["samples"] == 8192
    assert abs(out["fundamental"]["frequency_hz"] - 5e3) < out["resolution_hz"]   # fake sine is fs / 200
    assert abs(out["fundamental"]["amplitude_v"] - 1.0) < 0.02
    assert out["thd_percent"] < 1
    assert out["noise_floor_dbv"] < -100
    assert len(out["preview"]["frequency_hz"]) <= 40
    assert os.path.exists(out["csv_path"])
    with open(out["csv_path"]) as f:
        assert f.readline().strip() == "frequency_hz,amplitude_v,dbv"
    with pytest.raises(RuntimeError, match="window must be"):
        call_tool(server, "capture_spectrum", window="kaiser")


# --------------------------------------------------------------------------- sweep points

def test_frequency_points():
    log = analyzer.frequency_points(100, 1e4, 5, True)
    assert [round(f) for f in log] == [100, 316, 1000, 3162, 10000]
    assert analyzer.frequency_points(100, 500, 5, False) == [100, 200, 300, 400, 500]
    assert analyzer.frequency_points(100, 500, 1, True) == [100]
    with pytest.raises(Exception, match="lower than stop"):
        analyzer.frequency_points(500, 100, 3, True)


# --------------------------------------------------------------------------- network analyzer

def test_network_analyzer_measures_fake_high_pass(server, fake_dwf):
    out = call_tool(server, "network_analyzer", start_frequency=10, stop_frequency=1e5, steps=41)
    assert out["steps"] == 41
    first, last = out["points"][0], out["points"][-1]
    expected_first = 20 * math.log10(10 / math.sqrt(10 ** 2 + FC ** 2))
    assert abs(first["gain_db"] - expected_first) < 0.05
    assert abs(first["phase_deg"] - 89.2) < 0.5
    assert abs(last["gain_db"]) < 0.01 and abs(last["phase_deg"]) < 1
    assert first["input_level"] == 1.0 and first["warnings"] == []
    summary = out["summary"]
    assert summary["response"] == "high_pass"
    assert len(summary["cutoff_hz_minus3db"]) == 1
    assert abs(summary["cutoff_hz_minus3db"][0] - FC) / FC < 0.03
    assert os.path.exists(out["csv_path"])
    assert fake_dwf.calls_to("FDwfAnalogImpedanceReset")
    configure = [a[1].value for a in fake_dwf.calls_to("FDwfAnalogImpedanceConfigure")]
    assert configure == [1, 0]
    (periods,) = fake_dwf.calls_to("FDwfAnalogImpedancePeriodSet")
    assert periods[1].value == 16
    assert server.session.scope_settings is None


def test_network_analyzer_validation(server, fake_dwf):
    with pytest.raises(RuntimeError, match="exceeds the generator"):
        call_tool(server, "network_analyzer", start_frequency=1e3, stop_frequency=50e6)
    with pytest.raises(RuntimeError, match="steps must be"):
        call_tool(server, "network_analyzer", steps=0)
    with pytest.raises(RuntimeError, match="amplitude"):
        call_tool(server, "network_analyzer", amplitude=6)
    assert not fake_dwf.calls_to("FDwfAnalogImpedanceConfigure")


def test_bode_summary_classification():
    def points(gains):
        return [{"frequency_hz": 10 ** i, "gain_db": g, "phase_deg": 0} for i, g in enumerate(gains)]
    assert analyzer.summarize_bode(points([0, 0, 0, 0]))["response"] == "flat"
    assert analyzer.summarize_bode(points([0, 0, -3.5, -20]))["response"] == "low_pass"
    assert analyzer.summarize_bode(points([-20, -3.5, 0, 0]))["response"] == "high_pass"
    assert analyzer.summarize_bode(points([-20, 0, 0, -20]))["response"] == "band_pass"
    assert analyzer.summarize_bode([]) == {}


# --------------------------------------------------------------------------- impedance analyzer

def test_impedance_analyzer_measures_fake_capacitor(server, fake_dwf):
    out = call_tool(server, "impedance_analyzer", start_frequency=100, stop_frequency=1e5, steps=11,
                    reference_ohm=1e3)
    summary = out["summary"]
    assert summary["dut_type"] == "capacitive"
    assert abs(summary["series_capacitance_f"] - 0.22e-6) < 1e-9
    assert summary["median_resistance_ohm"] == 5.0
    p = out["points"][0]
    assert abs(p["impedance_ohm"] - 7234.3) < 1 and abs(p["phase_deg"] + 89.96) < 0.1
    assert p["reactance_ohm"] < 0 and p["quality_factor"] > 1000
    assert os.path.exists(out["csv_path"])
    (ref,) = fake_dwf.calls_to("FDwfAnalogImpedanceReferenceSet")
    assert ref[1].value == 1e3
    (mode,) = fake_dwf.calls_to("FDwfAnalogImpedanceModeSet")
    assert mode[1].value == 0


def test_impedance_analyzer_mode_and_validation(server, fake_dwf):
    call_tool(server, "impedance_analyzer", steps=2, mode="shunt_reference", save_csv=False)
    (mode,) = fake_dwf.calls_to("FDwfAnalogImpedanceModeSet")
    assert mode[1].value == 1
    with pytest.raises(RuntimeError, match="mode must be"):
        call_tool(server, "impedance_analyzer", mode="parallel")
    with pytest.raises(RuntimeError, match="reference_ohm"):
        call_tool(server, "impedance_analyzer", reference_ohm=0)


def test_summarize_impedance_inductor():
    points = [{"frequency_hz": f, "impedance_ohm": 2 * math.pi * f * 1e-3, "resistance_ohm": 1.0,
               "reactance_ohm": 2 * math.pi * f * 1e-3, "series_inductance_h": 1e-3,
               "series_capacitance_f": -1.0} for f in (1e3, 1e4, 1e5)]
    summary = analyzer.summarize_impedance(points)
    assert summary["dut_type"] == "inductive" and summary["series_inductance_h"] == 1e-3


# --------------------------------------------------------------------------- generator extras

def test_custom_waveform(server, fake_dwf):
    out = call_tool(server, "generate_waveform", channel=2, function="custom", frequency=1e3, amplitude=2.0,
                    data=[0, 0.5, 1, 0.5, 0, -0.5, -1, -0.5])
    assert out["custom_samples"] == 8 and out["sample_rate_hz"] == 8e3
    (data,) = fake_dwf.calls_to("FDwfAnalogOutNodeDataSet")
    assert data[1].value == 1 and data[4].value == 8
    assert list(data[3])[:3] == [0.0, 0.5, 1.0]
    (func,) = fake_dwf.calls_to("FDwfAnalogOutNodeFunctionSet")
    assert func[3].value == 30                                   # funcCustom
    with pytest.raises(RuntimeError, match="between -1 and 1"):
        call_tool(server, "generate_waveform", channel=1, function="custom", frequency=1e3, amplitude=1,
                  data=[0, 2])
    with pytest.raises(RuntimeError, match="data is required"):
        call_tool(server, "generate_waveform", channel=1, function="custom", frequency=1e3, amplitude=1)
    with pytest.raises(RuntimeError, match="only used with"):
        call_tool(server, "generate_waveform", channel=1, function="sine", frequency=1e3, amplitude=1, data=[0])
    with pytest.raises(RuntimeError, match="at most 4096"):
        call_tool(server, "generate_waveform", channel=1, function="custom", frequency=1e3, amplitude=1,
                  data=[0] * 4097)


def test_phase_and_start_all_channels(server, fake_dwf):
    call_tool(server, "generate_waveform", channel=1, function="sine", frequency=1e3, amplitude=1)
    out = call_tool(server, "generate_waveform", channel=2, function="sine", frequency=1e3, amplitude=1,
                    phase_deg=90, start_all_channels=True)
    assert out["phase_deg"] == 90 and out["running_channels"] == [1, 2]
    phases = [(a[1].value, a[3].value) for a in fake_dwf.calls_to("FDwfAnalogOutNodePhaseSet")]
    assert (1, 90.0) in phases
    assert fake_dwf.calls_to("FDwfAnalogOutConfigure")[-1][1].value == -1
    # FM/AM nodes are disabled on every plain generate
    disabled = [(a[1].value, a[2].value) for a in fake_dwf.calls_to("FDwfAnalogOutNodeEnableSet") if a[3].value == 0]
    assert (1, 1) in disabled and (1, 2) in disabled


def test_sweep_waveform_configures_fm_node(server, fake_dwf):
    out = call_tool(server, "sweep_waveform", channel=1, start_frequency=1e3, stop_frequency=9e3,
                    sweep_time_s=0.5, amplitude=1.0)
    assert out["carrier_frequency_hz"] == 5e3 and out["deviation_percent"] == 80.0
    calls = fake_dwf.calls
    enabled = [(a[1].value, a[2].value) for a in fake_dwf.calls_to("FDwfAnalogOutNodeEnableSet") if a[3].value == 1]
    assert (0, 0) in enabled and (0, 1) in enabled              # carrier and FM nodes of channel 1
    freq = [a[3].value for name, a in calls if name == "FDwfAnalogOutNodeFrequencySet" and a[2].value == 1]
    assert freq == [2.0]
    amp = [a[3].value for name, a in calls if name == "FDwfAnalogOutNodeAmplitudeSet" and a[2].value == 1]
    assert amp == [80.0]
    func = [a[3].value for name, a in calls if name == "FDwfAnalogOutNodeFunctionSet" and a[2].value == 1]
    assert func == [4]                                           # funcRampUp
    carrier_freq = [a[3].value for name, a in calls if name == "FDwfAnalogOutNodeFrequencySet" and a[2].value == 0]
    assert carrier_freq == [5e3]
    assert fake_dwf.calls_to("FDwfAnalogOutConfigure")[-1][2].value == 1
    out = call_tool(server, "sweep_waveform", channel=2, start_frequency=9e3, stop_frequency=1e3, sweep_time_s=0.5)
    func = [a[3].value for name, a in fake_dwf.calls if name == "FDwfAnalogOutNodeFunctionSet"
            and a[1].value == 1 and a[2].value == 1]
    assert func == [5]                                           # funcRampDown for a downward sweep
    with pytest.raises(RuntimeError, match="must differ"):
        call_tool(server, "sweep_waveform", channel=1, start_frequency=1e3, stop_frequency=1e3)
    with pytest.raises(RuntimeError, match="periodic shape"):
        call_tool(server, "sweep_waveform", channel=1, start_frequency=1e3, stop_frequency=2e3, function="noise")
