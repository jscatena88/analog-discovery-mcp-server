"""Smoke tests against a real Analog Discovery 2. Run with: uv run pytest --hardware

Most tests need nothing wired. The protocol and external-trigger tests (marked `jumpered`)
expect these jumpers: DIO0->DIO8, DIO1->DIO9, DIO2->DIO10 and T1->DIO15; run them with
`uv run pytest --hardware --jumpered`.
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


def test_static_pins_read_back_driven_levels(hw):
    out = call_tool(hw, "write_digital_pins", high=[1], low=[2])
    assert out["levels"]["dio1"] == 1 and out["levels"]["dio2"] == 0
    assert out["output_pins"] == [1, 2]
    out = call_tool(hw, "write_digital_pins", low=[1])
    assert out["levels"]["dio1"] == 0
    out = call_tool(hw, "write_digital_pins", inputs=[1, 2])
    assert out["output_pins"] == []


def test_pattern_clock_seen_by_logic_analyzer(hw):
    gen = call_tool(hw, "generate_pattern", channel=0, kind="clock", frequency=1e6)
    assert gen["frequency_hz"] == 1e6
    out = call_tool(hw, "capture_logic", pins=[0], trigger_pin=0, pretrigger_samples=100, max_edges=3)
    dio0 = out["pins"]["dio0"]
    assert abs(dio0["frequency_hz_estimate"] - 1e6) < 1e4
    assert dio0["transitions"] > 70
    assert out["trigger"]["index"] == 100
    fs = out["sampling_frequency_hz"]
    # a rising edge sits at (or within a couple of samples of) the trigger time
    assert any(level == 1 and abs(t - out["trigger"]["time_s"]) <= 3 / fs for t, level in dio0["edges"])
    assert os.path.exists(out["csv_path"])
    assert call_tool(hw, "stop_pattern")["running_channels"] == []
    quiet = call_tool(hw, "capture_logic", pins=[0], save_csv=False)
    assert quiet["pins"]["dio0"]["transitions"] <= 2


def test_custom_pattern_bit_rate(hw):
    # 11110000 at 1 Mbit/s is a 125 kHz square wave
    call_tool(hw, "generate_pattern", channel=3, kind="custom", frequency=1e6, data="11110000")
    out = call_tool(hw, "capture_logic", pins=[3], sampling_frequency=10e6, trigger_pin=3, save_csv=False)
    assert abs(out["pins"]["dio3"]["frequency_hz_estimate"] - 125e3) < 2e3
    call_tool(hw, "stop_pattern", channel=3)


jumpered = pytest.mark.jumpered


@jumpered
def test_uart_loopback(hw):
    out = call_tool(hw, "uart_send", data="Hello AD2\r\n", tx_pin=0, rx_pin=8, baud_rate=115200)
    assert out["received"]["text"] == "Hello AD2\r\n"
    assert out["stop_reason"] == "idle" and out["parity_errors"] == 0
    out = call_tool(hw, "uart_send", data="00 ff 55 aa", data_format="hex", tx_pin=0, rx_pin=8,
                    baud_rate=9600, parity="even", stop_bits=2)
    assert out["received"]["hex"] == "00 ff 55 aa"
    call_tool(hw, "uart_send", data="buffered", tx_pin=0, rx_pin=8, baud_rate=9600, parity="even",
              stop_bits=2, receive_timeout_s=0)
    out = call_tool(hw, "uart_receive", tx_pin=0, rx_pin=8, baud_rate=9600, parity="even", stop_bits=2,
                    timeout_s=1.0)
    assert out["received"]["text"] == "buffered"


@jumpered
def test_spi_loopback(hw):
    for mode in (0, 1, 2, 3):
        out = call_tool(hw, "spi_transfer", write="a5 01 ff 00 3c", clock_pin=1, mosi_pin=0, miso_pin=8,
                        select_pin=2, frequency=1e6, mode=mode)
        assert out["read"]["hex"] == "a5 01 ff 00 3c", f"mode {mode}"
    out = call_tool(hw, "spi_transfer", write=list(range(256)), read_count=300, clock_pin=1, mosi_pin=0,
                    miso_pin=8, select_pin=2, frequency=5e6, fill_byte=0x5A)
    assert out["read"]["bytes"] == list(range(256)) + [0x5A] * 44


@jumpered
def test_i2c_without_pullups_reports_bus_not_free(hw):
    with pytest.raises(RuntimeError, match="pull-up"):
        call_tool(hw, "i2c_scan", scl_pin=3, sda_pin=4)


@jumpered
def test_external_trigger_from_t1(hw):
    call_tool(hw, "generate_pattern", channel=15, kind="clock", frequency=1e3)
    out = call_tool(hw, "capture_waveform", channels=[1], sampling_frequency=1e6, trigger_source="external1",
                    trigger_timeout_s=0, max_wait_s=2.0, save_csv=False)
    assert out["trigger"]["source"] == "external1" and out["samples_per_channel"] == 8192
    with pytest.raises(RuntimeError, match="did not complete within 0.5"):
        call_tool(hw, "capture_waveform", channels=[1], trigger_source="external2", trigger_timeout_s=0,
                  max_wait_s=0.5, save_csv=False)
    call_tool(hw, "stop_pattern")
    # Releasing the pin lets it float, which can produce one spurious edge on T1; hold it low instead.
    call_tool(hw, "write_digital_pins", low=[15])
    with pytest.raises(RuntimeError, match="did not complete within 0.5"):
        call_tool(hw, "capture_waveform", channels=[1], trigger_source="external1", trigger_timeout_s=0,
                  max_wait_s=0.5, save_csv=False)
    call_tool(hw, "write_digital_pins", inputs=[15])


def test_wavegen_trigger_source(hw):
    call_tool(hw, "generate_waveform", channel=1, function="sine", frequency=1e3, amplitude=1.0)
    out = call_tool(hw, "capture_waveform", channels=[1], trigger_source="wavegen1", trigger_timeout_s=0,
                    max_wait_s=2.0, save_csv=False)
    assert out["samples_per_channel"] == 8192
    call_tool(hw, "stop_waveform")


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
