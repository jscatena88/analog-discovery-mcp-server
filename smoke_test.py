#!/usr/bin/env python3
"""Walk every instrument of a connected Analog Discovery 2 through the MCP tools.

    uv run smoke_test.py                 # checks that need nothing wired
    uv run smoke_test.py --jumpered      # + UART/SPI/I2C/external trigger (DIO0->8, DIO1->9, DIO2->10, T1->DIO15)
    uv run smoke_test.py --analog-bench  # + spectrum, sweep, analyzers (W1->1+, 0.22 uF to 2+, 1 k to GND)
    uv run smoke_test.py --diagnose      # analog wiring diagnosis: W1 DC step read on both channels

Exit status is 1 if any check fails. Each check prints PASS / FAIL / SKIP with a one-line detail.
"""

import argparse
import math
import sys
import time

import anyio

import server as srv
from ad2.analysis import estimate_frequency

RESULTS = []


def call(name, **arguments):
    from mcp.server.mcpserver.exceptions import ToolError
    try:
        return anyio.run(srv.mcp.call_tool, name, arguments).structured_content
    except ToolError as e:
        raise RuntimeError(str(e).split(": ", 1)[-1]) from e


def check(name, needs=None, enabled=True):
    """Decorator: run the check, record PASS/FAIL/SKIP, print one line."""
    def wrap(fn):
        if not enabled:
            RESULTS.append((name, "SKIP", f"needs --{needs}"))
            print(f"SKIP  {name:<28} needs --{needs}")
            return fn
        try:
            detail = fn() or ""
            RESULTS.append((name, "PASS", detail))
            print(f"PASS  {name:<28} {detail}")
        except Exception as e:  # noqa: BLE001 - report everything, keep going
            RESULTS.append((name, "FAIL", str(e)))
            print(f"FAIL  {name:<28} {e}")
        return fn
    return wrap


def expect(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jumpered", action="store_true", help="digital loopback jumpers are in place")
    parser.add_argument("--analog-bench", action="store_true", help="W1 -> 1+ and the RC circuit on 2+ are wired")
    parser.add_argument("--diagnose", action="store_true", help="only run the analog wiring diagnosis")
    parser.add_argument("--configuration", type=int, default=0, help="firmware configuration to open")
    args = parser.parse_args()

    if args.diagnose:
        return diagnose()

    @check("list_devices")
    def _():
        out = call("list_devices")
        expect(out["device_count"] >= 1, "no device found")
        d = out["devices"][0]
        return f"{d['name']} {d['serial']}, {len(d['configurations'])} configurations"

    @check("connect_device")
    def _():
        info = call("connect_device", configuration=args.configuration)
        return (f"{info['name']} WaveForms {info['waveforms_version']}, scope buffer "
                f"{info['analog_input']['max_buffer_size']}, logic buffer {info['digital_input']['max_buffer_size']}")

    @check("get_system_status")
    def _():
        s = call("get_system_status")["monitor"]
        expect(0 < s["temperature_c"] < 90, f"temperature {s['temperature_c']}")
        expect(4.0 < s["usb_voltage_v"] < 5.5, f"USB voltage {s['usb_voltage_v']}")
        return f"{s['temperature_c']:.1f} C, USB {s['usb_voltage_v']:.2f} V {s['usb_current_a']:.2f} A"

    @check("power_supplies")
    def _():
        out = call("set_power_supplies", positive_volts=3.3, negative_volts=-2.0, positive_enabled=True,
                   negative_enabled=True)
        expect(out["master_active"] and out["positive"]["active"], "supplies did not switch on")
        expect(abs(out["positive"]["voltage_v"] - 3.3) < 0.1, f"V+ reads {out['positive']['voltage_v']}")
        expect(abs(out["negative"]["voltage_v"] + 2.0) < 0.1, f"V- reads {out['negative']['voltage_v']}")
        off = call("disable_power_supplies")
        expect(not off["master_active"], "supplies did not switch off")
        return f"V+ {out['positive']['voltage_v']:.2f} V, V- {out['negative']['voltage_v']:.2f} V, then off"

    @check("capture_waveform")
    def _():
        out = call("capture_waveform", sampling_frequency=1e6, save_csv=False)
        expect(out["samples_per_channel"] >= 8192, "short capture")
        return f"{out['samples_per_channel']} samples x 2 channels at {out['sampling_frequency_hz']:g} S/s"

    @check("measure_voltage")
    def _():
        out = call("measure_voltage", channel=1)
        return f"ch1 {out['voltage_v']:+.3f} V"

    @check("generate_waveform + wavegen trigger")
    def _():
        call("generate_waveform", channel=1, function="sine", frequency=1e3, amplitude=1.0)
        out = call("capture_waveform", channels=[1], sampling_frequency=100e3, trigger_source="wavegen1",
                   trigger_timeout_s=0, max_wait_s=2, save_csv=False)
        call("stop_waveform")
        detail = "scope triggered from W1 start"
        if args.analog_bench:
            stats = out["channels"]["ch1"]
            expect(abs(stats["frequency_hz_estimate"] - 1e3) < 20, f"ch1 sees {stats['frequency_hz_estimate']} Hz")
            expect(abs(stats["peak_to_peak_v"] - 2.0) < 0.2, f"ch1 Vpp {stats['peak_to_peak_v']}")
            detail += f"; ch1 reads {stats['frequency_hz_estimate']:.0f} Hz {stats['peak_to_peak_v']:.2f} Vpp"
        return detail

    @check("capture_spectrum", "analog-bench", args.analog_bench)
    def _():
        call("generate_waveform", channel=1, function="sine", frequency=1e3, amplitude=1.0)
        out = call("capture_spectrum", channel=1, sampling_frequency=200e3, window="flattop", save_csv=False)
        call("stop_waveform")
        f = out["fundamental"]
        expect(abs(f["frequency_hz"] - 1e3) < 50 and 0.9 < f["amplitude_v"] < 1.1, f"fundamental {f}")
        return f"{f['frequency_hz']:.0f} Hz {f['amplitude_v']:.3f} V, THD {out['thd_percent']:.2f} %"

    @check("sweep_waveform", "analog-bench", args.analog_bench)
    def _():
        call("sweep_waveform", channel=1, start_frequency=1e3, stop_frequency=9e3, sweep_time_s=0.1)
        out = call("capture_waveform", channels=[1], sampling_frequency=100e3, trigger_source="wavegen1",
                   trigger_timeout_s=0, max_wait_s=2, preview_points=0, save_csv=False)
        call("stop_waveform")
        s, fs = out["preview"]["ch1"], out["sampling_frequency_hz"]
        first, last = estimate_frequency(s[: len(s) // 10], fs), estimate_frequency(s[-len(s) // 10:], fs)
        expect(first and last and first < 2500 and last > 6000, f"sweep read {first} -> {last} Hz")
        return f"{first:.0f} Hz at start, {last:.0f} Hz near end"

    @check("network_analyzer", "analog-bench", args.analog_bench)
    def _():
        out = call("network_analyzer", start_frequency=20, stop_frequency=100e3, steps=25, min_periods=8,
                   save_csv=False)
        s = out["summary"]
        expect(s.get("response") == "high_pass", f"response {s.get('response')}")
        return f"high-pass, -3 dB at {s['cutoff_hz_minus3db'][0]:.0f} Hz"

    @check("impedance_analyzer", "analog-bench", args.analog_bench)
    def _():
        out = call("impedance_analyzer", start_frequency=200, stop_frequency=20e3, steps=9, reference_ohm=1e3,
                   min_periods=8, save_csv=False)
        s = out["summary"]
        expect(s["dut_type"] == "capacitive", f"dut_type {s['dut_type']}")
        return f"capacitive, {s['series_capacitance_f'] * 1e6:.3f} uF"

    @check("write/read_digital_pins")
    def _():
        out = call("write_digital_pins", high=[4], low=[5])
        expect(out["levels"]["dio4"] == 1 and out["levels"]["dio5"] == 0, f"levels {out['high_pins']}")
        call("write_digital_pins", inputs=[4, 5])
        return "DIO4 high / DIO5 low read back, then released"

    @check("generate_pattern + capture_logic")
    def _():
        call("generate_pattern", channel=0, kind="clock", frequency=1e6)
        out = call("capture_logic", pins=[0], trigger_pin=0, pretrigger_samples=50, save_csv=False)
        call("stop_pattern")
        f = out["pins"]["dio0"]["frequency_hz_estimate"]
        expect(f and abs(f - 1e6) < 2e4, f"DIO0 reads {f} Hz")
        return f"1 MHz clock on DIO0 read back as {f / 1e6:.3f} MHz"

    @check("uart loopback", "jumpered", args.jumpered)
    def _():
        out = call("uart_send", data="smoke\r\n", tx_pin=0, rx_pin=8, baud_rate=115200)
        expect(out["received"]["text"] == "smoke\r\n", f"received {out['received']['text']!r}")
        return "DIO0 -> DIO8 at 115200 baud"

    @check("spi loopback", "jumpered", args.jumpered)
    def _():
        out = call("spi_transfer", write="a5 5a 00 ff", clock_pin=1, mosi_pin=0, miso_pin=8, select_pin=2)
        expect(out["read"]["hex"] == "a5 5a 00 ff", f"read {out['read']['hex']}")
        wide = call("spi_transfer", write=[0x1234, 0xBEEF], bits_per_word=16, clock_pin=1, mosi_pin=0, miso_pin=8,
                    select_pin=2)
        expect(wide["read_words"] == [0x1234, 0xBEEF], f"16-bit read {wide['read_words']}")
        return "8-bit and 16-bit words MOSI DIO0 -> MISO DIO8"

    @check("i2c bus check", "jumpered", args.jumpered)
    def _():
        try:
            out = call("i2c_scan", scl_pin=3, sda_pin=4)
        except RuntimeError as e:
            expect("pull-up" in str(e), f"unexpected error: {e}")
            return "floating bus correctly reported as not free"
        return f"bus free, {out['count']} device(s) found"

    @check("external trigger T1", "jumpered", args.jumpered)
    def _():
        call("generate_pattern", channel=15, kind="clock", frequency=1e3)
        call("capture_waveform", channels=[1], trigger_source="external1", trigger_timeout_s=0, max_wait_s=2,
             save_csv=False)
        call("stop_pattern")
        return "scope triggered from T1 (driven by DIO15)"

    @check("disconnect_device")
    def _():
        out = call("disconnect_device")
        expect(out["was_connected"], "was not connected")
        return "all outputs off, device released"

    failed = [r for r in RESULTS if r[1] == "FAIL"]
    passed = sum(1 for r in RESULTS if r[1] == "PASS")
    skipped = sum(1 for r in RESULTS if r[1] == "SKIP")
    print(f"\n{passed} passed, {len(failed)} failed, {skipped} skipped")
    return 1 if failed else 0


def diagnose():
    """Drive W1 with ±1 V DC and report what each scope channel sees."""
    print("Analog wiring diagnosis (W1 DC step, read on scope channels 1 and 2)")
    readings = {}
    for level in (1.0, -1.0):
        call("generate_waveform", channel=1, function="dc", frequency=1, amplitude=0, offset=level)
        time.sleep(0.4)
        out = call("capture_waveform", sampling_frequency=1e5, save_csv=False)
        readings[level] = (out["channels"]["ch1"]["mean_v"], out["channels"]["ch2"]["mean_v"])
        print(f"  W1 = {level:+.0f} V  ->  ch1 {readings[level][0]:+.3f} V   ch2 {readings[level][1]:+.3f} V")
    call("stop_waveform")
    call("disconnect_device")
    for channel, index in (("1", 0), ("2", 1)):
        hi, lo = readings[1.0][index], readings[-1.0][index]
        if hi > 0.8 and lo < -0.8:
            verdict = "follows W1 directly (DC-coupled, correct polarity)"
        elif hi < -0.8 and lo > 0.8:
            verdict = f"INVERTED: {channel}+ and {channel}- are swapped"
        elif abs(hi) < 0.1 and abs(lo) < 0.1:
            verdict = "no DC from W1 (open, or behind a series capacitor)"
        else:
            verdict = "partial coupling: check for a loose connection or a divider"
        print(f"  channel {channel}: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
