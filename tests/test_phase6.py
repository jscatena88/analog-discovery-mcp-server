"""Record modes, counters, trigger types, triggered starts, bus patterns, trigger pins (fake libdwf)."""

import os

import pytest

from conftest import call_tool


# --------------------------------------------------------------------------- scope trigger types and input options

def test_pulse_trigger_configuration(server, fake_dwf):
    call_tool(server, "capture_waveform", channels=[1], trigger_source="ch1", trigger_type="pulse", trigger_level=1.5,
              trigger_edge="rising", trigger_length_s=2e-4, trigger_length_condition="more",
              trigger_holdoff_s=0.01, trigger_hysteresis_v=0.05, save_csv=False)
    (kind,) = fake_dwf.calls_to("FDwfAnalogInTriggerTypeSet")
    assert kind[1].value == 1                                   # trigtypePulse
    (length,) = fake_dwf.calls_to("FDwfAnalogInTriggerLengthSet")
    assert length[1].value == 2e-4
    (cond,) = fake_dwf.calls_to("FDwfAnalogInTriggerLengthConditionSet")
    assert cond[1].value == 2                                   # triglenMore
    (hold,) = fake_dwf.calls_to("FDwfAnalogInTriggerHoldOffSet")
    assert hold[1].value == 0.01
    (hyst,) = fake_dwf.calls_to("FDwfAnalogInTriggerHysteresisSet")
    assert hyst[1].value == 0.05
    (level,) = fake_dwf.calls_to("FDwfAnalogInTriggerLevelSet")
    assert level[1].value == 1.5


def test_edge_trigger_does_not_set_length(server, fake_dwf):
    call_tool(server, "capture_waveform", channels=[1], trigger_source="ch2", trigger_edge="either", save_csv=False)
    assert not fake_dwf.calls_to("FDwfAnalogInTriggerLengthSet")
    (cond,) = fake_dwf.calls_to("FDwfAnalogInTriggerConditionSet")
    assert cond[1].value == 2                                   # DwfTriggerSlopeEither


def test_cross_instrument_trigger_sources(server, fake_dwf):
    for name, value in (("pc", 1), ("pattern", 6), ("logic", 3), ("external3", 13)):
        call_tool(server, "capture_waveform", channels=[1], trigger_source=name, save_csv=False)
        assert fake_dwf.calls_to("FDwfAnalogInTriggerSourceSet")[-1][1].value == value, name
    with pytest.raises(RuntimeError, match="trigger_type must be"):
        call_tool(server, "capture_waveform", trigger_source="ch1", trigger_type="glitch")


def test_input_filter_and_attenuation(server, fake_dwf):
    call_tool(server, "capture_waveform", filter="average", attenuation=10, save_csv=False)
    flt = fake_dwf.calls_to("FDwfAnalogInChannelFilterSet")[-1]   # the wrapper sets decimate first
    assert (flt[1].value, flt[2].value) == (-1, 1)              # all channels, filterAverage
    (att,) = fake_dwf.calls_to("FDwfAnalogInChannelAttenuationSet")
    assert att[2].value == 10
    calls = len(fake_dwf.calls_to("FDwfAnalogInChannelFilterSet"))
    call_tool(server, "capture_waveform", filter="average", attenuation=10, save_csv=False)
    assert len(fake_dwf.calls_to("FDwfAnalogInChannelFilterSet")) == calls   # settings reused
    with pytest.raises(RuntimeError, match="filter must be"):
        call_tool(server, "capture_waveform", filter="median")


# --------------------------------------------------------------------------- record modes

def test_record_waveform_streams_full_length(server, fake_dwf):
    out = call_tool(server, "record_waveform", channels=[1, 2], sampling_frequency=100e3, duration_s=0.25,
                    preview_points=50)
    assert out["samples_per_channel"] == 25000 and out["samples_lost"] == 0
    assert abs(out["duration_s"] - 0.25) < 1e-9
    assert len(out["preview"]["ch1"]) <= 50
    assert os.path.exists(out["csv_path"])
    with open(out["csv_path"]) as f:
        assert sum(1 for _ in f) == 25001
    modes = [a[1].value for a in fake_dwf.calls_to("FDwfAnalogInAcquisitionModeSet")]
    assert 3 in modes and modes[-1] == 0                        # record, then back to single
    (length,) = fake_dwf.calls_to("FDwfAnalogInRecordLengthSet")
    assert abs(length[1].value - 0.25) < 1e-9
    assert len(fake_dwf.calls_to("FDwfAnalogInStatusData")) == 2 * 25           # 25 chunks x 2 channels
    assert server.session.scope_settings is None


def test_record_waveform_limits(server):
    with pytest.raises(RuntimeError, match="exceeds the 2000000"):
        call_tool(server, "record_waveform", sampling_frequency=1e6, duration_s=3)
    with pytest.raises(RuntimeError, match="duration_s must be positive"):
        call_tool(server, "record_waveform", duration_s=0)


def test_record_logic_streams_full_length(server, fake_dwf):
    out = call_tool(server, "record_logic", pins=[0, 1], sampling_frequency=1e6, duration_s=0.01, max_edges=3)
    assert out["samples"] == 10000 and out["samples_lost"] == 0
    assert out["active_pins"] == [0]
    assert out["pins"]["dio1"]["high_fraction"] == 1.0
    assert os.path.exists(out["csv_path"])
    modes = [a[1].value for a in fake_dwf.calls_to("FDwfDigitalInAcquisitionModeSet")]
    assert 3 in modes and modes[-1] == 0
    assert len(fake_dwf.calls_to("FDwfDigitalInStatusData")) == 10


# --------------------------------------------------------------------------- counters

def test_measure_frequency_uses_hardware_counter(server, fake_dwf):
    out = call_tool(server, "measure_frequency", channel=2, gate_time_s=0.05, trigger_level=0.5)
    assert out == {"channel": 2, "frequency_hz": 12345.0, "edges_counted": 1234.0, "gate_time_s": 0.05,
                   "trigger_level_v": 0.5, "method": "hardware"}
    gates = [a[1].value for a in fake_dwf.calls_to("FDwfAnalogInCounterSet")]
    assert gates == [0.05, 0.0]                                 # armed, then released
    (chan,) = fake_dwf.calls_to("FDwfAnalogInTriggerChannelSet")
    assert chan[1].value == 1
    with pytest.raises(RuntimeError, match="gate_time_s"):
        call_tool(server, "measure_frequency", gate_time_s=20)


def test_software_frequency_fallback_without_hardware_counter(server, fake_dwf):
    import math
    fake_dwf.counter_supported = False
    # fast signal: fs / 200 -> resolved from the 100 MS/s capture
    out = call_tool(server, "measure_frequency", channel=1, gate_time_s=0.05)
    assert out["method"] == "software_capture"
    assert abs(out["frequency_hz"] - 500e3) < 1e3
    assert not fake_dwf.calls_to("FDwfAnalogInCounterSet")
    # slow signal: 1 kHz has no crossings in 82 us, so the record path is used
    fake_dwf.signal = lambda ch, i, fs: math.sin(2 * math.pi * 1000 * i / fs)
    out = call_tool(server, "measure_frequency", channel=1, gate_time_s=0.05)
    assert out["method"] == "software_record" and out["gate_time_s"] == 0.05
    assert abs(out["frequency_hz"] - 1000) < 1
    assert out["samples_lost"] == 0


def test_software_pin_frequency_fallback(server, fake_dwf):
    fake_dwf.counter_supported = False
    out = call_tool(server, "measure_pin_frequency", pin=0, gate_time_s=0.1)
    assert out["method"] == "software_capture" and abs(out["frequency_hz"] - 1e6) < 1e3
    fake_dwf.logic_pattern = lambda i: (i // 5000) & 1          # 100 Hz at 1 MS/s
    out = call_tool(server, "measure_pin_frequency", pin=0, gate_time_s=0.1)
    assert out["method"] == "software_record" and abs(out["frequency_hz"] - 100) < 0.5
    fake_dwf.logic_pattern = lambda i: 0b10
    with pytest.raises(RuntimeError, match="fewer than two rising edges"):
        call_tool(server, "measure_pin_frequency", pin=0, gate_time_s=0.01)


def test_measure_pin_frequency(server, fake_dwf):
    out = call_tool(server, "measure_pin_frequency", pin=5, gate_time_s=0.1)
    assert out["frequency_hz"] == 1e6 and out["pin"] == 5 and out["method"] == "hardware"
    (trig,) = fake_dwf.calls_to("FDwfDigitalInTriggerSet")
    assert trig[3].value == 1 << 5
    assert [a[1].value for a in fake_dwf.calls_to("FDwfDigitalInCounterSet")] == [0.1, 0.0]


# --------------------------------------------------------------------------- logic analyzer triggers

def test_logic_multi_pin_trigger_masks(server, fake_dwf):
    call_tool(server, "capture_logic", trigger_rising=[0, 3], trigger_falling=[7], trigger_high=[1],
              trigger_low=[2], trigger_count=3, trigger_length_min_s=1e-6, trigger_length_max_s=5e-6,
              save_csv=False)
    (trig,) = fake_dwf.calls_to("FDwfDigitalInTriggerSet")
    assert [a.value for a in trig[1:]] == [1 << 2, 1 << 1, (1 << 0) | (1 << 3), 1 << 7]
    (count,) = fake_dwf.calls_to("FDwfDigitalInTriggerCountSet")
    assert count[1].value == 3
    (length,) = fake_dwf.calls_to("FDwfDigitalInTriggerLengthSet")
    assert (length[1].value, length[2].value) == (1e-6, 5e-6)
    assert fake_dwf.calls_to("FDwfDigitalInTriggerSourceSet")[-1][1].value == 3


def test_logic_trigger_pin_combines_with_lists(server, fake_dwf):
    call_tool(server, "capture_logic", trigger_pin=4, trigger_edge="either", trigger_high=[9], save_csv=False)
    (trig,) = fake_dwf.calls_to("FDwfDigitalInTriggerSet")
    assert [a.value for a in trig[1:]] == [0, 1 << 9, 1 << 4, 1 << 4]
    with pytest.raises(RuntimeError, match="both high and low"):
        call_tool(server, "capture_logic", trigger_high=[1], trigger_low=[1])


def test_logic_trigger_from_other_instrument(server, fake_dwf):
    out = call_tool(server, "capture_logic", trigger_source="scope", trigger_slope="falling", pretrigger_samples=10,
                    save_csv=False)
    assert fake_dwf.calls_to("FDwfDigitalInTriggerSourceSet")[-1][1].value == 2
    (slope,) = fake_dwf.calls_to("FDwfDigitalInTriggerSlopeSet")
    assert slope[1].value == 1
    assert not fake_dwf.calls_to("FDwfDigitalInTriggerSet")
    assert out["trigger"]["index"] == 10 and out["trigger"]["source"] == "scope"


# --------------------------------------------------------------------------- triggered starts and trigger pins

def test_generator_triggered_start(server, fake_dwf):
    out = call_tool(server, "generate_waveform", channel=1, function="sine", frequency=1e3, amplitude=1.0,
                    trigger_source="pc", trigger_slope="falling", repeat_trigger=True)
    assert out["armed"] is True and out["running"] is False
    (src,) = fake_dwf.calls_to("FDwfAnalogOutTriggerSourceSet")
    assert (src[1].value, src[2].value) == (0, 1)
    (slope,) = fake_dwf.calls_to("FDwfAnalogOutTriggerSlopeSet")
    assert slope[2].value == 1
    (rep,) = fake_dwf.calls_to("FDwfAnalogOutRepeatTriggerSet")
    assert rep[2].value == 1
    with pytest.raises(RuntimeError, match="cannot trigger from itself"):
        call_tool(server, "generate_waveform", channel=2, function="sine", frequency=1e3, amplitude=1.0,
                  trigger_source="wavegen2")
    out = call_tool(server, "sweep_waveform", channel=2, start_frequency=1e3, stop_frequency=2e3,
                    trigger_source="external1")
    assert out["armed"] is True
    assert fake_dwf.calls_to("FDwfAnalogOutTriggerSourceSet")[-1][2].value == 11


def test_pattern_triggered_start_and_output_type(server, fake_dwf):
    out = call_tool(server, "generate_pattern", channel=3, kind="clock", frequency=1e6, output="open_drain",
                    trigger_source="external1", trigger_slope="either")
    assert out["armed"] is True and out["output"] == "open_drain"
    (src,) = fake_dwf.calls_to("FDwfDigitalOutTriggerSourceSet")
    assert src[1].value == 11
    (slope,) = fake_dwf.calls_to("FDwfDigitalOutTriggerSlopeSet")
    assert slope[1].value == 2
    (drv,) = fake_dwf.calls_to("FDwfDigitalOutOutputSet")
    assert (drv[1].value, drv[2].value) == (3, 1)
    with pytest.raises(RuntimeError, match="output must be"):
        call_tool(server, "generate_pattern", channel=3, output="totem")


def test_bus_pattern_packs_bit_slices(server, fake_dwf):
    out = call_tool(server, "generate_bus_pattern", pins=[0, 1, 2], words=list(range(8)), bit_rate=1e6)
    assert out["pins"] == [0, 1, 2] and out["words"] == 8 and out["bit_rate_hz"] == 1e6
    assert out["pattern_period_s"] == 8e-6 and out["running_channels"] == [0, 1, 2]
    data = {a[1].value: a[2]._obj[0] for a in fake_dwf.calls_to("FDwfDigitalOutDataSet")}
    assert data == {0: 0xAA, 1: 0xCC, 2: 0xF0}                 # bit i of 0..7, LSB-first per pin
    assert all(a[3].value == 8 for a in fake_dwf.calls_to("FDwfDigitalOutDataSet"))
    with pytest.raises(RuntimeError, match="between 0 and 7"):
        call_tool(server, "generate_bus_pattern", pins=[0, 1, 2], words=[8])
    with pytest.raises(RuntimeError, match="distinct"):
        call_tool(server, "generate_bus_pattern", pins=[0, 0], words=[1])


def test_trigger_pin_output_and_pc_trigger(server, fake_dwf):
    out = call_tool(server, "set_trigger_pin", pin=1, source="pattern")
    assert out == {"pin": "T1", "source": "pattern", "direction": "output"}
    (cfg,) = fake_dwf.calls_to("FDwfDeviceTriggerSet")
    assert (cfg[1].value, cfg[2].value) == (0, 6)
    out = call_tool(server, "set_trigger_pin", pin=2)
    assert out["direction"] == "input"
    with pytest.raises(RuntimeError, match="trigger_source must be"):
        call_tool(server, "set_trigger_pin", pin=1, source="external2")
    assert call_tool(server, "send_pc_trigger") == {"sent": True}
    assert fake_dwf.calls_to("FDwfDeviceTriggerPC")
