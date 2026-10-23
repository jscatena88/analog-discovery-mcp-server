"""Smoke tests against a real Analog Discovery 2. Run with: uv run pytest --hardware

Most tests need nothing wired. Two groups need bench wiring and their own flag:
- `jumpered` (--jumpered): DIO0->DIO8, DIO1->DIO9, DIO2->DIO10, T1->DIO15
- `analog_bench` (--analog-bench): W1 to 1+, 1- to GND; 0.22 uF from the W1/1+ node to 2+;
  1 kOhm from 2+ to GND; 2- to GND (a 723 Hz high-pass for the network analyzer, and the
  capacitor as the impedance analyzer's device under test)
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
    out = call_tool(hw, "spi_transfer", write=[0x1234, 0xBEEF, 0x0F0F, 0x8001], bits_per_word=16, clock_pin=1,
                    mosi_pin=0, miso_pin=8, select_pin=2, frequency=1e6)
    assert out["read_words"] == [0x1234, 0xBEEF, 0x0F0F, 0x8001]
    out = call_tool(hw, "spi_transfer", write=[0x12345678, 0xDEADBEEF], bits_per_word=32, clock_pin=1,
                    mosi_pin=0, miso_pin=8, select_pin=2, frequency=1e6)
    assert out["read_words"] == [0x12345678, 0xDEADBEEF]


@jumpered
def test_jtag_loopback_scan(hw):
    # TDI (DIO2) jumpered to TDO (DIO10): a DR scan reads back exactly what was shifted in
    pins = dict(tck_pin=0, tms_pin=1, tdi_pin=2, tdo_pin=10)
    out = call_tool(hw, "jtag_scan", register="dr", length=16, value="0xA5C3", **pins)
    assert out["shifted_out"] == "0xa5c3" and out["tap_state"] == "idle"
    out = call_tool(hw, "jtag_tap_state", state="shift_ir", **pins)
    assert out["state"] == "shift_ir"
    # with ones looping straight back, the IDCODE read sees an immediate end of chain
    out = call_tool(hw, "jtag_idcode", **pins)
    assert out["count"] == 0
    assert call_tool(hw, "jtag_release")["released"] is True


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


analog_bench = pytest.mark.analog_bench


@analog_bench
def test_network_analyzer_rc_high_pass(hw):
    out = call_tool(hw, "network_analyzer", start_frequency=20, stop_frequency=100e3, steps=25, min_periods=8)
    summary = out["summary"]
    assert summary["response"] == "high_pass"
    (cutoff,) = summary["cutoff_hz_minus3db"]
    assert 400 < cutoff < 1100                                  # 723 Hz nominal, capacitor tolerance
    assert abs(out["points"][-1]["gain_db"]) < 1 and abs(out["points"][-1]["phase_deg"]) < 10
    assert out["points"][0]["gain_db"] < -15 and 70 < out["points"][0]["phase_deg"] < 95
    assert all(p["warnings"] == [] for p in out["points"])
    assert os.path.exists(out["csv_path"])


@analog_bench
def test_impedance_analyzer_capacitor(hw):
    out = call_tool(hw, "impedance_analyzer", start_frequency=200, stop_frequency=20e3, steps=11,
                    reference_ohm=1e3, min_periods=8)
    summary = out["summary"]
    assert summary["dut_type"] == "capacitive"
    assert 0.15e-6 < summary["series_capacitance_f"] < 0.5e-6
    mid = out["points"][5]
    assert mid["reactance_ohm"] < 0 and -95 < mid["phase_deg"] < -60


@analog_bench
def test_spectrum_of_generator_sine(hw):
    call_tool(hw, "generate_waveform", channel=1, function="sine", frequency=1e3, amplitude=1.0)
    out = call_tool(hw, "capture_spectrum", channel=1, sampling_frequency=200e3, window="flattop", max_peaks=5)
    assert abs(out["fundamental"]["frequency_hz"] - 1e3) < 2 * out["resolution_hz"]
    assert 0.95 < out["fundamental"]["amplitude_v"] < 1.05
    assert out["thd_percent"] < 2
    assert out["noise_floor_dbv"] < -60
    call_tool(hw, "stop_waveform")


@analog_bench
def test_sweep_waveform_start_and_end_frequency(hw):
    call_tool(hw, "sweep_waveform", channel=1, start_frequency=1e3, stop_frequency=9e3, sweep_time_s=0.1, amplitude=1.0)
    out = call_tool(hw, "capture_waveform", channels=[1], sampling_frequency=100e3, trigger_source="wavegen1",
                    trigger_timeout_s=0, max_wait_s=2.0, preview_points=0, save_csv=False)
    samples = out["preview"]["ch1"]                               # preview_points=0 returns every sample
    fs = out["sampling_frequency_hz"]
    from ad2.analysis import estimate_frequency
    n = len(samples)
    first = estimate_frequency(samples[: n // 10], fs)              # first 8 ms of the 100 ms sweep
    last = estimate_frequency(samples[-n // 10:], fs)               # 74-82 ms into the sweep
    assert 900 < first < 2200, first
    assert 6000 < last < 9000, last
    call_tool(hw, "stop_waveform")


@analog_bench
def test_record_waveform_long_capture(hw):
    call_tool(hw, "generate_waveform", channel=1, function="sine", frequency=1e3, amplitude=1.0)
    out = call_tool(hw, "record_waveform", channels=[1], sampling_frequency=100e3, duration_s=0.5, save_csv=False)
    call_tool(hw, "stop_waveform")
    assert out["samples_per_channel"] == 50000 and out["samples_lost"] == 0
    assert abs(out["channels"]["ch1"]["frequency_hz_estimate"] - 1e3) < 5
    assert abs(out["channels"]["ch1"]["peak_to_peak_v"] - 2.0) < 0.2


@analog_bench
def test_measure_frequency_counter(hw):
    call_tool(hw, "generate_waveform", channel=1, function="sine", frequency=12345.0, amplitude=1.0)
    out = call_tool(hw, "measure_frequency", channel=1, gate_time_s=0.2)
    call_tool(hw, "stop_waveform")
    assert abs(out["frequency_hz"] - 12345.0) < 2, out


@analog_bench
def test_pulse_width_trigger(hw):
    # 1 kHz square at 20 % duty: positive pulses of 200 us on channel 1
    call_tool(hw, "generate_waveform", channel=1, function="square", frequency=1e3, amplitude=1.0, symmetry=20)
    out = call_tool(hw, "capture_waveform", channels=[1], sampling_frequency=1e6, trigger_source="ch1",
                    trigger_type="pulse", trigger_level=0.0, trigger_edge="rising", trigger_length_s=100e-6,
                    trigger_length_condition="more", trigger_timeout_s=0, max_wait_s=2, save_csv=False)
    assert out["trigger"]["type"] == "pulse"
    with pytest.raises(RuntimeError, match="did not complete"):
        call_tool(hw, "capture_waveform", channels=[1], sampling_frequency=1e6, trigger_source="ch1",
                  trigger_type="pulse", trigger_level=0.0, trigger_edge="rising", trigger_length_s=500e-6,
                  trigger_length_condition="more", trigger_timeout_s=0, max_wait_s=0.5, save_csv=False)
    call_tool(hw, "stop_waveform")


@analog_bench
def test_generator_waits_for_pc_trigger(hw):
    out = call_tool(hw, "generate_waveform", channel=1, function="sine", frequency=1e3, amplitude=1.0,
                    trigger_source="pc")
    assert out["armed"] is True
    quiet = call_tool(hw, "capture_waveform", channels=[1], sampling_frequency=100e3, save_csv=False)
    assert quiet["channels"]["ch1"]["peak_to_peak_v"] < 0.2
    call_tool(hw, "send_pc_trigger")
    running = call_tool(hw, "capture_waveform", channels=[1], sampling_frequency=100e3, save_csv=False)
    assert running["channels"]["ch1"]["peak_to_peak_v"] > 1.8
    call_tool(hw, "stop_waveform")


def test_measure_pin_frequency_counter(hw):
    call_tool(hw, "generate_pattern", channel=0, kind="clock", frequency=1e6)
    out = call_tool(hw, "measure_pin_frequency", pin=0, gate_time_s=0.1)
    call_tool(hw, "stop_pattern")
    assert abs(out["frequency_hz"] - 1e6) < 100, out


def test_bus_pattern_counts_on_three_pins(hw):
    call_tool(hw, "generate_bus_pattern", pins=[0, 1, 2], words=list(range(8)), bit_rate=1e6)
    out = call_tool(hw, "capture_logic", pins=[0, 1, 2], trigger_pin=2, save_csv=False)
    call_tool(hw, "stop_pattern")
    freqs = [out["pins"][f"dio{p}"]["frequency_hz_estimate"] for p in (0, 1, 2)]
    for measured, expected in zip(freqs, (500e3, 250e3, 125e3)):
        assert abs(measured - expected) < 0.02 * expected, freqs


def test_record_logic_long_capture(hw):
    call_tool(hw, "generate_pattern", channel=0, kind="clock", frequency=100e3)
    out = call_tool(hw, "record_logic", pins=[0], sampling_frequency=1e6, duration_s=0.05, save_csv=False)
    call_tool(hw, "stop_pattern")
    assert out["samples"] == 50000 and out["samples_lost"] == 0
    assert abs(out["pins"]["dio0"]["transitions"] - 10000) <= 2
    assert abs(out["pins"]["dio0"]["frequency_hz_estimate"] - 100e3) < 100


def test_logic_trigger_gated_by_pin_level(hw):
    call_tool(hw, "generate_pattern", channel=0, kind="clock", frequency=1e6)
    call_tool(hw, "write_digital_pins", high=[1])
    out = call_tool(hw, "capture_logic", pins=[0, 1], trigger_rising=[0], trigger_high=[1], trigger_timeout_s=0,
                    max_wait_s=1, save_csv=False)
    assert out["pins"]["dio1"]["high_fraction"] == 1.0
    call_tool(hw, "write_digital_pins", low=[1])
    with pytest.raises(RuntimeError, match="did not complete"):
        call_tool(hw, "capture_logic", pins=[0, 1], trigger_rising=[0], trigger_high=[1], trigger_timeout_s=0,
                  max_wait_s=0.5, save_csv=False)
    call_tool(hw, "write_digital_pins", inputs=[1])
    call_tool(hw, "stop_pattern")


def test_pattern_waits_for_pc_trigger(hw):
    out = call_tool(hw, "generate_pattern", channel=0, kind="clock", frequency=1e6, trigger_source="pc")
    assert out["armed"] is True
    quiet = call_tool(hw, "capture_logic", pins=[0], save_csv=False)
    assert quiet["pins"]["dio0"]["transitions"] <= 1
    call_tool(hw, "send_pc_trigger")
    running = call_tool(hw, "capture_logic", pins=[0], save_csv=False)
    assert running["pins"]["dio0"]["transitions"] > 70
    call_tool(hw, "stop_pattern")


def test_trigger_pin_output_round_trip(hw):
    out = call_tool(hw, "set_trigger_pin", pin=1, source="pattern")
    assert out["direction"] == "output"
    out = call_tool(hw, "set_trigger_pin", pin=1, source="none")
    assert out["direction"] == "input"


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
