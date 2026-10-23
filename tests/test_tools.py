"""End-to-end tool calls against the fake libdwf (no hardware)."""

import os

import pytest

from conftest import call_tool


def test_tool_list_is_complete(server):
    import anyio
    names = {t.name for t in anyio.run(server.mcp.list_tools)}
    assert names == {
        "list_devices", "connect_device", "disconnect_device", "get_device_info", "get_system_status",
        "set_power_supplies", "disable_power_supplies", "capture_waveform", "measure_voltage",
        "generate_waveform", "stop_waveform", "read_digital_pins", "write_digital_pins",
        "generate_pattern", "stop_pattern", "capture_logic", "uart_send", "uart_receive",
        "spi_transfer", "i2c_scan", "i2c_write", "i2c_read", "i2c_write_read", "i2c_spy",
        "capture_spectrum", "network_analyzer", "impedance_analyzer", "sweep_waveform",
        "record_waveform", "measure_frequency", "record_logic", "measure_pin_frequency",
        "generate_bus_pattern", "set_trigger_pin", "send_pc_trigger",
        "jtag_idcode", "jtag_count_devices", "jtag_scan", "jtag_tap_state", "jtag_release",
        "jtag_probe_instructions", "swd_idcode", "swd_identify", "swd_read_register", "swd_write_register",
        "observe_pins", "detect_uart", "find_swd", "find_jtag", "find_i2c", "measure_pullups",
    }


def test_list_devices_reports_serial_and_configs(server):
    out = call_tool(server, "list_devices")
    assert out["device_count"] == 1
    dev = out["devices"][0]
    assert dev["serial"] == "SN:FAKE0001"
    assert dev["device_type"] == "Discovery2"
    assert [c["analog_in_buffer"] for c in dev["configurations"]] == [8192, 16384]


def test_connect_and_info(server, fake_dwf):
    info = call_tool(server, "connect_device", configuration=1)
    assert info["name"] == "Analog Discovery 2"
    assert info["configuration"] == 1
    assert info["analog_input"]["channels"] == 2
    assert info["analog_output"]["max_amplitude_volts"] == 5.0
    assert info["analog_io_channels"] == ["V+", "V-", "USB", "Aux"]
    (args,) = fake_dwf.calls_to("FDwfDeviceConfigOpen")
    assert args[1].value == 1


def test_reconnect_does_not_duplicate_capabilities(server):
    call_tool(server, "connect_device")
    call_tool(server, "connect_device")
    info = call_tool(server, "get_device_info")
    assert info["analog_io_channels"] == ["V+", "V-", "USB", "Aux"]


def test_capture_returns_stats_preview_and_csv(server):
    out = call_tool(server, "capture_waveform", channels=[1, 2], preview_points=50)
    assert out["samples_per_channel"] == 8192
    assert out["sampling_frequency_hz"] == 20e6
    ch1, ch2 = out["channels"]["ch1"], out["channels"]["ch2"]
    assert abs(ch1["peak_to_peak_v"] - 2.0) < 0.01
    assert abs(ch2["peak_to_peak_v"] - 1.0) < 0.01
    assert abs(ch1["frequency_hz_estimate"] - 100e3) < 2e3  # fake signal is fs / 200
    assert len(out["preview"]["ch1"]) <= 50
    assert len(out["preview"]["time_s"]) == len(out["preview"]["ch1"])
    assert os.path.exists(out["csv_path"])
    with open(out["csv_path"]) as f:
        assert sum(1 for _ in f) == 8193


def test_capture_defaults_to_all_channels_and_can_skip_csv(server):
    out = call_tool(server, "capture_waveform", save_csv=False)
    assert set(out["channels"]) == {"ch1", "ch2"}
    assert out["csv_path"] is None


def test_capture_reuses_scope_configuration(server, fake_dwf):
    call_tool(server, "capture_waveform", save_csv=False)
    call_tool(server, "capture_waveform", save_csv=False)
    assert len(fake_dwf.calls_to("FDwfAnalogInFrequencySet")) == 1
    call_tool(server, "capture_waveform", sampling_frequency=1e6, save_csv=False)
    assert len(fake_dwf.calls_to("FDwfAnalogInFrequencySet")) == 2


def test_capture_with_trigger_configures_source(server, fake_dwf):
    call_tool(server, "capture_waveform", channels=[1], trigger_source="ch2", trigger_level=0.25,
              trigger_edge="falling", save_csv=False)
    (src,) = fake_dwf.calls_to("FDwfAnalogInTriggerSourceSet")
    assert src[1].value == 2  # trigsrcDetectorAnalogIn
    (chan,) = fake_dwf.calls_to("FDwfAnalogInTriggerChannelSet")
    assert chan[1].value == 1  # zero-based channel 2
    (level,) = fake_dwf.calls_to("FDwfAnalogInTriggerLevelSet")
    assert level[1].value == 0.25


def test_capture_rejects_bad_arguments(server):
    with pytest.raises(RuntimeError, match="between 1 and 2"):
        call_tool(server, "capture_waveform", channels=[3])
    with pytest.raises(RuntimeError, match="trigger_source"):
        call_tool(server, "capture_waveform", trigger_source="ch9")


def test_capture_times_out_instead_of_hanging(server, fake_dwf):
    fake_dwf.acquisition_done = False
    with pytest.raises(RuntimeError, match="did not complete within 0.05"):
        call_tool(server, "capture_waveform", max_wait_s=0.05)


def test_measure_voltage(server):
    out = call_tool(server, "measure_voltage", channel=2, samples=4)
    assert out == {"channel": 2, "voltage_v": 1.234, "min_v": 1.234, "max_v": 1.234, "samples": 4}


def test_system_status(server):
    out = call_tool(server, "get_system_status")
    assert out["monitor"]["temperature_c"] == 29.5
    assert out["monitor"]["usb_voltage_v"] == 4.99
    assert out["supplies"]["master_enabled"] is False


def test_set_power_supplies(server, fake_dwf):
    out = call_tool(server, "set_power_supplies", positive_volts=3.3, positive_enabled=True,
                    negative_volts=-1.5)
    assert out["master_enabled"] is True
    assert out["positive"]["enabled"] is True
    assert out["positive"]["voltage_setpoint_v"] == 3.3
    assert out["positive"]["voltage_v"] == 3.3
    assert out["negative"]["voltage_setpoint_v"] == -1.5
    assert out["negative"]["enabled"] is False
    assert fake_dwf.master_enabled == 1
    assert fake_dwf.calls_to("FDwfAnalogIOConfigure")


def test_set_power_supplies_rejects_out_of_range_without_touching_device(server, fake_dwf):
    with pytest.raises(RuntimeError, match="outside the settable range 0.5 V to 5.0 V"):
        call_tool(server, "set_power_supplies", positive_volts=7.0)
    assert not fake_dwf.calls_to("FDwfAnalogIOChannelNodeSet")
    with pytest.raises(RuntimeError, match="-5.0 V to -0.5 V"):
        call_tool(server, "set_power_supplies", negative_volts=2.0)


def test_disable_power_supplies(server, fake_dwf):
    call_tool(server, "set_power_supplies", positive_volts=5.0, positive_enabled=True)
    out = call_tool(server, "disable_power_supplies")
    assert out["master_enabled"] is False
    assert out["positive"]["enabled"] is False


def test_generate_and_stop(server, fake_dwf):
    out = call_tool(server, "generate_waveform", channel=1, function="Square", frequency=2e3,
                    amplitude=1.0, offset=0.5, symmetry=25)
    assert out["function"] == "square"
    assert out["running"] is True
    (cfg,) = fake_dwf.calls_to("FDwfAnalogOutConfigure")
    assert cfg[1].value == 0 and cfg[2].value is True
    (sym,) = fake_dwf.calls_to("FDwfAnalogOutNodeSymmetrySet")
    assert sym[3].value == 25
    info = call_tool(server, "get_device_info")
    assert info["wavegen_running_channels"] == [1]
    out = call_tool(server, "stop_waveform", channel=1)
    assert out == {"stopped_channels": [1], "running_channels": []}


def test_stop_all_resets_every_channel(server, fake_dwf):
    call_tool(server, "generate_waveform", channel=2, function="sine", frequency=1e3, amplitude=1.0)
    call_tool(server, "stop_waveform")
    (reset,) = fake_dwf.calls_to("FDwfAnalogOutReset")
    assert reset[1].value == -1


def test_generate_rejects_out_of_range(server, fake_dwf):
    with pytest.raises(RuntimeError, match="must not exceed 5.0 V"):
        call_tool(server, "generate_waveform", channel=1, function="sine", frequency=1e3,
                  amplitude=4.0, offset=2.0)
    with pytest.raises(RuntimeError, match="function must be one of"):
        call_tool(server, "generate_waveform", channel=1, function="sawtooth", frequency=1e3, amplitude=1)
    assert not fake_dwf.calls_to("FDwfAnalogOutConfigure")


def test_disconnect_turns_everything_off(server, fake_dwf):
    call_tool(server, "generate_waveform", channel=1, function="sine", frequency=1e3, amplitude=1.0)
    call_tool(server, "set_power_supplies", positive_volts=3.3, positive_enabled=True)
    out = call_tool(server, "disconnect_device")
    assert out == {"disconnected": True, "was_connected": True}
    assert fake_dwf.calls_to("FDwfAnalogOutReset")
    assert fake_dwf.master_enabled == 0
    assert fake_dwf.calls_to("FDwfDeviceClose")
    assert call_tool(server, "disconnect_device")["was_connected"] is False
