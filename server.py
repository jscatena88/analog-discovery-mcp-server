#!/usr/bin/env python3
"""MCP server for the Digilent Analog Discovery 2 (WaveForms SDK).

Tools are thin wrappers over the ad2 package; every tool returns a JSON object and
raises ToolError with an actionable message when the request cannot be fulfilled.
"""

import sys
from contextlib import contextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

try:
    from WF_SDK import device as wf_device
except (ImportError, OSError) as e:
    print("Error: WaveForms SDK (libdwf) or the WF_SDK package could not be loaded.", file=sys.stderr)
    print(f"Details: {e}", file=sys.stderr)
    sys.exit(1)

from ad2 import (analogio, analysis, analyzer, bytesutil, digital, export, i2c, jtag, logic, pattern, scope,
                 spectrum, spi, triggers, uart, wavegen)
from ad2.errors import AD2Error
from ad2.session import Session, enumerate_devices

INSTRUCTIONS = """\
Digilent Analog Discovery 2 (USB test instrument). All tools return JSON; failures raise an
error whose message says what to change.

Connectors (flywire colours in the Digilent harness):
- Scope inputs 1+/1- and 2+/2- (differential; tie the - input to GND for single-ended use).
- Generator outputs W1, W2 (±5 V, 10 MHz max). Power supplies V+ (0.5..5 V), V- (-0.5..-5 V).
- Digital pins DIO 0-15 (3.3 V logic, 5 V tolerant). Trigger pins T1/T2. Several GND pins.

Tools by instrument:
- Device: list_devices, connect_device (firmware configuration: 0 default, 1 = 16k scope buffer,
  3 = 16k logic buffers), get_device_info, get_system_status (temperature, USB power, supply
  state), disconnect_device (stops every output and releases the device).
- Scope: capture_waveform (1-2 channels, 8192 samples; edge/pulse/transition/window trigger from
  ch1/ch2, or start from wavegen, pattern, logic, pc or external T1-T4), record_waveform (long
  records streamed over USB, up to 2M samples), measure_voltage (DC voltmeter), measure_frequency,
  capture_spectrum (FFT, peaks, THD).
- Generator: generate_waveform (standard shapes, custom samples, bursts, phase, triggered start),
  sweep_waveform, stop_waveform.
- Supplies: set_power_supplies, disable_power_supplies.
- Digital: read_digital_pins, write_digital_pins (static I/O), generate_pattern, generate_bus_pattern
  (parallel words across pins) and stop_pattern (clocks to 50 MHz, pulses, custom bit streams,
  triggered start), capture_logic (16 pins, 100 MS/s, multi-pin level/edge trigger with count and
  pulse-length conditions), record_logic (long records, up to 4M samples), measure_pin_frequency.
- Triggers: set_trigger_pin drives T1/T2 from an instrument event; send_pc_trigger fires anything
  armed with trigger_source "pc". Every instrument can start from any other.
- JTAG (bit-banged on any four DIO pins, a few ms per bit): jtag_idcode, jtag_count_devices,
  jtag_scan (IR/DR shifts), jtag_tap_state (walk the TAP state machine), jtag_release.
- Protocols on any DIO pins: uart_send, uart_receive, spi_transfer, i2c_scan, i2c_write, i2c_read,
  i2c_write_read, i2c_spy.
- Analyzers: network_analyzer (Bode: gain/phase of ch2 relative to ch1 while W1 sweeps) and
  impedance_analyzer (R, X, C, L of a part in series with a known resistor).

Conventions: frequencies in Hz, voltages in V, times in s. Scope and generator channels are 1-based
(1, 2); DIO pins are 0-based (0-15). I2C addresses are 7-bit, not pre-shifted. Byte data may be
given as hex strings ("01 ab ff") or lists of ints.

Workflow:
- Tools auto-connect to the first device; call connect_device only to pick a device or configuration.
- Captures return statistics plus a decimated preview and write every sample to a CSV whose path is
  in the result (default ./captures); read that file when full resolution matters. Pick
  sampling_frequency so the record (buffer_size / rate) spans several periods of the signal.
- Outputs (generator, pattern, supplies) stay on until stopped; disconnect_device turns all off.
- To verify analog wiring, drive W1 with function "dc" and read the channel means with
  capture_waveform: a swapped +/- pair reads the opposite sign.
- Trigger waits are bounded by max_wait_s; a timeout error means no edge was seen, not a crash.

Caveats: the DIO pins are shared, and a pin driven by the pattern generator overrides its static
level; the logic analyzer reads back pins the device itself drives, so loopback needs no wiring.
Both analyzers drive W1 and take over both scope channels while running. I2C needs external
pull-ups on SDA and SCL. Supply readings settle a few hundred ms after a change. The WaveForms
desktop application must not have the device open at the same time.
"""

mcp = MCPServer("Analog Discovery 2", instructions=INSTRUCTIONS)
session = Session()


@contextmanager
def tool_errors():
    """Translate instrument-layer failures into ToolError so the client sees the message."""
    try:
        yield
    except AD2Error as e:
        raise ToolError(str(e)) from e
    except wf_device.error as e:
        raise ToolError(f"WaveForms SDK error: {e}") from e


# --------------------------------------------------------------------------- device

@mcp.tool()
def list_devices() -> dict[str, Any]:
    """List Digilent devices visible to the WaveForms runtime, with serial numbers and the firmware
    configurations each supports (scope/logic buffer sizes). Works without connecting. A device held
    open by the WaveForms application is still listed but cannot be opened."""
    with tool_errors():
        devices = enumerate_devices()
        return {"device_count": len(devices), "devices": devices}


@mcp.tool()
def connect_device(device_name: str | None = None, configuration: int = 0) -> dict[str, Any]:
    """Open a device. Other tools auto-connect with defaults, so this is only needed to choose a
    specific device or firmware configuration.

    Args:
        device_name: None for the first device, or e.g. "Analog Discovery 2".
        configuration: Index from list_devices. For the AD2: 0 = default (8k scope buffer),
            1 = 16k scope buffer, 3 = 16k logic analyzer/pattern buffers with a 512-sample scope.
    """
    with tool_errors():
        return session.connect(device_name, configuration)


@mcp.tool()
def disconnect_device() -> dict[str, Any]:
    """Stop the generator, turn off the power supplies, reset the scope and release the device."""
    with tool_errors():
        was_connected = session.connected
        session.disconnect()
        return {"disconnected": True, "was_connected": was_connected}


@mcp.tool()
def get_device_info() -> dict[str, Any]:
    """Capabilities of the connected device: channel counts, buffer sizes, voltage ranges, and
    which instruments are currently active."""
    with tool_errors():
        return session.info()


@mcp.tool()
def get_system_status() -> dict[str, Any]:
    """Board temperature, USB supply voltage and current, auxiliary input, and the state of the
    V+ / V- power supplies."""
    with tool_errors():
        dd = session.ensure()
        return {"monitor": analogio.system_monitor(dd), "supplies": analogio.supplies_status(dd)}


# --------------------------------------------------------------------------- power supplies

@mcp.tool()
def set_power_supplies(
    positive_volts: float | None = None,
    negative_volts: float | None = None,
    positive_enabled: bool | None = None,
    negative_enabled: bool | None = None,
    master_enabled: bool = True,
) -> dict[str, Any]:
    """Program the V+ and V- supplies. Parameters left as None are not changed. A rail outputs
    only when both its own enable and master_enabled are true.

    Args:
        positive_volts: V+ setpoint, 0.5 to 5.0 V.
        negative_volts: V- setpoint, -5.0 to -0.5 V (negative number).
        positive_enabled: Turn V+ on or off.
        negative_enabled: Turn V- on or off.
        master_enabled: Master switch for both rails (default on).
    """
    with tool_errors():
        dd = session.ensure()
        return analogio.set_supplies(dd, positive_volts, negative_volts,
                                     positive_enabled, negative_enabled, master_enabled)


@mcp.tool()
def disable_power_supplies() -> dict[str, Any]:
    """Turn off V+ and V- and the master supply switch."""
    with tool_errors():
        return analogio.disable_supplies(session.ensure())


# --------------------------------------------------------------------------- oscilloscope

@mcp.tool()
def capture_waveform(
    channels: list[int] | None = None,
    sampling_frequency: float = 20e6,
    buffer_size: int = 0,
    range_volts: float = 5.0,
    offset_volts: float = 0.0,
    trigger_source: str = "none",
    trigger_level: float = 0.0,
    trigger_edge: str = "rising",
    trigger_timeout_s: float = 1.0,
    max_wait_s: float = 5.0,
    preview_points: int = 200,
    save_csv: bool = True,
    csv_path: str | None = None,
    trigger_type: str = "edge",
    trigger_length_s: float = 0.0,
    trigger_length_condition: str = "more",
    trigger_holdoff_s: float = 0.0,
    trigger_hysteresis_v: float | None = None,
    filter: str = "decimate",
    attenuation: float = 1.0,
) -> dict[str, Any]:
    """Single-shot oscilloscope capture. Returns per-channel statistics (min, max, mean, Vpp, RMS,
    estimated frequency), a decimated preview, and the path of a CSV holding every sample.

    Args:
        channels: Scope channels to read, e.g. [1] or [1, 2]. Default: all channels.
        sampling_frequency: Sample rate in Hz (max 100e6). Capture length = buffer_size / rate.
        buffer_size: Samples per channel; 0 = device maximum (8192 by default).
        range_volts: Full-scale input range per channel (±range_volts). 5 V or 50 V on the AD2.
        offset_volts: Input offset in volts.
        trigger_source: none, ch1, ch2 (level trigger on that input), wavegen1, wavegen2, pattern
            (pattern generator start), logic (the logic analyzer's pin condition), pc
            (send_pc_trigger), external1..external4 (T1..T4 pins).
        trigger_level: Trigger threshold in volts (ch1/ch2 sources).
        trigger_edge: rising, falling or either. For pulse type: rising = positive pulse.
        trigger_timeout_s: Auto-trigger after this long without a trigger event (0 = never).
        max_wait_s: Hard limit on how long the tool waits for the acquisition to finish.
        preview_points: Number of decimated samples returned inline per channel.
        save_csv: Write the full capture to disk.
        csv_path: Where to write the CSV; default is a timestamped file under ./captures
            (override the directory with the AD2_CAPTURE_DIR environment variable).
        trigger_type: edge (default), pulse (pulse width vs trigger_length_s), transition (time to
            cross the hysteresis band vs trigger_length_s), window (leaving/entering level ± hysteresis).
        trigger_length_s: Duration compared by pulse/transition types.
        trigger_length_condition: more, less or timeout (for pulse/transition).
        trigger_holdoff_s: Minimum time between triggers.
        trigger_hysteresis_v: Trigger hysteresis / window half-width; device default when omitted.
        filter: Per-sample reduction when the ADC runs faster than the sample rate: decimate,
            average (lower noise) or min_max (keeps glitches).
        attenuation: Probe factor, e.g. 10 for a 10x probe; reported volts are scaled by it.
    """
    with tool_errors():
        fs, data = scope.capture(session, channels, sampling_frequency, buffer_size, range_volts,
                                 offset_volts, trigger_source, trigger_level, trigger_edge,
                                 trigger_timeout_s, max_wait_s, trigger_type, trigger_length_s,
                                 trigger_length_condition, trigger_holdoff_s, trigger_hysteresis_v,
                                 filter, attenuation)
        result = _capture_result(fs, data, preview_points, save_csv, csv_path)
        result.update({
            "range_volts": range_volts,
            "offset_volts": offset_volts,
            "trigger": {"source": trigger_source, "type": trigger_type, "level_v": trigger_level,
                        "edge": trigger_edge},
        })
        return result


def _capture_result(fs, data, preview_points, save_csv, csv_path, **extra):
    n = len(next(iter(data.values())))
    preview = {"time_s": analysis.time_axis(n, fs, preview_points)}
    channel_stats = {}
    for ch, samples in data.items():
        channel_stats[f"ch{ch}"] = analysis.summarize(samples, fs)
        preview[f"ch{ch}"] = analysis.decimate(samples, preview_points)
    path = export.write_capture_csv(csv_path, fs, data) if save_csv else None
    return {
        "sampling_frequency_hz": fs,
        "samples_per_channel": n,
        "duration_s": n / fs,
        "channels": channel_stats,
        "preview": preview,
        "csv_path": path,
        **extra,
    }


@mcp.tool()
def record_waveform(
    channels: list[int] | None = None,
    sampling_frequency: float = 100e3,
    duration_s: float = 1.0,
    range_volts: float = 5.0,
    offset_volts: float = 0.0,
    trigger_source: str = "none",
    trigger_level: float = 0.0,
    trigger_edge: str = "rising",
    trigger_timeout_s: float = 1.0,
    max_wait_s: float | None = None,
    preview_points: int = 500,
    save_csv: bool = True,
    csv_path: str | None = None,
    filter: str = "decimate",
    attenuation: float = 1.0,
) -> dict[str, Any]:
    """Record a waveform longer than the scope buffer by streaming samples over USB (up to
    2,000,000 samples per channel). Suited to sample rates up to about 1 MS/s; the result reports
    samples lost or corrupted if the host could not keep up. Statistics, preview and CSV as for
    capture_waveform.

    Args:
        channels: Scope channels, default all.
        sampling_frequency: Sample rate in Hz.
        duration_s: Record length in seconds (duration_s * sampling_frequency samples).
        range_volts, offset_volts, filter, attenuation: As for capture_waveform.
        trigger_source, trigger_level, trigger_edge, trigger_timeout_s: Edge trigger that starts the record.
        max_wait_s: Hard limit; default duration_s + 5.
        preview_points, save_csv, csv_path: Output options.
    """
    with tool_errors():
        fs, data, lost, corrupted = scope.record(session, channels, sampling_frequency, duration_s, range_volts,
                                                 offset_volts, trigger_source, trigger_level, trigger_edge,
                                                 trigger_timeout_s, max_wait_s, filter, attenuation)
        return _capture_result(fs, data, preview_points, save_csv, csv_path, samples_lost=lost,
                               samples_corrupted=corrupted, range_volts=range_volts,
                               trigger={"source": trigger_source, "level_v": trigger_level, "edge": trigger_edge})


@mcp.tool()
def measure_frequency(channel: int = 1, gate_time_s: float = 0.1, trigger_level: float = 0.0,
                      trigger_hysteresis_v: float | None = None, range_volts: float = 5.0,
                      offset_volts: float = 0.0) -> dict[str, Any]:
    """Frequency counter on a scope channel. Uses the hardware counter on devices that have one;
    on the Analog Discovery 2 (which has none) it counts level crossings in a 100 MS/s capture, or in
    a record spanning gate_time_s for signals below about 50 kHz. The result's `method` says which.

    Args:
        channel: Scope channel, 1 or 2.
        gate_time_s: Counting window; longer gives more resolution.
        trigger_level: Crossing level in volts.
        trigger_hysteresis_v: Noise rejection band; device default when omitted.
        range_volts, offset_volts: Input range and offset.
    """
    with tool_errors():
        return scope.count_frequency(session, channel, gate_time_s, trigger_level, trigger_hysteresis_v,
                                     range_volts, offset_volts)


@mcp.tool()
def capture_spectrum(
    channel: int = 1,
    sampling_frequency: float = 1e6,
    buffer_size: int = 0,
    range_volts: float = 5.0,
    offset_volts: float = 0.0,
    window: str = "hann",
    max_peaks: int = 10,
    preview_points: int = 200,
    save_csv: bool = True,
    csv_path: str | None = None,
) -> dict[str, Any]:
    """FFT of one scope capture. Returns the strongest peaks (frequency, volts, dBV), the
    fundamental's harmonics and THD, the noise floor, a max-hold preview and a CSV of every bin.
    Frequency resolution = sampling_frequency / buffer_size; the spectrum spans up to half the
    sample rate, so pick sampling_frequency a few times above the highest frequency of interest.

    Args:
        channel: Scope channel, 1 or 2.
        sampling_frequency: Sample rate in Hz (max 100e6).
        buffer_size: Samples; 0 = maximum (8192, or 16384 with configuration 1).
        range_volts, offset_volts: Input range and offset.
        window: hann (default), blackman, flattop (best amplitude accuracy) or rectangular.
        max_peaks: Peaks to list.
        preview_points: Max-hold decimated points returned inline.
        save_csv, csv_path: Full spectrum export.
    """
    with tool_errors():
        fs, data = scope.capture(session, [channel], sampling_frequency, buffer_size, range_volts, offset_volts)
        result = spectrum.analyze(data[channel], fs, window, max_peaks, preview_points, csv_path, save_csv)
        result["channel"] = channel
        return result


@mcp.tool()
def measure_voltage(channel: int, samples: int = 16) -> dict[str, Any]:
    """Voltmeter: read the instantaneous voltage on a scope channel, averaged over several quick
    samples. Use capture_waveform for anything time-varying.

    Args:
        channel: Scope channel, 1 or 2.
        samples: Number of readings to average.
    """
    with tool_errors():
        return scope.measure(session, channel, samples)


# --------------------------------------------------------------------------- waveform generator

@mcp.tool()
def generate_waveform(
    channel: int,
    function: str,
    frequency: float,
    amplitude: float,
    offset: float = 0.0,
    symmetry: float = 50.0,
    run_time_s: float = 0.0,
    wait_s: float = 0.0,
    repeat: int = 0,
    data: list[float] | None = None,
    phase_deg: float = 0.0,
    start_all_channels: bool = False,
    trigger_source: str = "none",
    trigger_slope: str = "rising",
    repeat_trigger: bool = False,
) -> dict[str, Any]:
    """Start a waveform on W1 or W2. The output keeps running until stop_waveform or
    disconnect_device. With a trigger_source the channel arms and starts on that event.

    Args:
        channel: Generator channel, 1 (W1) or 2 (W2).
        function: sine, square, triangle, ramp_up, ramp_down, dc, noise, pulse, trapezium,
            sine_power, or custom (one period given by data). For dc, the level is offset.
        frequency: Frequency in Hz (for custom: repetitions of the data per second).
        amplitude: Peak amplitude in volts; the output swings offset ± amplitude, within ±5 V.
        offset: DC offset in volts.
        symmetry: Duty cycle / symmetry in percent (square and pulse duty, triangle skew).
        run_time_s: Run for this many seconds per burst; 0 = continuous.
        wait_s: Delay before each burst starts, in seconds.
        repeat: Number of bursts; 0 = repeat forever.
        data: For custom: one period as samples between -1 and 1 (up to 4096), played at
            frequency * len(data) samples per second.
        phase_deg: Phase offset of the carrier in degrees (relative to the other channel when
            started together).
        start_all_channels: Restart every configured generator channel together so W1 and W2
            are phase-aligned.
        trigger_source: none (start now), pc (send_pc_trigger), scope (scope trigger detector),
            logic (logic analyzer pin condition), pattern, wavegen1/wavegen2 (the other channel),
            external1..external4.
        trigger_slope: rising, falling or either.
        repeat_trigger: Wait for the trigger before every repeat, not only the first.
    """
    with tool_errors():
        return wavegen.generate(session, channel, function, frequency, amplitude, offset,
                                symmetry, run_time_s, wait_s, repeat, data, phase_deg, start_all_channels,
                                trigger_source, trigger_slope, repeat_trigger)


@mcp.tool()
def sweep_waveform(
    channel: int,
    start_frequency: float,
    stop_frequency: float,
    sweep_time_s: float = 1.0,
    amplitude: float = 1.0,
    offset: float = 0.0,
    function: str = "sine",
    repeat: int = 0,
    trigger_source: str = "none",
    trigger_slope: str = "rising",
    repeat_trigger: bool = False,
) -> dict[str, Any]:
    """Linear frequency sweep on W1 or W2 from start_frequency to stop_frequency over sweep_time_s,
    repeating until stop_waveform (or `repeat` times). Use capture_waveform with
    trigger_source wavegen1/wavegen2 to catch the start of a sweep. trigger_source arms the
    sweep to start on an event, as for generate_waveform."""
    with tool_errors():
        return wavegen.sweep(session, channel, start_frequency, stop_frequency, sweep_time_s, amplitude,
                             offset, function, repeat, trigger_source, trigger_slope, repeat_trigger)


@mcp.tool()
def set_trigger_pin(pin: int, source: str = "none") -> dict[str, Any]:
    """Drive trigger pin T1 or T2 as an output from an instrument event, so the AD2 can trigger
    other equipment, or return it to an input with source "none".

    Args:
        pin: 1 for T1, 2 for T2.
        source: none (input), pc, scope, logic, scope_start, logic_start, pattern, wavegen1, wavegen2.
    """
    with tool_errors():
        return triggers.set_pin_output(session.ensure(), pin, source)


@mcp.tool()
def send_pc_trigger() -> dict[str, Any]:
    """Fire the software trigger. Anything armed with trigger_source "pc" (scope, logic analyzer,
    generator, pattern generator, or a trigger pin output) starts now."""
    with tool_errors():
        return triggers.pc_trigger(session.ensure())


@mcp.tool()
def stop_waveform(channel: int = 0) -> dict[str, Any]:
    """Stop generator output on one channel, or on all channels with channel=0."""
    with tool_errors():
        return wavegen.stop(session, channel)


# --------------------------------------------------------------------------- JTAG

@mcp.tool()
def jtag_idcode(tck_pin: int = 0, tms_pin: int = 1, tdi_pin: int = 2, tdo_pin: int = 3,
                max_devices: int = 8, reset_pin: int | None = None, reset_delay_s: float = 0.03,
                instruction: int | str | None = None, ir_length: int | None = None) -> dict[str, Any]:
    """Reset the JTAG TAP and read the IDCODE of every device in the chain (bit-banged over four
    DIO pins). Each device reports its 32-bit IDCODE, JEDEC manufacturer, part number and version;
    devices without an IDCODE register are listed as BYPASS. TRST, if the target has one, must be
    released externally. Pins are 3.3 V logic.

    Args:
        tck_pin, tms_pin, tdi_pin, tdo_pin: DIO pins wired to the target's TCK, TMS, TDI, TDO.
        max_devices: Stop after this many devices.
        reset_pin: DIO pin on the target's reset line: pulse it low first and read reset_delay_s
            after release. Needed for targets whose JTAG only answers shortly after reset.
        reset_delay_s: Wait after releasing reset.
        instruction, ir_length: Load this IR value before reading (single-device chains), for TAPs
            whose reset default is BYPASS, e.g. Xtensa IDCODE 0x1e with ir_length 5.
    """
    with tool_errors():
        return jtag.idcode(session, max_devices, tck_pin, tms_pin, tdi_pin, tdo_pin, reset_pin, reset_delay_s,
                           instruction, ir_length)


@mcp.tool()
def jtag_count_devices(tck_pin: int = 0, tms_pin: int = 1, tdi_pin: int = 2, tdo_pin: int = 3,
                       max_devices: int = 32, reset_pin: int | None = None,
                       reset_delay_s: float = 0.03) -> dict[str, Any]:
    """Count the devices in the JTAG chain using BYPASS (works even for devices without IDCODE).
    reset_pin/reset_delay_s pulse the target's reset first, as for jtag_idcode."""
    with tool_errors():
        return jtag.count_devices(session, tck_pin, tms_pin, tdi_pin, tdo_pin, max_devices, reset_pin, reset_delay_s)


@mcp.tool()
def jtag_probe_instructions(ir_length: int, tck_pin: int = 0, tms_pin: int = 1, tdi_pin: int = 2,
                            tdo_pin: int = 3, instructions: list[int] | None = None, max_dr_length: int = 64,
                            reset_pin: int | None = None, reset_delay_s: float = 0.03,
                            window_s: float = 0.35) -> dict[str, Any]:
    """Characterize an undocumented TAP: load each IR value and measure the data register it
    selects (length in bits and capture value). 32-bit registers starting with a 1 are decoded as
    IDCODEs. Single-device chains only. With reset_pin, the target is re-reset whenever the
    post-reset window (window_s) would run out, so the whole table can be gathered from a
    target that only answers briefly after reset.

    Args:
        ir_length: Instruction register length (find it from the IR capture pattern of jtag_scan).
        tck_pin, tms_pin, tdi_pin, tdo_pin: Pin assignment.
        instructions: IR values to try; default all 2 ** ir_length (at most 256 per call).
        max_dr_length: Give up on a register longer than this.
        reset_pin, reset_delay_s, window_s: Reset handling for windowed targets.
    """
    with tool_errors():
        return jtag.probe_instructions(session, ir_length, tck_pin, tms_pin, tdi_pin, tdo_pin, instructions,
                                       max_dr_length, reset_pin, reset_delay_s, window_s)


@mcp.tool()
def jtag_scan(register: str, length: int, value: int | str | None = None, end_state: str = "idle",
              tck_pin: int = 0, tms_pin: int = 1, tdi_pin: int = 2, tdo_pin: int = 3) -> dict[str, Any]:
    """Shift a value through the instruction (ir) or data (dr) register and return what came out
    on TDO. Goes to Shift-IR/DR from the current TAP state, shifts `length` bits LSB first, then
    exits through Update to end_state. Use an IR scan to select an instruction, then a DR scan to
    read or write its register.

    Args:
        register: "ir" or "dr".
        length: Number of bits to shift (1-4096).
        value: Bits to shift in, as an int or hex string; default 0.
        end_state: idle (default), pause_dr, pause_ir, update_dr, update_ir or reset.
        tck_pin, tms_pin, tdi_pin, tdo_pin: DIO pin assignment.
    """
    with tool_errors():
        return jtag.scan(session, register, length, value, end_state, tck_pin, tms_pin, tdi_pin, tdo_pin)


@mcp.tool()
def jtag_tap_state(state: str = "idle", tck_pin: int = 0, tms_pin: int = 1, tdi_pin: int = 2,
                   tdo_pin: int = 3) -> dict[str, Any]:
    """Walk the TAP state machine to a named state along the shortest TMS path and report the
    sequence used. States: reset, idle, select_dr, capture_dr, shift_dr, exit1_dr, pause_dr,
    exit2_dr, update_dr, and the same for _ir. "reset" clocks five TMS-high cycles."""
    with tool_errors():
        return jtag.tap_state(session, state, tck_pin, tms_pin, tdi_pin, tdo_pin)


@mcp.tool()
def jtag_release() -> dict[str, Any]:
    """Return the JTAG pins to inputs and forget the TAP state."""
    with tool_errors():
        return jtag.release(session)


# --------------------------------------------------------------------------- analyzers

@mcp.tool()
def network_analyzer(
    start_frequency: float = 100.0,
    stop_frequency: float = 1e6,
    steps: int = 31,
    amplitude: float = 1.0,
    offset: float = 0.0,
    log_scale: bool = True,
    min_periods: int = 16,
    max_wait_s: float = 5.0,
    save_csv: bool = True,
    csv_path: str | None = None,
) -> dict[str, Any]:
    """Bode plot: sweep W1 and measure the gain (dB) and phase (degrees) of scope channel 2
    relative to channel 1 at each frequency. Wire W1 and 1+ to the circuit input, 2+ to its
    output, 1- and 2- to ground. The summary gives the -3 dB cutoff(s) and the response type.
    Takes over W1 and both scope channels; low start frequencies take longer (min_periods / f).

    Args:
        start_frequency, stop_frequency: Sweep range in Hz.
        steps: Number of frequency points.
        amplitude: Stimulus amplitude in volts.
        offset: Stimulus DC offset in volts.
        log_scale: Logarithmic (true) or linear spacing.
        min_periods: Periods averaged per point.
        max_wait_s: Extra time allowed per point beyond min_periods / f.
        save_csv, csv_path: Export of all points.
    """
    with tool_errors():
        return analyzer.bode(session, start_frequency, stop_frequency, steps, amplitude, offset, log_scale,
                             min_periods, max_wait_s, csv_path, save_csv)


@mcp.tool()
def impedance_analyzer(
    start_frequency: float = 100.0,
    stop_frequency: float = 1e6,
    steps: int = 21,
    reference_ohm: float = 1e3,
    mode: str = "series_reference",
    amplitude: float = 1.0,
    offset: float = 0.0,
    log_scale: bool = True,
    min_periods: int = 16,
    max_wait_s: float = 5.0,
    save_csv: bool = True,
    csv_path: str | None = None,
) -> dict[str, Any]:
    """Measure a component's impedance across frequency using a known reference resistor:
    |Z|, phase, R, X, series/parallel C and L, dissipation and Q per point, plus a summary that
    classifies the device (capacitive / inductive / resistive) with its median value.

    Wiring for mode series_reference: W1 and 1+ to one end of the device, the other end to 2+
    and to the reference resistor, resistor's other end to ground, 1- and 2- to ground.
    Mode shunt_reference swaps the resistor and the device. Choose reference_ohm close to the
    expected |Z| in the frequency range of interest.
    """
    with tool_errors():
        return analyzer.impedance(session, start_frequency, stop_frequency, steps, reference_ohm, mode,
                                 amplitude, offset, log_scale, min_periods, max_wait_s, csv_path, save_csv)


# --------------------------------------------------------------------------- static digital I/O

@mcp.tool()
def read_digital_pins() -> dict[str, Any]:
    """Read the level of every DIO pin (0-15). Pins the device drives read back their own level.
    Also reports which pins are currently configured as outputs."""
    with tool_errors():
        return digital.read(session.ensure())


@mcp.tool()
def write_digital_pins(
    high: list[int] | None = None,
    low: list[int] | None = None,
    inputs: list[int] | None = None,
) -> dict[str, Any]:
    """Drive DIO pins as static outputs or release them. Pins not mentioned keep their state.

    Args:
        high: Pins to drive to logic 1 (3.3 V).
        low: Pins to drive to logic 0.
        inputs: Pins to release to high impedance (input).
    """
    with tool_errors():
        return digital.write(session.ensure(), high, low, inputs)


# --------------------------------------------------------------------------- pattern generator

@mcp.tool()
def generate_pattern(
    channel: int,
    kind: str = "clock",
    frequency: float = 1e3,
    duty_cycle: float = 50.0,
    data: str | list[int] | None = None,
    idle: str = "initial",
    run_time_s: float = 0.0,
    wait_s: float = 0.0,
    repeat: int = 0,
    output: str = "push_pull",
    trigger_source: str = "none",
    trigger_slope: str = "rising",
    repeat_trigger: bool = False,
) -> dict[str, Any]:
    """Start a digital pattern on a DIO pin. Runs until stop_pattern or disconnect_device.

    Args:
        channel: DIO pin number, 0-15.
        kind: clock (50 % square wave), pulse (square wave with duty_cycle), custom (bit stream
            from data, repeated), random (noise at the given bit rate).
        frequency: Output frequency in Hz for clock/pulse (max 50e6); bit rate for custom/random.
        duty_cycle: Percent high time for kind=pulse.
        data: Bits for kind=custom, first bit sent first: "10110010", "0b1011", or [1,0,1,1].
            Up to 1024 bits in the default configuration.
        idle: Level between bursts and after stopping: initial, low, high, high_impedance.
        run_time_s: Burst length in seconds; 0 = continuous. Shared by all pattern channels.
        wait_s: Delay before each burst. Shared by all pattern channels.
        repeat: Number of bursts; 0 = forever. Shared by all pattern channels.
        output: Driver type: push_pull, open_drain, open_source or three_state.
        trigger_source: none (start now), pc, scope, logic, wavegen1, wavegen2, external1..4.
            Shared by all pattern channels.
        trigger_slope: rising, falling or either.
        repeat_trigger: Wait for the trigger before every repeat.
    """
    with tool_errors():
        return pattern.generate(session, channel, kind, frequency, duty_cycle, data, idle,
                                run_time_s, wait_s, repeat, output, trigger_source, trigger_slope,
                                repeat_trigger)


@mcp.tool()
def generate_bus_pattern(
    pins: list[int],
    words: list[int],
    bit_rate: float = 1e6,
    idle: str = "initial",
    run_time_s: float = 0.0,
    wait_s: float = 0.0,
    repeat: int = 0,
    output: str = "push_pull",
    trigger_source: str = "none",
    trigger_slope: str = "rising",
    repeat_trigger: bool = False,
) -> dict[str, Any]:
    """Play a sequence of words across several DIO pins in parallel: bit i of each word drives
    pins[i]. Emulates a parallel bus, a counter, or a multi-signal timing pattern. The sequence
    repeats until stop_pattern unless run_time_s/repeat limit it.

    Args:
        pins: DIO pins, least significant bit first, e.g. [0, 1, 2, 3] for a 4-bit bus.
        words: Values to play, one per bit period, each below 2 ** len(pins). Up to 1024.
        bit_rate: Words per second.
        idle, run_time_s, wait_s, repeat, output, trigger_source, trigger_slope, repeat_trigger:
            As for generate_pattern.
    """
    with tool_errors():
        return pattern.generate_bus(session, pins, words, bit_rate, idle, run_time_s, wait_s, repeat, output,
                                    trigger_source, trigger_slope, repeat_trigger)


@mcp.tool()
def stop_pattern(channel: int | None = None) -> dict[str, Any]:
    """Stop the pattern generator on one DIO pin, or on every pin when channel is omitted."""
    with tool_errors():
        return pattern.stop(session, channel)


# --------------------------------------------------------------------------- logic analyzer

@mcp.tool()
def capture_logic(
    pins: list[int] | None = None,
    sampling_frequency: float = 100e6,
    buffer_size: int = 0,
    trigger_pin: int | None = None,
    trigger_edge: str = "rising",
    pretrigger_samples: int = 0,
    trigger_timeout_s: float = 1.0,
    max_wait_s: float = 5.0,
    max_edges: int = 100,
    save_csv: bool = True,
    csv_path: str | None = None,
    trigger_rising: list[int] | None = None,
    trigger_falling: list[int] | None = None,
    trigger_high: list[int] | None = None,
    trigger_low: list[int] | None = None,
    trigger_count: int = 1,
    trigger_length_min_s: float = 0.0,
    trigger_length_max_s: float = 0.0,
    trigger_source: str = "logic",
    trigger_slope: str = "rising",
) -> dict[str, Any]:
    """Single-shot logic analyzer capture of DIO 0-15. Returns, per pin, the initial and final
    level, transition count, high fraction, estimated frequency and the first transitions as
    [time_s, new_level] pairs, plus the path of a CSV with every sample.

    Args:
        pins: Pins to report on (default: all 16). All pins are always captured and saved.
        sampling_frequency: Sample rate in Hz, up to 100e6. Duration = buffer_size / rate.
        buffer_size: Samples; 0 = configuration maximum (4096 by default, 16384 with configuration 3).
        trigger_pin: Pin whose edge starts the capture; omit (and give no other trigger lists)
            to capture immediately.
        trigger_edge: rising, falling or either, for trigger_pin.
        pretrigger_samples: Samples to keep from before the trigger.
        trigger_timeout_s: Auto-trigger after this long without a trigger; 0 = never.
        max_wait_s: Hard limit on how long the tool waits.
        max_edges: Maximum transitions listed inline per pin.
        save_csv: Write the full capture to disk.
        csv_path: Where to write it; default is a timestamped file under ./captures.
        trigger_rising, trigger_falling: Pins where an edge triggers (any one of them).
        trigger_high, trigger_low: Pins that must be at that level when the edge occurs.
        trigger_count: Trigger on the Nth occurrence of the condition.
        trigger_length_min_s, trigger_length_max_s: The condition must hold for at least min
            (and, if max > 0, at most max) seconds, e.g. to catch a pulse of a given width.
        trigger_source: logic (the pin condition above), or scope, pattern, wavegen1, wavegen2, pc,
            external1..4 to start from another instrument.
        trigger_slope: Edge for non-logic sources.
    """
    with tool_errors():
        dd = session.ensure()
        pins = digital.check_pins(dd, pins, "pins") or list(range(digital.pin_count(dd)))
        fs, samples, trigger_index = logic.capture(session, sampling_frequency, buffer_size, trigger_pin,
                                                   trigger_edge, pretrigger_samples, trigger_timeout_s,
                                                   max_wait_s, trigger_rising, trigger_falling, trigger_high,
                                                   trigger_low, trigger_count, trigger_length_min_s,
                                                   trigger_length_max_s, trigger_source, trigger_slope)
        return _logic_result(dd, fs, samples, pins, max_edges, save_csv, csv_path,
                             trigger=None if trigger_index is None else
                             {"source": trigger_source, "pin": trigger_pin, "edge": trigger_edge,
                              "index": trigger_index, "time_s": trigger_index / fs})


def _logic_result(dd, fs, samples, pins, max_edges, save_csv, csv_path, **extra):
    summaries = {f"dio{p}": logic.summarize_pin(samples, p, fs, max_edges) for p in pins}
    path = logic.write_logic_csv(csv_path, fs, samples, digital.pin_count(dd)) if save_csv else None
    return {
        "sampling_frequency_hz": fs,
        "samples": len(samples),
        "duration_s": len(samples) / fs,
        "active_pins": [p for p in pins if summaries[f"dio{p}"]["transitions"]],
        "pins": summaries,
        "csv_path": path,
        **extra,
    }


@mcp.tool()
def record_logic(
    pins: list[int] | None = None,
    sampling_frequency: float = 1e6,
    duration_s: float = 0.1,
    trigger_pin: int | None = None,
    trigger_edge: str = "rising",
    trigger_timeout_s: float = 1.0,
    max_wait_s: float | None = None,
    max_edges: int = 100,
    save_csv: bool = True,
    csv_path: str | None = None,
    trigger_rising: list[int] | None = None,
    trigger_falling: list[int] | None = None,
    trigger_high: list[int] | None = None,
    trigger_low: list[int] | None = None,
    trigger_source: str = "logic",
) -> dict[str, Any]:
    """Record DIO 0-15 for longer than the logic buffer by streaming over USB (up to 4,000,000
    samples). Suited to sample rates up to a few MS/s; the result reports lost samples if the
    host could not keep up. Per-pin summaries and CSV as for capture_logic.

    Args:
        pins: Pins to summarize; all pins are captured and saved.
        sampling_frequency: Sample rate in Hz.
        duration_s: Record length in seconds.
        trigger_pin, trigger_edge, trigger_rising, trigger_falling, trigger_high, trigger_low,
            trigger_timeout_s, trigger_source: Trigger that starts the record, as for capture_logic.
        max_wait_s: Hard limit; default duration_s + 5.
        max_edges, save_csv, csv_path: Output options.
    """
    with tool_errors():
        dd = session.ensure()
        pins = digital.check_pins(dd, pins, "pins") or list(range(digital.pin_count(dd)))
        fs, samples, lost, corrupted = logic.record(session, sampling_frequency, duration_s, trigger_pin,
                                                    trigger_edge, trigger_timeout_s, max_wait_s, trigger_rising,
                                                    trigger_falling, trigger_high, trigger_low, trigger_source)
        return _logic_result(dd, fs, samples, pins, max_edges, save_csv, csv_path,
                             samples_lost=lost, samples_corrupted=corrupted)


@mcp.tool()
def measure_pin_frequency(pin: int = 0, gate_time_s: float = 0.1) -> dict[str, Any]:
    """Frequency counter on a DIO pin: hardware counter where the device has one, otherwise rising
    edges counted in a 100 MS/s capture or in a record spanning gate_time_s (AD2).

    Args:
        pin: DIO pin number, 0-15.
        gate_time_s: Counting window; longer gives more resolution.
    """
    with tool_errors():
        return logic.count_frequency(session, pin, gate_time_s)


# --------------------------------------------------------------------------- UART

@mcp.tool()
def uart_send(
    data: str,
    data_format: str = "text",
    tx_pin: int = 0,
    rx_pin: int = 1,
    baud_rate: float = 9600,
    data_bits: int = 8,
    parity: str = "none",
    stop_bits: float = 1.0,
    receive_timeout_s: float = 0.5,
) -> dict[str, Any]:
    """Transmit bytes on tx_pin and collect whatever arrives on rx_pin for receive_timeout_s
    (stopping early once the reply goes quiet). Bytes that arrived before the send are discarded.

    Args:
        data: Text, or hex bytes such as "01 ab ff" when data_format is "hex".
        data_format: "text" (UTF-8) or "hex".
        tx_pin: DIO pin driven as TX (idle high).
        rx_pin: DIO pin sampled as RX.
        baud_rate: Bits per second, e.g. 9600 or 115200.
        data_bits: 5 to 8.
        parity: none, even, odd, mark or space.
        stop_bits: 1, 1.5 or 2.
        receive_timeout_s: How long to wait for a reply; 0 sends without listening.
    """
    with tool_errors():
        payload = bytesutil.parse_text_or_hex(data, data_format)
        return uart.send(session, payload, tx_pin, rx_pin, baud_rate, data_bits, parity, stop_bits,
                         receive_timeout_s)


@mcp.tool()
def uart_receive(
    rx_pin: int = 1,
    tx_pin: int = 0,
    baud_rate: float = 9600,
    data_bits: int = 8,
    parity: str = "none",
    stop_bits: float = 1.0,
    timeout_s: float = 2.0,
    idle_timeout_s: float = 0.2,
    max_bytes: int = 8192,
) -> dict[str, Any]:
    """Listen on rx_pin without transmitting (serial spy / console capture). Returns once max_bytes
    arrive, once data stops for idle_timeout_s, or after timeout_s. Bytes received since the last
    UART call are included. The TX pin is reserved but not driven unless uart_send is used.

    Args:
        rx_pin: DIO pin to listen on.
        tx_pin: Any unused DIO pin (the engine requires one).
        baud_rate, data_bits, parity, stop_bits: Line settings, as for uart_send.
        timeout_s: Maximum wait, up to 60 s.
        idle_timeout_s: Return after this much silence following data; 0 waits for timeout_s.
        max_bytes: Stop after this many bytes.
    """
    with tool_errors():
        return uart.receive(session, tx_pin, rx_pin, baud_rate, data_bits, parity, stop_bits,
                            timeout_s, idle_timeout_s, max_bytes)


# --------------------------------------------------------------------------- SPI

@mcp.tool()
def spi_transfer(
    write: str | list[int] | None = None,
    read_count: int = 0,
    clock_pin: int = 1,
    mosi_pin: int = 2,
    miso_pin: int = 3,
    select_pin: int = 0,
    frequency: float = 1e6,
    mode: int = 0,
    msb_first: bool = True,
    bits_per_word: int = 8,
    fill_byte: int = 0,
) -> dict[str, Any]:
    """SPI master full-duplex transfer: select asserted, max(len(write), read_count) words clocked
    out while the same number are read back, select released.

    Args:
        write: Words to send. For 8-bit (or narrower) words: hex ("9f 00 00") or a list of ints.
            For 16/24/32-bit words: a list of ints. Padded with fill_byte when read_count is larger.
        read_count: Minimum number of words to clock (and read back).
        clock_pin, mosi_pin, miso_pin, select_pin: DIO pin assignments.
        frequency: SCK frequency in Hz.
        mode: SPI mode 0-3 (CPOL/CPHA).
        msb_first: Bit order (for wide words also the byte order on the wire).
        bits_per_word: 1 to 8, or 16, 24, 32. Wide words are returned in read_words.
        fill_byte: Byte value sent while only reading.
    """
    with tool_errors():
        return spi.transfer(session, write, read_count, bits_per_word, fill_byte, clock_pin, mosi_pin,
                            miso_pin, select_pin, frequency, mode, msb_first, True)


# --------------------------------------------------------------------------- I2C

@mcp.tool()
def i2c_scan(scl_pin: int = 0, sda_pin: int = 1, rate: float = 100e3,
             first: int = 0x08, last: int = 0x77) -> dict[str, Any]:
    """Probe every 7-bit address in [first, last] and list the ones that acknowledge. Fails with a
    wiring hint if SDA or SCL is held low (missing pull-ups or unpowered slave).

    Args:
        scl_pin, sda_pin: DIO pins for clock and data.
        rate: Bus clock in Hz (100e3 standard, 400e3 fast).
        first, last: Address range to scan.
    """
    with tool_errors():
        return i2c.scan(session, scl_pin, sda_pin, rate, first, last)


@mcp.tool()
def i2c_write(address: int, data: str | list[int], scl_pin: int = 0, sda_pin: int = 1,
              rate: float = 100e3) -> dict[str, Any]:
    """Write bytes to a 7-bit I2C address. The result says whether every byte was acknowledged.

    Args:
        address: 7-bit address (0x00-0x7F), not pre-shifted.
        data: Bytes as hex ("00 ff") or a list of ints; may be empty to only address the device.
        scl_pin, sda_pin, rate: Bus settings.
    """
    with tool_errors():
        return i2c.write(session, address, data, scl_pin, sda_pin, rate)


@mcp.tool()
def i2c_read(address: int, count: int, scl_pin: int = 0, sda_pin: int = 1,
             rate: float = 100e3) -> dict[str, Any]:
    """Read count bytes from a 7-bit I2C address."""
    with tool_errors():
        return i2c.read(session, address, count, scl_pin, sda_pin, rate)


@mcp.tool()
def i2c_write_read(address: int, data: str | list[int], count: int, scl_pin: int = 0,
                   sda_pin: int = 1, rate: float = 100e3) -> dict[str, Any]:
    """Write bytes (typically a register number) then read count bytes after a repeated start,
    the usual way to read a register from a sensor."""
    with tool_errors():
        return i2c.write_read(session, address, data, count, scl_pin, sda_pin, rate)


@mcp.tool()
def i2c_spy(duration_s: float = 2.0, max_messages: int = 100, scl_pin: int = 0, sda_pin: int = 1,
            rate: float = 100e3) -> dict[str, Any]:
    """Passively decode I2C traffic between other devices on the bus for duration_s seconds.
    Each message has the address, direction, data bytes, NAK position and stop flag."""
    with tool_errors():
        return i2c.spy(session, scl_pin, sda_pin, rate, duration_s, max_messages)


if __name__ == "__main__":
    mcp.run()
