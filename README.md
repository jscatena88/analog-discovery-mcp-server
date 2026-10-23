# Analog Discovery 2 MCP Server

An MCP (Model Context Protocol) server for controlling the Digilent Analog Discovery 2 device. This server provides tools to interact with the Analog Discovery 2's oscilloscope, waveform generator, and other functions through the MCP protocol.

## Features

- **Device Management**: List, connect, and disconnect Analog Discovery 2 devices
- **Oscilloscope**: Capture analog waveforms from channels 0 and 1
- **Waveform Generator**: Generate various waveforms (sine, square, triangle, ramp, DC) on output channels
- **Device Information**: Get version and device details

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

### `list_devices`
List all available Analog Discovery 2 devices connected to the system.

**Parameters**: None

**Example**:
```json
{
  "tool": "list_devices"
}
```

### `connect_device`
Connect to an Analog Discovery 2 device.

**Parameters**:
- `device_name` (string, optional): Device name filter (None for first available, "Analog Discovery 2" for specific)

**Example**:
```json
{
  "tool": "connect_device",
  "arguments": {
    "device_name": "Analog Discovery 2"
  }
}
```

### `disconnect_device`
Disconnect from the current device.

**Parameters**: None

### `get_device_info`
Get information about the currently connected device.

**Parameters**: None

### `capture_waveform`
Capture an analog waveform from a channel (oscilloscope function).

**Parameters**:
- `channel` (integer, required): Channel number (1 or 2)
- `sampling_frequency` (number, optional): Sampling frequency in Hz (default: 20MHz)
- `buffer_size` (integer, optional): Buffer size in samples (0 = maximum, default: 0)
- `offset` (number, optional): Channel offset in volts (default: 0.0)
- `amplitude_range` (number, optional): Channel range in volts (default: 5.0)

**Example**:
```json
{
  "tool": "capture_waveform",
  "arguments": {
    "channel": 1,
    "sampling_frequency": 1000000,
    "buffer_size": 4096,
    "offset": 0.0,
    "amplitude_range": 5.0
  }
}
```

### `generate_waveform`
Generate a waveform on a channel (waveform generator function).

**Parameters**:
- `channel` (integer, required): Channel number (1 or 2)
- `function` (string, required): Waveform type: "sine", "square", "triangle", "ramp_up", "ramp_down", "dc", "noise", "pulse", or "trapezium"
- `frequency` (number, required): Frequency in Hz
- `amplitude` (number, required): Amplitude in volts
- `offset` (number, optional): DC offset in volts (default: 0.0)

**Example**:
```json
{
  "tool": "generate_waveform",
  "arguments": {
    "channel": 1,
    "function": "sine",
    "frequency": 1000,
    "amplitude": 2.5,
    "offset": 0.0
  }
}
```

### `stop_waveform`
Stop waveform generation on a channel.

**Parameters**:
- `channel` (integer, required): Channel number (1 or 2)

**Example**:
```json
{
  "tool": "stop_waveform",
  "arguments": {
    "channel": 1
  }
}
```

## Usage Examples

### Basic Workflow

1. **List available devices**:
   ```
   list_devices
   ```

2. **Connect to device**:
   ```
   connect_device
   ```
   or with explicit device name:
   ```
   connect_device {"device_name": "Analog Discovery 2"}
   ```

3. **Capture a waveform**:
   ```
   capture_waveform {"channel": 1, "sampling_frequency": 20000000}
   ```

4. **Generate a sine wave**:
   ```
   generate_waveform {"channel": 1, "function": "sine", "frequency": 1000, "amplitude": 2.5}
   ```

5. **Stop waveform generation**:
   ```
   stop_waveform {"channel": 1}
   ```

6. **Disconnect**:
   ```
   disconnect_device
   ```

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
- **Supported Functions**: Analog input (oscilloscope), analog output (waveform generator)
- **Note**: Channels are 1-indexed (1-2) as per WF_SDK convention

## License

This project is provided as-is for use with the Analog Discovery 2 device.

## Testing

A test script is included to verify your installation:
```bash
uv run test_device.py
```

This will test:
- Device connection
- Oscilloscope waveform capture
- Waveform generator

## Resources

- [Digilent Analog Discovery 2](https://digilent.com/reference/test-and-measurement/analog-discovery-2/start)
- [WaveForms SDK Documentation](https://digilent.com/reference/software/waveforms/waveforms-sdk/start)
- [MCP Documentation](https://modelcontextprotocol.io/)
- [macOS Setup Guide](MACOS_SETUP.md) - Detailed macOS troubleshooting guide

