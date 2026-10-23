"""Smoke tests against a real Analog Discovery 2. Run with: uv run pytest --hardware

Nothing needs to be wired to the device. Scope readings are only checked for sanity.
"""

import os

import pytest

from conftest import call_tool

pytestmark = pytest.mark.hardware


@pytest.fixture
def hw(monkeypatch, tmp_path):
    monkeypatch.setenv("AD2_CAPTURE_DIR", str(tmp_path))
    import server as srv
    srv.session.disconnect()
    yield srv
    srv.session.disconnect()


def test_enumerate_and_connect(hw):
    devices = call_tool(hw, "list_devices")
    assert devices["device_count"] >= 1
    assert devices["devices"][0]["serial"].startswith("SN:")
    info = call_tool(hw, "connect_device")
    assert info["name"] == "Analog Discovery 2"
    assert info["analog_input"]["max_buffer_size"] == 8192
    info = call_tool(hw, "connect_device", configuration=1)
    assert info["analog_input"]["max_buffer_size"] == 16384


def test_system_status(hw):
    out = call_tool(hw, "get_system_status")
    assert 0 < out["monitor"]["temperature_c"] < 90
    assert 4.0 < out["monitor"]["usb_voltage_v"] < 5.5


def test_capture_both_channels(hw):
    out = call_tool(hw, "capture_waveform", sampling_frequency=1e6, preview_points=20)
    assert out["samples_per_channel"] == 8192
    assert out["sampling_frequency_hz"] == 1e6
    assert set(out["channels"]) == {"ch1", "ch2"}
    assert os.path.exists(out["csv_path"])
    for stats in out["channels"].values():
        assert -5.5 < stats["min_v"] <= stats["max_v"] < 5.5


def test_capture_trigger_timeout_is_bounded(hw):
    # Trigger on ch1 at a level the floating input will never reach; auto-trigger after 0.2 s.
    out = call_tool(hw, "capture_waveform", channels=[1], trigger_source="ch1", trigger_level=4.9,
                    trigger_timeout_s=0.2, max_wait_s=3.0, save_csv=False)
    assert out["samples_per_channel"] == 8192


def test_measure_voltage(hw):
    out = call_tool(hw, "measure_voltage", channel=1, samples=8)
    assert -5.5 < out["voltage_v"] < 5.5


def test_generate_and_stop(hw):
    out = call_tool(hw, "generate_waveform", channel=1, function="sine", frequency=1e3, amplitude=1.0)
    assert out["running"] is True
    assert call_tool(hw, "get_device_info")["wavegen_running_channels"] == [1]
    assert call_tool(hw, "stop_waveform")["running_channels"] == []


def test_power_supplies_round_trip(hw):
    out = call_tool(hw, "set_power_supplies", positive_volts=3.3, positive_enabled=True,
                    negative_volts=-2.0, negative_enabled=True)
    assert out["master_enabled"] is True and out["master_active"] is True
    assert out["positive"]["enabled"] is True and out["positive"]["active"] is True
    assert abs(out["positive"]["voltage_setpoint_v"] - 3.3) < 0.05
    assert abs(out["positive"]["voltage_v"] - 3.3) < 0.05      # measured after settling
    assert abs(out["negative"]["voltage_v"] + 2.0) < 0.05
    out = call_tool(hw, "disable_power_supplies")
    assert out["master_enabled"] is False and out["master_active"] is False
    assert out["positive"]["enabled"] is False and out["positive"]["active"] is False
