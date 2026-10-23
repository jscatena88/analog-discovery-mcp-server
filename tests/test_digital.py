"""Static DIO, pattern generator and logic analyzer tools against the fake libdwf."""

import os

import pytest

from conftest import call_tool


# --------------------------------------------------------------------------- static DIO

def test_read_pins_default_all_inputs_low(server):
    out = call_tool(server, "read_digital_pins")
    assert out["value"] == 0 and out["value_hex"] == "0x0000"
    assert out["high_pins"] == [] and out["output_pins"] == []
    assert len(out["levels"]) == 16 and out["levels"]["dio15"] == 0


def test_read_pins_sees_external_levels(server, fake_dwf):
    fake_dwf.dio_external = 0b1010
    out = call_tool(server, "read_digital_pins")
    assert out["high_pins"] == [1, 3]


def test_write_pins_drives_and_releases(server, fake_dwf):
    out = call_tool(server, "write_digital_pins", high=[1, 3], low=[2])
    assert out["output_pins"] == [1, 2, 3]
    assert out["high_pins"] == [1, 3]
    assert out["levels"]["dio2"] == 0
    assert fake_dwf.calls_to("FDwfDigitalIOConfigure")
    out = call_tool(server, "write_digital_pins", inputs=[1], low=[3])
    assert out["output_pins"] == [2, 3]
    assert out["high_pins"] == []


def test_write_pins_rejects_bad_input(server, fake_dwf):
    with pytest.raises(RuntimeError, match="more than one"):
        call_tool(server, "write_digital_pins", high=[1], low=[1])
    with pytest.raises(RuntimeError, match="between 0 and 15"):
        call_tool(server, "write_digital_pins", high=[16])
    with pytest.raises(RuntimeError, match="nothing to do"):
        call_tool(server, "write_digital_pins")
    assert not fake_dwf.calls_to("FDwfDigitalIOOutputSet")


# --------------------------------------------------------------------------- pattern generator

def test_clock_uses_fine_counter(server, fake_dwf):
    out = call_tool(server, "generate_pattern", channel=0, kind="clock", frequency=1e6)
    assert out["frequency_hz"] == 1e6 and out["duty_cycle_percent"] == 50.0
    (div,) = fake_dwf.calls_to("FDwfDigitalOutDividerSet")
    assert div[2].value == 1
    (counter,) = fake_dwf.calls_to("FDwfDigitalOutCounterSet")
    assert (counter[2].value, counter[3].value) == (50, 50)
    (cfg,) = fake_dwf.calls_to("FDwfDigitalOutConfigure")
    assert cfg[1].value == 1
    assert out["running_channels"] == [0]


def test_low_frequency_pulse_raises_divider(server, fake_dwf):
    out = call_tool(server, "generate_pattern", channel=5, kind="pulse", frequency=1e3, duty_cycle=25)
    (div,) = fake_dwf.calls_to("FDwfDigitalOutDividerSet")
    assert div[2].value == 4                       # 100e6 / 1e3 / 32768 -> 4
    (counter,) = fake_dwf.calls_to("FDwfDigitalOutCounterSet")
    assert (counter[2].value, counter[3].value) == (18750, 6250)
    assert abs(out["frequency_hz"] - 1e3) < 1e-6 and out["duty_cycle_percent"] == 25.0


def test_custom_pattern_packs_bits(server, fake_dwf):
    out = call_tool(server, "generate_pattern", channel=2, kind="custom", frequency=1e6, data="1011 0001")
    assert out["bits"] == 8 and out["bit_rate_hz"] == 1e6 and out["pattern_period_s"] == 8e-6
    (data,) = fake_dwf.calls_to("FDwfDigitalOutDataSet")
    assert data[3].value == 8
    assert data[2]._obj[0] == 0b10001101          # LSB first: 1,0,1,1,0,0,0,1
    (kind,) = fake_dwf.calls_to("FDwfDigitalOutTypeSet")
    assert kind[2].value == 1                      # DwfDigitalOutTypeCustom


def test_pattern_rejects_bad_arguments(server, fake_dwf):
    with pytest.raises(RuntimeError, match="at most 5e\\+07 Hz"):
        call_tool(server, "generate_pattern", channel=0, kind="clock", frequency=60e6)
    with pytest.raises(RuntimeError, match="data is required"):
        call_tool(server, "generate_pattern", channel=0, kind="custom", frequency=1e6)
    with pytest.raises(RuntimeError, match="at most 1024"):
        call_tool(server, "generate_pattern", channel=0, kind="custom", frequency=1e6, data="1" * 1025)
    with pytest.raises(RuntimeError, match="kind must be"):
        call_tool(server, "generate_pattern", channel=0, kind="sine", frequency=1e3)
    with pytest.raises(RuntimeError, match="only contain 0 and 1"):
        call_tool(server, "generate_pattern", channel=0, kind="custom", frequency=1e3, data="1012")
    assert not fake_dwf.calls_to("FDwfDigitalOutConfigure")


def test_stop_pattern_one_and_all(server, fake_dwf):
    call_tool(server, "generate_pattern", channel=0, kind="clock", frequency=1e6)
    call_tool(server, "generate_pattern", channel=1, kind="random", frequency=1e5)
    out = call_tool(server, "stop_pattern", channel=0)
    assert out == {"stopped_channels": [0], "running_channels": [1]}
    disable = [a for a in fake_dwf.calls_to("FDwfDigitalOutEnableSet") if a[2].value == 0]
    assert disable and disable[-1][1].value == 0
    assert fake_dwf.calls_to("FDwfDigitalOutConfigure")[-1][1].value == 1   # channel 1 keeps running
    out = call_tool(server, "stop_pattern")
    assert out == {"stopped_channels": [1], "running_channels": []}
    assert fake_dwf.calls_to("FDwfDigitalOutReset")
    assert call_tool(server, "get_device_info")["pattern_running_pins"] == []


# --------------------------------------------------------------------------- logic analyzer

def test_capture_logic_summaries_and_csv(server):
    out = call_tool(server, "capture_logic", pins=[0, 1, 2], max_edges=5)
    assert out["samples"] == 4096 and out["sampling_frequency_hz"] == 100e6
    assert out["trigger"] is None
    assert out["active_pins"] == [0]
    dio0 = out["pins"]["dio0"]
    assert abs(dio0["frequency_hz_estimate"] - 1e6) < 1e3
    assert dio0["transitions"] == 81 and dio0["edges_truncated"] is True
    assert dio0["edges"][0] == [50 / 100e6, 1] and len(dio0["edges"]) == 5
    assert abs(dio0["high_fraction"] - 0.5) < 0.02
    dio1 = out["pins"]["dio1"]
    assert dio1 == {"initial_level": 1, "final_level": 1, "transitions": 0, "high_fraction": 1.0,
                    "frequency_hz_estimate": None, "edges": [], "edges_truncated": False}
    assert os.path.exists(out["csv_path"])
    with open(out["csv_path"]) as f:
        lines = f.read().splitlines()
    assert lines[0] == "time_s,value_hex," + ",".join(f"dio{p}" for p in range(16))
    assert len(lines) == 4097
    assert lines[1].startswith("0,0x0002,0,1,0")


def test_capture_logic_trigger_configuration(server, fake_dwf):
    out = call_tool(server, "capture_logic", sampling_frequency=25e6, buffer_size=1000, trigger_pin=3,
                    trigger_edge="falling", pretrigger_samples=100, save_csv=False)
    assert out["sampling_frequency_hz"] == 25e6 and out["samples"] == 1000
    assert out["trigger"] == {"pin": 3, "edge": "falling", "index": 100, "time_s": 100 / 25e6}
    (div,) = fake_dwf.calls_to("FDwfDigitalInDividerSet")
    assert div[1].value == 4
    (trig,) = fake_dwf.calls_to("FDwfDigitalInTriggerSet")
    assert [a.value for a in trig[1:]] == [0, 0, 0, 1 << 3]
    (pos,) = fake_dwf.calls_to("FDwfDigitalInTriggerPositionSet")
    assert pos[1].value == 900
    (src,) = fake_dwf.calls_to("FDwfDigitalInTriggerSourceSet")
    assert src[1].value == 3                        # trigsrcDetectorDigitalIn
    call_tool(server, "capture_logic", sampling_frequency=25e6, buffer_size=1000, save_csv=False)
    assert len(fake_dwf.calls_to("FDwfDigitalInDividerSet")) == 1   # settings reused


def test_capture_logic_rejects_bad_arguments(server):
    with pytest.raises(RuntimeError, match="exceeds this configuration's maximum of 4096"):
        call_tool(server, "capture_logic", buffer_size=5000)
    with pytest.raises(RuntimeError, match="trigger_edge"):
        call_tool(server, "capture_logic", trigger_pin=0, trigger_edge="up")
    with pytest.raises(RuntimeError, match="pretrigger_samples"):
        call_tool(server, "capture_logic", trigger_pin=0, pretrigger_samples=4096)
    with pytest.raises(RuntimeError, match="between 1 and 1e\\+08"):
        call_tool(server, "capture_logic", sampling_frequency=200e6)


def test_capture_logic_times_out(server, fake_dwf):
    fake_dwf.acquisition_done = False
    with pytest.raises(RuntimeError, match="did not complete within 0.05"):
        call_tool(server, "capture_logic", trigger_pin=0, max_wait_s=0.05)


def test_disconnect_resets_digital_instruments(server, fake_dwf):
    call_tool(server, "generate_pattern", channel=0, kind="clock", frequency=1e6)
    call_tool(server, "write_digital_pins", high=[4])
    call_tool(server, "disconnect_device")
    for name in ("FDwfDigitalOutReset", "FDwfDigitalInReset", "FDwfDigitalIOReset"):
        assert fake_dwf.calls_to(name), name
    assert fake_dwf.dio_output_enable == 0
