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


def pytest_configure(config):
    config.addinivalue_line("markers", "hardware: needs a connected Analog Discovery 2")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--hardware"):
        return
    skip = pytest.mark.skip(reason="needs --hardware and a connected Analog Discovery 2")
    for item in items:
        if "hardware" in item.keywords:
            item.add_marker(skip)


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
