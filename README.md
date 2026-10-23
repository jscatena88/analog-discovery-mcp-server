# Analog Discovery 2 MCP Server

An MCP (Model Context Protocol) server for controlling the Digilent Analog Discovery 2 device. This server provides tools to interact with the Analog Discovery 2's oscilloscope, waveform generator, and other functions through the MCP protocol.

## Features

- **Oscilloscope**: single-shot capture on one or both channels with edge triggering, per-channel statistics (min, max, mean, Vpp, RMS, estimated frequency), a decimated preview, and the full record saved as CSV
- **Voltmeter**: averaged DC readings on either scope channel
- **Waveform generator**: sine, square, triangle, ramp, DC, noise, pulse, trapezium and sine-power outputs with symmetry/duty and burst (run, wait, repeat) control on W1 and W2
- **Power supplies**: program and switch V+ / V- with range validation and setpoint/measured readback
- **System monitor**: board temperature, USB voltage and current, auxiliary input
- **Static digital I/O**: read all 16 DIO pins, drive pins high/low, release them to input
- **Pattern generator**: clocks up to 50 MHz, pulses with duty cycle, custom bit streams and random data on any DIO pin, with burst control
- **Logic analyzer**: 16-channel capture up to 100 MS/s with edge trigger and pre-trigger, per-pin transition/frequency summaries, and the full record saved as CSV
- **UART**: send with reply capture, or listen-only, on any two DIO pins
- **SPI master**: full-duplex transfers on any four DIO pins, modes 0-3, 1-8 bit words
- **I2C master and spy**: address scan, write, read, write-then-read, and passive bus decoding (external pull-ups required)
- **External triggers**: T1/T2 usable as scope trigger sources
- **Spectrum analyzer**: FFT of a capture with peaks, harmonics, THD and noise floor
- **Network analyzer**: Bode gain/phase sweeps with -3 dB cutoff and response classification
- **Impedance analyzer**: |Z|, phase, R, X, C and L of a component against a reference resistor
- **Generator extras**: custom sample data, phase offset, phase-aligned start of both channels, linear frequency sweeps
- **Device management**: enumeration with serial numbers and firmware configurations, explicit configuration selection (for example the 16k-sample scope buffer), auto-connect on first use, and a disconnect that turns every output off
- **Hardware-free tests**: a fake `libdwf` lets the whole tool surface run under pytest without a device

## Prerequisites

### 1. Install WaveForms SDK

Before using this MCP server, you must install the WaveForms SDK from Digilent:

1. Download WaveForms from [Digilent's website](https://digilent.com/reference/software/waveforms/waveforms-sdk/start)
2. Install WaveForms.app to `/Applications/`
3. **CRITICAL for macOS**: Copy `dwf.framework` to the system frameworks directory:
   ```bash
   sudo cp -R /Applications/WaveForms.app/Contents/Frameworks/dwf.framework /Library/Frameworks/
   ```
   This step is required for the SDK to properly enumerate devices. Without it, you may encounter ERC 3090 errors.
4. Ensure the Analog Discovery 2 is connected via USB

### 2. Python Requirements

- Python 3.11 or later (`.python-version` pins 3.12; `uv` will download it if needed)
- [uv](https://docs.astral.sh/uv/) for dependency and virtual environment management

## Installation

1. Clone or download this repository:
```bash
cd analog-discovery-mcp-server
```

2. Create the virtual environment and install dependencies (this reads `pyproject.toml` and `uv.lock`):
```bash
uv sync
```

3. Verify the installation:
```bash
uv run server.py
```
The server will start and wait for MCP client connections via stdio. Press Ctrl+C to stop it.

If you prefer not to use `uv`, a plain venv works too:
```bash
python3 -m venv .venv && . .venv/bin/activate && pip install "mcp>=2.3,<3"
```

## MCP Server Configuration

The server speaks MCP over stdio. Every client below launches it with `uv run server.py`, which
creates or updates the virtual environment automatically on first run.

### For Claude Code

The repository ships a project-scoped `.mcp.json`, so opening Claude Code inside this directory is enough.
Claude Code asks once whether to trust the project's MCP servers; approve it and the tools appear.
Check with:
```bash
claude mcp list
```

To make the server available from any directory instead, register it at user scope:
```bash
claude mcp add --scope user analog-discovery-2 -- uv run --directory /absolute/path/to/analog-discovery-mcp-server server.py
```

### For Cursor

`.cursor/mcp.json` is included and uses the same `uv run` command.

### For Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or
`%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "analog-discovery-2": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/analog-discovery-mcp-server", "server.py"]
    }
  }
}
```

### For Other MCP Clients

Configure your client to run the following command with stdio transport:
```bash
uv run --directory /path/to/analog-discovery-mcp-server server.py
```

## Available Tools

Every tool returns a JSON object. Failures (bad arguments, device busy, trigger timeout) come back as tool errors with an actionable message. Tools auto-connect to the first device on first use.

### Device

| Tool | Purpose |
|---|---|
| `list_devices` | Enumerate devices with serial number, type and the firmware configurations each supports (channel counts and buffer sizes). Works without connecting. |
| `connect_device(device_name?, configuration=0)` | Open a specific device and/or firmware configuration. AD2 configurations: 0 default (8k scope buffer), 1 = 16k scope buffer, 3 = 16k logic buffers with a 512-sample scope. |
| `disconnect_device` | Stop the generator, turn off the supplies, reset the scope and release the device. |
| `get_device_info` | Channel counts, buffer sizes, voltage ranges and which instruments are active. |
| `get_system_status` | Board temperature, USB voltage/current, auxiliary input, and the V+ / V- supply state. |

### Oscilloscope

**`capture_waveform`** performs one acquisition and returns per-channel statistics, a decimated preview, and the path of a CSV containing every sample.

| Parameter | Default | Notes |
|---|---|---|
| `channels` | all | List of scope channels, e.g. `[1]` or `[1, 2]` |
| `sampling_frequency` | `20e6` | Hz, up to 100e6 |
| `buffer_size` | `0` | Samples per channel; 0 = device maximum |
| `range_volts` | `5.0` | Full-scale input range (±range); 5 V or 50 V on the AD2 |
| `offset_volts` | `0.0` | Input offset |
| `trigger_source` | `"none"` | `none`, `ch1`, `ch2`, `wavegen1`, `wavegen2`, `external1`..`external4` |
| `trigger_level` | `0.0` | Volts (analog sources) |
| `trigger_edge` | `"rising"` | `rising` or `falling` |
| `trigger_timeout_s` | `1.0` | Auto-trigger after this long without an event; 0 = never |
| `max_wait_s` | `5.0` | Hard limit on how long the tool waits for the acquisition |
| `preview_points` | `200` | Decimated samples returned inline per channel |
| `save_csv` | `true` | Write the full capture to disk |
| `csv_path` | auto | Default: timestamped file under `./captures` (override the directory with `AD2_CAPTURE_DIR`) |

Result shape:
```json
{
  "sampling_frequency_hz": 1000000.0,
  "samples_per_channel": 8192,
  "duration_s": 0.008192,
  "trigger": {"source": "ch1", "level_v": 0.5, "edge": "rising"},
  "channels": {
    "ch1": {"samples": 8192, "min_v": -1.0, "max_v": 1.0, "mean_v": 0.0, "peak_to_peak_v": 2.0,
            "rms_v": 0.707, "ac_rms_v": 0.707, "frequency_hz_estimate": 1000.0}
  },
  "preview": {"time_s": [0.0, "..."], "ch1": [0.0, "..."]},
  "csv_path": "/path/to/captures/capture_20261023_130014_093954.csv"
}
```
Signals under 20 mV peak-to-peak are treated as flat and get no frequency estimate.

**`measure_voltage(channel, samples=16)`** returns an averaged instantaneous reading for a scope channel.

### Waveform generator

**`generate_waveform(channel, function, frequency, amplitude, offset=0, symmetry=50, run_time_s=0, wait_s=0, repeat=0)`**

- `channel`: 1 (W1) or 2 (W2)
- `function`: `sine`, `square`, `triangle`, `ramp_up`, `ramp_down`, `dc`, `noise`, `pulse`, `trapezium`, `sine_power`. For `dc` the level is `offset`.
- `amplitude` + |`offset`| must stay within ±5 V; the tool refuses otherwise.
- `symmetry`: duty cycle / skew in percent.
- `run_time_s`, `wait_s`, `repeat`: burst control; zeros mean continuous.

Output runs until **`stop_waveform(channel=0)`** (0 = all channels) or `disconnect_device`.

### Power supplies

**`set_power_supplies(positive_volts?, negative_volts?, positive_enabled?, negative_enabled?, master_enabled=true)`** programs V+ (0.5 to 5 V) and V- (-5 to -0.5 V). Parameters left out are unchanged; out-of-range voltages are rejected rather than clamped. A rail outputs only when its own enable and the master switch are both on. The result reports setpoints (`enabled`, `voltage_setpoint_v`) and measured values (`active`, `voltage_v`, `current_a`).

**`disable_power_supplies`** turns both rails and the master switch off.

### Digital I/O (DIO 0-15)

The 16 DIO pins are 3.3 V logic (5 V tolerant) and are shared by three instruments. A pin driven by the pattern generator overrides its static level, and the logic analyzer reads back pins the device itself is driving, so loopback experiments need no wiring.

**`read_digital_pins`** returns every pin's level, the list of high pins and the list of pins configured as outputs.

**`write_digital_pins(high?, low?, inputs?)`** drives the listed pins to 1 or 0, or releases them to high impedance. Pins not mentioned keep their state.

**`generate_pattern(channel, kind="clock", frequency=1e3, duty_cycle=50, data?, idle="initial", run_time_s=0, wait_s=0, repeat=0)`**

- `kind`: `clock` (50 % square), `pulse` (square with `duty_cycle`), `custom` (bit stream from `data`, repeated), `random`
- `frequency`: output frequency for clock/pulse (max 50 MHz); bit rate for custom/random
- `data`: `"10110010"`, `"0b1011"` or `[1, 0, 1, 1]`, first bit sent first, up to 1024 bits
- `idle`: `initial`, `low`, `high`, `high_impedance`
- `run_time_s`, `wait_s`, `repeat`: burst control, shared by all pattern channels

The result reports the frequency actually produced. **`stop_pattern(channel?)`** stops one pin or all of them.

**`capture_logic`** performs one logic analyzer acquisition.

| Parameter | Default | Notes |
|---|---|---|
| `pins` | all | Pins to summarize; every pin is always captured and saved |
| `sampling_frequency` | `100e6` | Hz, up to 100e6 |
| `buffer_size` | `0` | Samples; 0 = maximum (4096 by default, 16384 with configuration 3) |
| `trigger_pin` | none | Pin whose edge starts the capture |
| `trigger_edge` | `"rising"` | `rising`, `falling` or `either` |
| `pretrigger_samples` | `0` | Samples kept from before the edge |
| `trigger_timeout_s` | `1.0` | Auto-trigger after this long; 0 = never |
| `max_wait_s` | `5.0` | Hard limit on how long the tool waits |
| `max_edges` | `100` | Transitions listed inline per pin |
| `save_csv`, `csv_path` | `true`, auto | As for `capture_waveform` |

Per pin the result gives `initial_level`, `final_level`, `transitions`, `high_fraction`, `frequency_hz_estimate` and `edges` as `[time_s, new_level]` pairs; `active_pins` lists the pins that toggled. The CSV has a column per pin plus the raw 16-bit value.

### Protocols (UART, SPI, I2C)

The protocol engines run on any DIO pins; pick pins that are not in use by the pattern generator or static I/O.

**`uart_send(data, data_format="text", tx_pin=0, rx_pin=1, baud_rate=9600, data_bits=8, parity="none", stop_bits=1, receive_timeout_s=0.5)`** transmits and collects the reply, returning early once the reply goes quiet. `data_format` is `text` (UTF-8) or `hex` (`"01 ab ff"`). Received data comes back as text, hex and a byte list.

**`uart_receive(rx_pin=1, tx_pin=0, ..., timeout_s=2, idle_timeout_s=0.2, max_bytes=8192)`** listens without transmitting (the engine still reserves a TX pin; pick an unused one). Bytes received since the last UART call are included.

**`spi_transfer(write?, read_count=0, clock_pin=1, mosi_pin=2, miso_pin=3, select_pin=0, frequency=1e6, mode=0, msb_first=true, bits_per_word=8, fill_byte=0)`** clocks `max(len(write), read_count)` words inside one select assertion and returns what came back on MISO. Words wider than 8 bits are not supported yet.

**`i2c_scan(scl_pin=0, sda_pin=1, rate=100e3, first=0x08, last=0x77)`** lists the addresses that acknowledge. **`i2c_write(address, data, ...)`**, **`i2c_read(address, count, ...)`** and **`i2c_write_read(address, data, count, ...)`** (write then repeated-start read, the usual register read) report `acknowledged`, the NAK position and the bytes transferred. Addresses are 7-bit and not pre-shifted. **`i2c_spy(duration_s=2, max_messages=100, ...)`** passively decodes traffic between other devices into messages with address, direction, data and NAK/stop flags.

The AD2 has no I2C pull-ups. If SDA or SCL is held low the tools fail with a wiring hint instead of returning garbage.

### Analysis (spectrum, network and impedance analyzers)

**`capture_spectrum(channel=1, sampling_frequency=1e6, buffer_size=0, range_volts=5, offset_volts=0, window="hann", max_peaks=10, preview_points=200, save_csv=true, csv_path?)`** takes one capture and returns the strongest peaks (frequency, volts peak, dBV), the fundamental's harmonics with THD, the noise floor, a max-hold preview and a CSV of every bin. Use `flattop` for the most accurate amplitudes. Resolution is `sampling_frequency / buffer_size`, and the spectrum reaches half the sample rate.

**`network_analyzer(start_frequency=100, stop_frequency=1e6, steps=31, amplitude=1, offset=0, log_scale=true, min_periods=16, max_wait_s=5, save_csv=true, csv_path?)`** sweeps W1 and measures the gain (dB) and phase (degrees) of channel 2 relative to channel 1. Wire W1 and 1+ to the circuit input, 2+ to its output, 1- and 2- to ground. The summary lists the -3 dB cutoff(s) and classifies the response (low_pass, high_pass, band_pass, flat, ...). Low start frequencies take longer, about `min_periods / f` per point.

**`impedance_analyzer(start_frequency=100, stop_frequency=1e6, steps=21, reference_ohm=1e3, mode="series_reference", amplitude=1, ...)`** measures a component through a known reference resistor. For `series_reference`: W1 and 1+ to one end of the device, the other end to 2+ and to the resistor, resistor to ground, 1- and 2- to ground. Each point gives |Z|, phase, R, X, series/parallel C and L, dissipation and Q; the summary classifies the device as capacitive, inductive or resistive with its median value. Pick `reference_ohm` near the expected |Z|.

Both analyzers take over W1 and both scope channels while they run.

**`sweep_waveform(channel, start_frequency, stop_frequency, sweep_time_s=1, amplitude=1, offset=0, function="sine", repeat=0)`** produces a linear frequency sweep on W1 or W2. Trigger a capture on `wavegen1`/`wavegen2` to catch the start of a sweep.

`generate_waveform` also accepts `function="custom"` with `data` (one period as samples in -1..1, up to 4096, played at `frequency * len(data)` samples per second), `phase_deg`, and `start_all_channels=true` to restart W1 and W2 together so their phases line up.

## Usage Examples

### Basic Workflow

1. **See what is attached**: `list_devices`
2. **Drive W1 with a 1 kHz, 2 Vpp sine**: `generate_waveform {"channel": 1, "function": "sine", "frequency": 1000, "amplitude": 1.0}`
3. **Capture it on channel 1, triggered on the rising edge at 0 V**:
   `capture_waveform {"channels": [1], "sampling_frequency": 1000000, "trigger_source": "ch1", "trigger_level": 0.0}`
4. **Read the full record** from the `csv_path` in the result if the statistics and preview are not enough.
5. **Power a circuit at 3.3 V**: `set_power_supplies {"positive_volts": 3.3, "positive_enabled": true}`
6. **Check the DC level on channel 2**: `measure_voltage {"channel": 2}`
7. **Put a 1 MHz clock on DIO 0 and look at it**: `generate_pattern {"channel": 0, "kind": "clock", "frequency": 1000000}` then `capture_logic {"pins": [0], "trigger_pin": 0, "pretrigger_samples": 100}`
8. **Toggle a reset line**: `write_digital_pins {"low": [5]}` ... `write_digital_pins {"high": [5]}`
9. **Talk to a serial console on DIO 4/5 at 115200**: `uart_send {"data": "help\r\n", "tx_pin": 4, "rx_pin": 5, "baud_rate": 115200}`
10. **Read a SPI flash ID** (CS DIO0, SCK DIO1, MOSI DIO2, MISO DIO3): `spi_transfer {"write": "9f", "read_count": 4}`
11. **Find I2C devices and read a register**: `i2c_scan {}` then `i2c_write_read {"address": 104, "data": "75", "count": 1}`
12. **Bode plot of a filter between W1/1+ and 2+**: `network_analyzer {"start_frequency": 20, "stop_frequency": 100000, "steps": 41}`
13. **Measure a capacitor against a 1 kΩ reference**: `impedance_analyzer {"start_frequency": 200, "stop_frequency": 20000, "reference_ohm": 1000}`
14. **Check a signal's harmonics**: `capture_spectrum {"channel": 1, "sampling_frequency": 200000, "window": "flattop"}`
15. **Shut everything down**: `disconnect_device`

To use the 16k-sample scope buffer, call `connect_device {"configuration": 1}` first.

## Troubleshooting

### "No Analog Discovery devices found" or ERC 3090 error (macOS)
**This is the most common issue on macOS.** The error "Adept NOK dmgrEnumDevicesEx ERC: 3090" indicates the SDK cannot enumerate devices.

**Solution**: Ensure `dwf.framework` is copied to `/Library/Frameworks/`:
```bash
sudo cp -R /Applications/WaveForms.app/Contents/Frameworks/dwf.framework /Library/Frameworks/
```

**Additional checks**:
- Ensure the Analog Discovery 2 is connected via USB
- Verify `dwf.framework` exists: `ls -la /Library/Frameworks/dwf.framework`
- Check USB permissions on macOS (System Settings > Privacy & Security > USB Access)
- Ensure Terminal/Python has USB access permissions
- Try unplugging and replugging the device after copying the framework

### "WF_SDK not found" or import errors
- Ensure WaveForms SDK is installed (download from Digilent website)
- Ensure `WF_SDK` folder is in the project directory (included in this repository)
- Launch the server with `uv run server.py` from the project directory, or pass `--directory`, so that `WF_SDK` is importable

### "No module named 'mcp'"
- Run `uv sync` in the project directory, then launch with `uv run server.py`

### Import errors
- Verify Python version is 3.11 or later: `uv run python --version`
- Recreate the environment: `rm -rf .venv && uv sync`

### Device connection issues
- Make sure only one process is accessing the device at a time
- Close WaveForms application if it's running (it has exclusive access to the device)
- Try disconnecting and reconnecting the device
- Restart your Mac if USB permissions seem stuck

### Testing Installation
Run the test script to verify everything is working:
```bash
uv run test_device.py
```

This will test device connection, oscilloscope capture, and waveform generation.

## Technical Details

- **Framework**: official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (`mcp` 2.x, `MCPServer` decorator API)
- **Protocol**: MCP (Model Context Protocol) over stdio
- **Packaging**: `pyproject.toml` + `uv.lock`, virtual environment managed by `uv`
- **Python Library**: Uses `WF_SDK` wrapper from [Digilent's official WaveForms SDK Getting Started examples](https://github.com/Digilent/WaveForms-SDK-Getting-Started-PY)
- **Device**: Digilent Analog Discovery 2
- **Supported Functions**: oscilloscope, voltmeter, waveform generator, power supplies, system monitor, static digital I/O, pattern generator, logic analyzer, UART, SPI master, I2C master and spy, spectrum / network / impedance analyzers, generator sweeps and custom data
- **Layout**: `server.py` registers the tools; `ad2/` holds one module per instrument on top of `WF_SDK/`
- **Local WF_SDK patches**: macOS framework path fallback, and skipping digital buffer queries in firmware configurations that have no digital channels (otherwise `connect_device` with configuration 1 or 2 fails)
- **Note**: Channels are 1-indexed (1-2) as per WF_SDK convention

## License

This project is provided as-is for use with the Analog Discovery 2 device.

## Testing

Unit tests run without a device. The WaveForms runtime must still be installed, because `WF_SDK` loads `libdwf` at import time; the device itself is replaced by a fake in `tests/conftest.py`.
```bash
uv run pytest
```
With an Analog Discovery 2 attached, add the hardware smoke tests. They briefly enable the supplies and the generator with nothing connected to the device.
```bash
uv run pytest --hardware
```
Two groups of hardware tests need bench wiring and are enabled separately:
```bash
uv run pytest --hardware --jumpered       # DIO0->DIO8, DIO1->DIO9, DIO2->DIO10, T1->DIO15
uv run pytest --hardware --analog-bench   # W1->1+, 1- to GND, 0.22 uF from W1/1+ to 2+, 1 kOhm from 2+ to GND, 2- to GND
```
The digital jumpers exercise UART, SPI, the I2C bus check and the external trigger; the analog bench is a 723 Hz high-pass that exercises the network and impedance analyzers, the spectrum tool and sweeps.
`test_device.py` remains as a standalone check of the vendored WF_SDK wrapper.

## Resources

- [Digilent Analog Discovery 2](https://digilent.com/reference/test-and-measurement/analog-discovery-2/start)
- [WaveForms SDK Documentation](https://digilent.com/reference/software/waveforms/waveforms-sdk/start)
- [MCP Documentation](https://modelcontextprotocol.io/)
- [macOS Setup Guide](MACOS_SETUP.md) - Detailed macOS troubleshooting guide

