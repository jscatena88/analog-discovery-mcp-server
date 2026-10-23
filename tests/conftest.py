"""Test fixtures: a fake libdwf so the server can be exercised without hardware.

The WaveForms runtime (libdwf) still has to be installed, because the vendored WF_SDK
loads it at import time; only the device is faked. Tests marked `hardware` need a real
Analog Discovery 2 and run only with `pytest --hardware`.
"""

import math
import os
import sys

import anyio
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def pytest_addoption(parser):
    parser.addoption("--hardware", action="store_true", default=False,
                     help="run tests that need a connected Analog Discovery 2")
    parser.addoption("--jumpered", action="store_true", default=False,
                     help="also run hardware tests that need jumpers DIO0->8, DIO1->9, DIO2->10, T1->DIO15")
    parser.addoption("--analog-bench", action="store_true", default=False,
                     help="also run hardware tests that need W1->1+ and the RC circuit on 2+ (see test_hardware.py)")


def pytest_configure(config):
    config.addinivalue_line("markers", "hardware: needs a connected Analog Discovery 2")
    config.addinivalue_line("markers", "jumpered: needs digital loopback jumpers on the device")
    config.addinivalue_line("markers", "analog_bench: needs the W1 / RC analog bench wiring")


def pytest_collection_modifyitems(config, items):
    skip_hw = pytest.mark.skip(reason="needs --hardware and a connected Analog Discovery 2")
    skip_jumper = pytest.mark.skip(reason="needs --jumpered and digital loopback jumpers (see test_hardware.py)")
    skip_analog = pytest.mark.skip(reason="needs --analog-bench and the W1 / RC wiring (see test_hardware.py)")
    for item in items:
        if "hardware" in item.keywords and not config.getoption("--hardware"):
            item.add_marker(skip_hw)
        elif "jumpered" in item.keywords and not config.getoption("--jumpered"):
            item.add_marker(skip_jumper)
        elif "analog_bench" in item.keywords and not config.getoption("--analog-bench"):
            item.add_marker(skip_analog)


def _fill(buf, text):
    buf.value = text.encode()


def _set(arg, value):
    """arg is what ctypes.byref() produced; write through to the underlying object."""
    arg._obj.value = value


# (channel name, label, [(node name, unit, set_min, set_max, read_min, read_max)])
AD2_IO = [
    ("Positive Supply", "V+", [("Enable", "", 0, 1, 0, 1), ("Voltage", "V", 0.5, 5.0, 0, 5), ("Current", "A", 0, 1, 0, 0)]),
    ("Negative Supply", "V-", [("Enable", "", 0, 1, 0, 1), ("Voltage", "V", -5.0, -0.5, 0, 5), ("Current", "A", 0, 1, 0, 0)]),
    ("USB Monitor", "USB", [("Voltage", "V", 0, 0, 0, 6.65), ("Current", "A", 0, 0, 0, 1.06), ("Temperature", "C", 0, 0, -128, 127.75)]),
    ("Auxiliary Monitor", "Aux", [("Voltage", "V", 0, 0, 0, 26.35), ("Current", "A", 0, 0, 0, 3.2)]),
]


class FakeDwf:
    """Stand-in for libdwf. Every FDwf* call succeeds (returns 1) and is recorded; the handlers
    below fill output arguments with values resembling an Analog Discovery 2."""

    def __init__(self):
        self.calls = []
        self.io_values = {("USB", "Voltage"): 4.99, ("USB", "Current"): 0.12,
                          ("USB", "Temperature"): 29.5, ("Aux", "Voltage"): 0.0, ("Aux", "Current"): 0.0}
        self.io_set = {}
        self.master_enabled = 0
        self.scope_buffer = 8192
        self.sample_rate = 20e6
        self.acquisition_done = True
        # 1 V (ch1) / 0.5 V (ch2) sine at 1/200 of the sample rate: ~41 periods per 8192-sample buffer
        self.signal = lambda ch, i, fs: (1.0 if ch == 0 else 0.5) * math.sin(2 * math.pi * i / 200)
        # static DIO state and external levels seen on pins that are inputs
        self.dio_output_enable = 0
        self.dio_output = 0
        self.dio_external = 0
        # logic analyzer sample i: DIO0 toggles every 50 samples (1 MHz at 100 MS/s), DIO1 held high
        self.logic_pattern = lambda i: ((i // 50) & 1) | 0b10
        # UART: transmitted bytes loop back into the receive queue
        self.uart_queue = bytearray()
        self.uart_tx_log = []
        # I2C: one fake slave at 0x1D holding 16 bytes of "registers"
        self.i2c_bus_free = 1
        self.i2c_devices = {0x1D: bytearray(range(16))}
        self.i2c_writes = []
        self.i2c_spy_fragments = []     # (start, stop, bytes, nak) tuples handed out in order
        # analyzer engine: a 1 k / 0.22 uF high-pass as the transfer function, the capacitor
        # (with 5 ohm ESR) as the impedance device under test
        self.analyzer_r = 1e3
        self.analyzer_c = 0.22e-6
        self.analyzer_esr = 5.0
        self.analyzer_frequency = 100.0

    def calls_to(self, name):
        return [args for called, args in self.calls if called == name]

    def __getattr__(self, name):
        if not name.startswith("FDwf"):
            raise AttributeError(name)
        handler = getattr(self, "_h_" + name[4:], None)

        def call(*args):
            self.calls.append((name, args))
            result = handler(*args) if handler else None
            return 1 if result is None else result

        return call

    # enumeration / open
    def _h_Enum(self, filt, count): _set(count, 1)
    def _h_EnumDeviceName(self, i, buf): _fill(buf, "Analog Discovery 2")
    def _h_EnumSN(self, i, buf): _fill(buf, "SN:FAKE0001")
    def _h_EnumDeviceType(self, i, dev_id, rev): _set(dev_id, 3); _set(rev, 3)
    def _h_EnumConfig(self, i, n): _set(n, 2)

    def _h_EnumConfigInfo(self, cfg, info, out):
        table = {1: 2, 2: 2, 4: 16, 5: 16, 7: 8192 if cfg.value == 0 else 16384, 8: 4096, 9: 4096, 10: 1024}
        _set(out, table.get(info.value, 0))

    def _h_DeviceConfigOpen(self, i, cfg, handle): _set(handle, 1)
    def _h_GetVersion(self, buf): _fill(buf, "3.23.4")
    def _h_GetLastErrorMsg(self, buf): _fill(buf, "")

    # analog in capabilities
    def _h_AnalogInChannelCount(self, h, n): _set(n, 2)
    def _h_AnalogInBufferSizeInfo(self, h, lo, hi): _set(hi, 8192)
    def _h_AnalogInBitsInfo(self, h, n): _set(n, 14)
    def _h_AnalogInChannelRangeInfo(self, h, a, b, c): _set(a, 0.5); _set(b, 50.0); _set(c, 2)
    def _h_AnalogInChannelOffsetInfo(self, h, a, b, c): _set(a, -25.0); _set(b, 25.0); _set(c, 1)

    # analog out capabilities
    def _h_AnalogOutCount(self, h, n): _set(n, 2)
    def _h_AnalogOutNodeInfo(self, h, ch, mask): _set(mask, 1)
    def _h_AnalogOutNodeDataInfo(self, h, ch, nd, lo, hi): _set(hi, 4096)
    def _h_AnalogOutNodeAmplitudeInfo(self, h, ch, nd, a, b): _set(a, 0.0); _set(b, 5.0)
    def _h_AnalogOutNodeOffsetInfo(self, h, ch, nd, a, b): _set(a, -5.0); _set(b, 5.0)
    def _h_AnalogOutNodeFrequencyInfo(self, h, ch, nd, a, b): _set(a, 0.0); _set(b, 10e6)

    # analog IO (supplies + monitor)
    def _h_AnalogIOChannelCount(self, h, n): _set(n, len(AD2_IO))
    def _h_AnalogIOChannelName(self, h, ch, name, label): _fill(name, AD2_IO[ch.value][0]); _fill(label, AD2_IO[ch.value][1])
    def _h_AnalogIOChannelInfo(self, h, ch, n): _set(n, len(AD2_IO[ch.value][2]))

    def _h_AnalogIOChannelNodeName(self, h, ch, nd, name, unit):
        node = AD2_IO[ch.value][2][nd.value]
        _fill(name, node[0]); _fill(unit, node[1])

    def _h_AnalogIOChannelNodeSetInfo(self, h, ch, nd, a, b, c):
        node = AD2_IO[ch.value][2][nd.value]
        _set(a, node[2]); _set(b, node[3]); _set(c, 1)

    def _h_AnalogIOChannelNodeStatusInfo(self, h, ch, nd, a, b, c):
        node = AD2_IO[ch.value][2][nd.value]
        _set(a, node[4]); _set(b, node[5]); _set(c, 1)

    def _h_AnalogIOChannelNodeSet(self, h, ch, nd, value): self.io_set[(ch.value, nd.value)] = value.value

    def _h_AnalogIOChannelNodeStatus(self, h, ch, nd, out):
        label = AD2_IO[ch.value][1]
        node = AD2_IO[ch.value][2][nd.value][0]
        _set(out, self.io_set.get((ch.value, nd.value), self.io_values.get((label, node), 0.0)))

    def _h_AnalogIOChannelNodeGet(self, h, ch, nd, out): _set(out, self.io_set.get((ch.value, nd.value), 0.0))
    def _h_AnalogIOEnableSet(self, h, flag): self.master_enabled = flag.value
    def _h_AnalogIOEnableGet(self, h, out): _set(out, self.master_enabled)
    def _h_AnalogIOEnableStatus(self, h, out): _set(out, self.master_enabled)

    # digital capabilities
    def _h_DigitalInBitsInfo(self, h, n): _set(n, 16)
    def _h_DigitalInBufferSizeInfo(self, h, n): _set(n, 4096)
    def _h_DigitalOutCount(self, h, n): _set(n, 16)
    def _h_DigitalOutDataInfo(self, h, ch, n): _set(n, 1024)
    def _h_DigitalOutInternalClockInfo(self, h, out): _set(out, 100e6)
    def _h_DigitalInInternalClockInfo(self, h, out): _set(out, 100e6)
    def _h_DigitalOutCounterInfo(self, h, ch, lo, hi): _set(hi, 32768)
    def _h_DigitalOutStatus(self, h, out): _set(out, 3)

    # static DIO
    def _h_DigitalIOOutputEnableSet(self, h, mask): self.dio_output_enable = mask.value
    def _h_DigitalIOOutputEnableGet(self, h, out): _set(out, self.dio_output_enable)
    def _h_DigitalIOOutputSet(self, h, mask): self.dio_output = mask.value
    def _h_DigitalIOOutputGet(self, h, out): _set(out, self.dio_output)
    def _h_DigitalIOReset(self, h): self.dio_output_enable = 0; self.dio_output = 0

    def _h_DigitalIOInputStatus(self, h, out):
        driven = self.dio_output & self.dio_output_enable
        _set(out, driven | (self.dio_external & ~self.dio_output_enable))

    # logic analyzer
    def _h_DigitalInStatus(self, h, read, status):
        if hasattr(status, "_obj"):
            _set(status, 2 if self.acquisition_done else 1)

    def _h_DigitalInStatusData(self, h, buf, nbytes):
        for i in range(nbytes.value // 2):
            buf[i] = self.logic_pattern(i)

    # UART (loopback)
    def _h_DigitalUartTx(self, h, buf, n):
        if buf is None:
            return
        data = bytes(buf[:n.value])
        self.uart_tx_log.append(data)
        self.uart_queue += data

    def _h_DigitalUartRx(self, h, buf, n, count, parity):
        _set(parity, 0)
        if buf is None:
            self.uart_queue.clear()
            _set(count, 0)
            return
        take = min(n.value, len(self.uart_queue))
        for i in range(take):
            buf[i] = self.uart_queue[i]
        del self.uart_queue[:take]
        _set(count, take)

    # SPI (MOSI looped back to MISO)
    def _h_DigitalSpiWriteRead(self, h, dq, bits, tx, ntx, rx, nrx):
        for i in range(nrx.value):
            rx[i] = tx[i] if i < ntx.value else 0

    # I2C
    def _h_DigitalI2cClear(self, h, free): _set(free, self.i2c_bus_free)

    def _h_DigitalI2cWrite(self, h, addr, tx, n, nak):
        address = addr.value >> 1
        if address not in self.i2c_devices:
            _set(nak, 1)
            return
        data = bytes(tx[:n.value]) if tx is not None and n.value else b""
        self.i2c_writes.append((address, data))
        _set(nak, 0)

    def _h_DigitalI2cRead(self, h, addr, rx, n, nak):
        address = addr.value >> 1
        if address not in self.i2c_devices:
            _set(nak, 1)
            return
        memory = self.i2c_devices[address]
        for i in range(n.value):
            rx[i] = memory[i % len(memory)]
        _set(nak, 0)

    def _h_DigitalI2cWriteRead(self, h, addr, tx, ntx, rx, nrx, nak):
        address = addr.value >> 1
        if address not in self.i2c_devices:
            _set(nak, 1)
            return
        memory = self.i2c_devices[address]
        register = tx[0]
        for i in range(nrx.value):
            rx[i] = memory[(register + i) % len(memory)]
        _set(nak, 0)

    # analyzer engine
    def _h_AnalogImpedanceFrequencySet(self, h, f): self.analyzer_frequency = f.value

    def _h_AnalogImpedanceStatus(self, h, status):
        if status is not None:
            _set(status, 2)

    def _transfer(self):
        w = 2 * math.pi * self.analyzer_frequency
        jwrc = 1j * w * self.analyzer_r * self.analyzer_c
        return jwrc / (1 + jwrc)

    def _h_AnalogImpedanceStatusInput(self, h, ch, gain, phase):
        if ch.value == 0:
            _set(gain, 1.0)
            return
        transfer = self._transfer()
        _set(gain, 1 / abs(transfer))                   # engine reports C1/C2
        if phase is not None:
            _set(phase, -math.atan2(transfer.imag, transfer.real))

    def _h_AnalogImpedanceStatusMeasure(self, h, measure, out):
        w = 2 * math.pi * self.analyzer_frequency
        r, x = self.analyzer_esr, -1 / (w * self.analyzer_c)
        z = complex(r, x)
        table = {0: abs(z), 1: math.atan2(x, r), 2: r, 3: x, 8: self.analyzer_c, 9: self.analyzer_c,
                 10: x / w, 11: x / w, 12: r / abs(x), 13: abs(x) / r}
        _set(out, table.get(measure.value, 0.0))

    def _h_AnalogImpedanceStatusWarning(self, h, ch, out): _set(out, 0)

    def _h_DigitalI2cSpyStatus(self, h, start, stop, data, count, nak):
        if self.i2c_spy_fragments:
            f_start, f_stop, payload, f_nak = self.i2c_spy_fragments.pop(0)
        else:
            f_start, f_stop, payload, f_nak = 0, 0, b"", 0
        _set(start, f_start); _set(stop, f_stop); _set(nak, f_nak)
        for i, b in enumerate(payload):
            data._obj[i] = b
        _set(count, len(payload))

    # scope acquisition
    def _h_AnalogInFrequencySet(self, h, f): self.sample_rate = f.value
    def _h_AnalogInFrequencyGet(self, h, out): _set(out, self.sample_rate)
    def _h_AnalogInBufferSizeSet(self, h, n): self.scope_buffer = n.value
    def _h_AnalogInBufferSizeGet(self, h, out): _set(out, self.scope_buffer)

    def _h_AnalogInStatus(self, h, read, status):
        if hasattr(status, "_obj"):
            _set(status, 2 if self.acquisition_done else 1)

    def _h_AnalogInStatusData(self, h, ch, buf, n):
        for i in range(n.value):
            buf[i] = self.signal(ch.value, i, self.sample_rate)

    def _h_AnalogInStatusSample(self, h, ch, out): _set(out, 1.234)


@pytest.fixture
def fake_dwf(monkeypatch):
    import WF_SDK.device
    import WF_SDK.scope
    import WF_SDK.wavegen
    fake = FakeDwf()
    for module in (WF_SDK.device, WF_SDK.scope, WF_SDK.wavegen):
        monkeypatch.setattr(module, "dwf", fake)
    return fake


@pytest.fixture
def server(fake_dwf, monkeypatch, tmp_path):
    monkeypatch.setenv("AD2_CAPTURE_DIR", str(tmp_path))
    from ad2 import analogio
    monkeypatch.setattr(analogio, "SETTLE_S", 0.0)
    import server as srv
    srv.session.disconnect()
    yield srv
    srv.session.disconnect()


def call_tool(srv, name, **arguments):
    """Invoke a tool in-process and return its structured result; raise RuntimeError on tool error."""
    from mcp.server.mcpserver.exceptions import ToolError
    try:
        result = anyio.run(srv.mcp.call_tool, name, arguments)
    except ToolError as e:
        raise RuntimeError(str(e)) from e
    if getattr(result, "isError", False) or getattr(result, "is_error", False):
        text = " ".join(getattr(c, "text", "") for c in result.content)
        raise RuntimeError(text)
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    return structured
