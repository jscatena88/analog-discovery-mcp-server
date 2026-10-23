"""Device session: enumeration, connect/disconnect, capability summary."""

import ctypes

from WF_SDK import device, scope as wf_scope, wavegen as wf_wavegen
import dwfconstants as constants

from .errors import AD2Error

_CONFIG_FIELDS = [
    ("analog_in_channels", constants.DECIAnalogInChannelCount),
    ("analog_out_channels", constants.DECIAnalogOutChannelCount),
    ("digital_in_channels", constants.DECIDigitalInChannelCount),
    ("digital_out_channels", constants.DECIDigitalOutChannelCount),
    ("analog_in_buffer", constants.DECIAnalogInBufferSize),
    ("analog_out_buffer", constants.DECIAnalogOutBufferSize),
    ("digital_in_buffer", constants.DECIDigitalInBufferSize),
    ("digital_out_buffer", constants.DECIDigitalOutBufferSize),
]


def _device_type_names() -> dict[int, str]:
    names = {}
    for attr in dir(constants):
        if attr.startswith("devid"):
            value = getattr(constants, attr)
            names[getattr(value, "value", value)] = attr[len("devid"):]
    return names


def enumerate_devices() -> list[dict]:
    """List every Digilent device the runtime can see, with its firmware configurations."""
    dwf = device.dwf
    count = ctypes.c_int()
    if dwf.FDwfEnum(constants.enumfilterAll, ctypes.byref(count)) == 0:
        device.check_error()
    type_names = _device_type_names()
    devices = []
    for index in range(count.value):
        name = ctypes.create_string_buffer(64)
        serial = ctypes.create_string_buffer(16)
        device_id = ctypes.c_int()
        revision = ctypes.c_int()
        config_count = ctypes.c_int()
        dwf.FDwfEnumDeviceName(ctypes.c_int(index), name)
        dwf.FDwfEnumSN(ctypes.c_int(index), serial)
        dwf.FDwfEnumDeviceType(ctypes.c_int(index), ctypes.byref(device_id), ctypes.byref(revision))
        dwf.FDwfEnumConfig(ctypes.c_int(index), ctypes.byref(config_count))
        configurations = []
        for cfg in range(config_count.value):
            entry = {"index": cfg}
            for key, const in _CONFIG_FIELDS:
                info = ctypes.c_int()
                dwf.FDwfEnumConfigInfo(ctypes.c_int(cfg), const, ctypes.byref(info))
                entry[key] = info.value
            configurations.append(entry)
        devices.append({
            "index": index,
            "name": name.value.decode(errors="replace"),
            "serial": serial.value.decode(errors="replace"),
            "device_type": type_names.get(device_id.value, str(device_id.value)),
            "revision": revision.value,
            "configurations": configurations,
        })
    return devices


def _reset_wrapper_state() -> None:
    """WF_SDK appends capability lists on every open; clear them so a reconnect starts clean."""
    io = device.data.analog.IO
    for attr in ("node_count", "channel_name", "channel_label", "node_name", "node_unit",
                 "min_set_range", "max_set_range", "min_read_range", "max_read_range",
                 "set_steps", "read_steps"):
        setattr(io, attr, [])
    out = device.data.analog.output
    for attr in ("node_count", "node_type", "max_buffer_size", "min_amplitude", "max_amplitude",
                 "min_offset", "max_offset", "min_frequency", "max_frequency"):
        setattr(out, attr, [])


class Session:
    """Holds the open device and which instruments have been configured on it."""

    def __init__(self):
        self.device_data = None
        self.configuration = 0
        self.device_name = None
        self.scope_settings = None      # (sampling_frequency, buffer_size, range, offset) in effect
        self.logic_settings = None      # (divider, buffer_size) in effect
        self.uart_settings = None       # (tx, rx, baud, bits, parity, stop) in effect
        self.uart_tx_ready = False      # TX line initialized (driven idle-high)
        self.spi_settings = None
        self.i2c_settings = None        # (scl, sda, rate) in effect
        self.jtag = None                # ad2.jtag.Port when a JTAG port is set up
        self.swd_settings = None        # (swclk, swdio, rate) in effect
        self.wavegen_running = set()    # channels with an active output
        self.pattern_running = set()    # DIO pins driven by the pattern generator

    @property
    def connected(self) -> bool:
        return self.device_data is not None

    def connect(self, device_name: str | None = None, configuration: int = 0) -> dict:
        if self.connected:
            self.disconnect()
        if configuration < 0:
            raise AD2Error("configuration must be 0 or a positive index from list_devices")
        _reset_wrapper_state()
        try:
            self.device_data = device.open(device_name, configuration)
        except device.error as e:
            # device.open may fail after the handle was opened (e.g. while reading
            # capabilities); release it or every later open reports the device busy.
            try:
                device.dwf.FDwfDeviceCloseAll()
            except Exception:
                pass
            raise AD2Error(
                f"Could not open device: {e}. Check the USB cable and make sure the WaveForms "
                "application or another process does not have the device open."
            ) from e
        self.configuration = configuration
        self.device_name = device_name
        return self.info()

    def ensure(self):
        """Return the open device, connecting with defaults if needed."""
        if not self.connected:
            self.connect()
        return self.device_data

    def disconnect(self) -> None:
        if not self.connected:
            return
        dd = self.device_data
        from . import analogio, digital  # local imports: neither depends on session
        for shutdown in (lambda: wf_scope.close(dd),
                         lambda: wf_wavegen.close(dd, 0),
                         lambda: device.dwf.FDwfDigitalOutReset(dd.handle),
                         lambda: device.dwf.FDwfDigitalInReset(dd.handle),
                         lambda: device.dwf.FDwfDigitalUartReset(dd.handle),
                         lambda: device.dwf.FDwfDigitalSpiReset(dd.handle),
                         lambda: device.dwf.FDwfDigitalI2cReset(dd.handle),
                         lambda: device.dwf.FDwfDigitalSwdReset(dd.handle),
                         lambda: digital.reset(dd),
                         lambda: analogio.disable_supplies(dd, settle=False),
                         lambda: device.close(dd)):
            try:
                shutdown()
            except Exception:
                pass
        self.device_data = None
        self.scope_settings = None
        self.logic_settings = None
        self.uart_settings = None
        self.uart_tx_ready = False
        self.spi_settings = None
        self.i2c_settings = None
        self.jtag = None
        self.swd_settings = None
        self.wavegen_running = set()
        self.pattern_running = set()

    def info(self) -> dict:
        dd = self.ensure()
        ai, ao, di, do = dd.analog.input, dd.analog.output, dd.digital.input, dd.digital.output

        def first(rows):
            return rows[0][0] if rows and rows[0] else None

        return {
            "name": dd.name,
            "waveforms_version": dd.version,
            "configuration": self.configuration,
            "analog_input": {
                "channels": ai.channel_count,
                "max_buffer_size": ai.max_buffer_size,
                "resolution_bits": ai.max_resolution,
                "range_volts": [ai.min_range, ai.max_range],
                "offset_volts": [ai.min_offset, ai.max_offset],
            },
            "analog_output": {
                "channels": ao.channel_count,
                "max_buffer_size": first(ao.max_buffer_size),
                "max_amplitude_volts": first(ao.max_amplitude),
                "offset_volts": [first(ao.min_offset), first(ao.max_offset)],
                "max_frequency_hz": first(ao.max_frequency),
            },
            "digital_input": {"channels": di.channel_count, "max_buffer_size": di.max_buffer_size},
            "digital_output": {"channels": do.channel_count, "max_buffer_size": do.max_buffer_size},
            "analog_io_channels": list(dd.analog.IO.channel_label),
            "scope_configured": self.scope_settings is not None,
            "wavegen_running_channels": sorted(self.wavegen_running),
            "pattern_running_pins": sorted(self.pattern_running),
        }
