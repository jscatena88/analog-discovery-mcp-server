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
    parser.addoption("--esp8266", action="store_true", default=False,
                     help="also run hardware tests that need an ESP8266 on DIO0-4 (TDO, TCK, TDI, TMS, nReset)")
    parser.addoption("--imu", action="store_true", default=False,
                     help="also run hardware tests that need an MPU6886 on V+ with SCL on DIO15 and SDA on DIO7")


def pytest_configure(config):
    config.addinivalue_line("markers", "hardware: needs a connected Analog Discovery 2")
    config.addinivalue_line("markers", "jumpered: needs digital loopback jumpers on the device")
    config.addinivalue_line("markers", "analog_bench: needs the W1 / RC analog bench wiring")
    config.addinivalue_line("markers", "esp8266: needs an ESP8266 JTAG target on DIO0-4")
    config.addinivalue_line("markers", "imu: needs an MPU6886 on V+, SCL DIO15, SDA DIO7")


def pytest_collection_modifyitems(config, items):
    groups = (("jumpered", "--jumpered", "digital loopback jumpers"),
              ("analog_bench", "--analog-bench", "the W1 / RC wiring"),
              ("esp8266", "--esp8266", "an ESP8266 on DIO0-4"),
              ("imu", "--imu", "an MPU6886 on DIO7/DIO15"))
    skip_hw = pytest.mark.skip(reason="needs --hardware and a connected Analog Discovery 2")
    for item in items:
        if "hardware" in item.keywords and not config.getoption("--hardware"):
            item.add_marker(skip_hw)
            continue
        for marker, option, what in groups:
            if marker in item.keywords and not config.getoption(option):
                item.add_marker(pytest.mark.skip(reason=f"needs {option} and {what} (see test_hardware.py)"))


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


class FakeTap:
    """A minimal JTAG TAP: 4-bit IR (capture 0b0001), IDCODE on reset or IR 0b1110, BYPASS otherwise.
    Clocked by TCK edges on the fake's static output word; TDO is presented on dio_external."""

    def __init__(self, tck, tms, tdi, tdo, idcode=0x4BA00477):
        self.tck, self.tms, self.tdi, self.tdo = tck, tms, tdi, tdo
        self.idcode = idcode
        self.state = "reset"
        self.ir = 0b1110
        self.shift = 0
        self.shift_len = 32
        self.tdo_level = 0

    def observe(self, previous, current, fake):
        from ad2.jtag import STATES
        was_high = (previous >> self.tck) & 1
        is_high = (current >> self.tck) & 1
        tms = (current >> self.tms) & 1
        tdi = (current >> self.tdi) & 1
        if not was_high and is_high:                      # rising edge: capture/shift/update, then move
            if self.state == "capture_dr":
                if self.ir == 0b1110:
                    self.shift, self.shift_len = self.idcode, 32
                else:
                    self.shift, self.shift_len = 0, 1
            elif self.state == "capture_ir":
                self.shift, self.shift_len = 0b0001, 4
            elif self.state in ("shift_dr", "shift_ir"):
                self.shift = (self.shift >> 1) | (tdi << (self.shift_len - 1))
            nxt = STATES[self.state][tms]
            if nxt == "update_ir":                        # entering update_ir latches the IR
                self.ir = self.shift & 0xF
            if nxt == "reset":
                self.ir = 0b1110
            self.state = nxt
        elif was_high and not is_high:                    # falling edge: TDO shows the LSB of the shift register
            if self.state in ("shift_dr", "shift_ir"):
                self.tdo_level = self.shift & 1
        fake.dio_external = (fake.dio_external & ~(1 << self.tdo)) | (self.tdo_level << self.tdo)


class FakeI2cSlave:
    """A register-file I2C slave driven by the bit-banged master's output-enable changes. The master
    pulls a line low by enabling its output (value 0); the slave pulls SDA low through sda_drive."""

    def __init__(self, scl, sda, address=0x68, registers=None):
        self.scl, self.sda, self.address = scl, sda, address
        self.registers = registers if registers is not None else bytearray(range(0x80))
        self.pointer = 0
        self.active = False
        self.n = 0                  # bits clocked in the current byte (8 = data done, 9 = ack clock done)
        self.byte = 0
        self.first = True           # next incoming byte is the address
        self.out = False            # slave is transmitting
        self.out_byte = 0
        self.master_ack = False
        self.sda_drive = 1

    @staticmethod
    def _master(enable, pin):
        return 0 if (enable >> pin) & 1 else 1          # enabled output = master driving low

    def observe(self, previous, current, fake):
        scl_was, scl_is = self._master(previous, self.scl), self._master(current, self.scl)
        sda_was, sda_is = self._master(previous, self.sda), self._master(current, self.sda)
        if scl_was and scl_is and sda_was and not sda_is:          # START / repeated START
            self.active, self.n, self.byte, self.first, self.out = True, 0, 0, True, False
            self.sda_drive = 1
        elif scl_was and scl_is and not sda_was and sda_is:        # STOP
            self.active = False
            self.sda_drive = 1
        elif self.active and not scl_was and scl_is:               # SCL rising: sample
            if self.n < 8:
                if not self.out:
                    self.byte = (self.byte << 1) | sda_is
                self.n += 1
            elif self.n == 8:
                if self.out:
                    self.master_ack = sda_is == 0
                self.n = 9
        elif self.active and scl_was and not scl_is:               # SCL falling: present
            if self.n == 8:
                if self.out:
                    self.sda_drive = 1                              # release for the master's ACK
                else:
                    self._incoming_byte_done()
            elif self.n == 9:
                self.n, self.byte = 0, 0
                if self.out:
                    if getattr(self, "pending_first_bit", False):
                        self.pending_first_bit = False            # first byte was loaded with the address
                    elif not self.master_ack:
                        self.active = False
                        self.sda_drive = 1
                        return self._publish(fake)
                    else:
                        self._load_next()
                    self.sda_drive = (self.out_byte >> 7) & 1
                else:
                    self.sda_drive = 1
            elif self.out:
                self.sda_drive = (self.out_byte >> (7 - self.n)) & 1
        self._publish(fake)

    def _publish(self, fake):
        fake.dio_external = (fake.dio_external & ~(1 << self.sda)) | (self.sda_drive << self.sda)

    def _incoming_byte_done(self):
        if self.first:
            self.first = False
            if (self.byte >> 1) != self.address:
                self.active = False
                self.sda_drive = 1
                return
            self.sda_drive = 0                                      # ACK the address
            if self.byte & 1:
                self.out = True
                self._load_next()
                self.pending_first_bit = True
        else:
            self.pointer = self.byte                                # register pointer write
            self.sda_drive = 0

    def _load_next(self):
        self.out_byte = self.registers[self.pointer % len(self.registers)]
        self.pointer += 1


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
        self.i2c_target_pins = None          # (scl, sda): when set, the slave answers only on these pins
        self.i2c_pins = (None, None)
        self.i2c_stretch_detect = 1          # engine setting; some buses only work with it off
        self.i2c_stretch_faults = False      # when True, transfers bus-error while stretch detection is on
        self.i2c_devices = {0x1D: bytearray(range(16))}
        self.i2c_writes = []
        self.i2c_spy_fragments = []     # (start, stop, bytes, nak) tuples handed out in order
        # analyzer engine: a 1 k / 0.22 uF high-pass as the transfer function, the capacitor
        # (with 5 ohm ESR) as the impedance device under test
        self.analyzer_r = 1e3
        self.analyzer_c = 0.22e-6
        self.analyzer_esr = 5.0
        self.analyzer_frequency = 100.0
        # record mode: samples handed out per status call; counters: fixed readings, tick advances per call
        self.record_chunk = 1000
        self.counter_supported = True        # False models the AD2, which has no hardware counter
        # simulated JTAG target behind the static pins (None = no target; TDO follows dio_external)
        self.tap = None
        # simulated bit-banged I2C slave on the static pins (None = none)
        self.i2c_slave = None
        # simulated SW-DP: answers on (swclk, swdio) == swd_target only
        self.swd_target = None
        self.swd_pins = (None, None)
        self.swd_dp = {"ctrl_stat": 0, "select": 0}
        self.swd_rdbuff = 0
        self.swd_writes = []
        self.analog_counter_hz = 12345.0
        self.digital_counter_hz = 1e6
        self.counter_tick = 0

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
    def _h_DigitalIOOutputEnableSet(self, h, mask):
        previous = self.dio_output_enable
        self.dio_output_enable = mask.value
        if self.i2c_slave is not None:
            self.i2c_slave.observe(previous, self.dio_output_enable, self)

    def _h_DigitalIOOutputEnableGet(self, h, out): _set(out, self.dio_output_enable)
    def _h_DigitalIOOutputSet(self, h, mask):
        previous = self.dio_output
        self.dio_output = mask.value
        if self.tap is not None:
            self.tap.observe(previous, self.dio_output, self)

    def _h_DigitalIOOutputGet(self, h, out): _set(out, self.dio_output)
    def _h_DigitalIOReset(self, h): self.dio_output_enable = 0; self.dio_output = 0

    def _h_DigitalIOInputStatus(self, h, out):
        driven = self.dio_output & self.dio_output_enable
        value = driven | (self.dio_external & ~self.dio_output_enable)
        if self.i2c_slave is not None:
            # open-drain: an enabled (low) output or the slave pulling low wins; otherwise the pull-up
            for pin in (self.i2c_slave.scl, self.i2c_slave.sda):
                if (self.dio_output_enable >> pin) & 1:
                    value &= ~(1 << pin)
                elif pin == self.i2c_slave.sda:
                    value = (value & ~(1 << pin)) | (self.i2c_slave.sda_drive << pin)
                else:
                    value |= 1 << pin
        _set(out, value)

    # logic analyzer
    def _h_DigitalInStatus(self, h, read, status):
        if hasattr(status, "_obj"):
            _set(status, 2 if self.acquisition_done else 1)

    def _h_DigitalInConfigure(self, h, reconfigure, start):
        if start.value:
            self.logic_offset = 0

    def _h_DigitalInStatusData(self, h, buf, nbytes):
        offset = getattr(self, "logic_offset", 0)
        for i in range(nbytes.value // 2):
            buf[i] = self.logic_pattern(offset + i)
        self.logic_offset = offset + nbytes.value // 2

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
    def _h_DigitalI2cSclSet(self, h, pin): self.i2c_pins = (pin.value, self.i2c_pins[1])
    def _h_DigitalI2cSdaSet(self, h, pin): self.i2c_pins = (self.i2c_pins[0], pin.value)
    def _h_DigitalI2cClear(self, h, free): _set(free, self.i2c_bus_free)
    def _h_DigitalI2cStretchSet(self, h, flag): self.i2c_stretch_detect = flag.value

    def _i2c_present(self, address):
        if self.i2c_target_pins is not None and self.i2c_pins != self.i2c_target_pins:
            return False
        return address in self.i2c_devices

    def _i2c_faulting(self):
        return self.i2c_stretch_faults and self.i2c_stretch_detect

    def _h_DigitalI2cWrite(self, h, addr, tx, n, nak):
        address = addr.value >> 1
        if self._i2c_faulting():
            _set(nak, -1)
            return
        if not self._i2c_present(address):
            _set(nak, 1)
            return
        data = bytes(tx[:n.value]) if tx is not None and n.value else b""
        self.i2c_writes.append((address, data))
        _set(nak, 0)

    def _h_DigitalI2cRead(self, h, addr, rx, n, nak):
        address = addr.value >> 1
        if self._i2c_faulting():
            _set(nak, -1)
            return
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

    # SWD engine with a simulated Cortex-M4 style SW-DP (DPIDR 0x2ba01477, AHB-AP 0x24770011)
    def _h_DigitalSwdCkSet(self, h, pin): self.swd_pins = (pin.value, self.swd_pins[1])
    def _h_DigitalSwdIoSet(self, h, pin): self.swd_pins = (self.swd_pins[0], pin.value)

    def _swd_connected(self):
        return self.swd_target is not None and self.swd_pins == self.swd_target

    def _h_DigitalSwdRead(self, h, ap, a32, ack, value, crc):
        if not self._swd_connected():
            _set(ack, 7); _set(value, 0xFFFFFFFF); _set(crc, 1)
            return
        _set(crc, 0)
        if ap.value:
            bank = (self.swd_dp["select"] >> 4) & 0xF
            table = {(0xF, 3): 0x24770011, (0xF, 2): 0xE00FF003}
            self.swd_rdbuff = table.get((bank, a32.value), 0)
            _set(ack, 1); _set(value, 0)
            return
        dp = {0: 0x2BA01477, 1: self.swd_dp["ctrl_stat"], 3: self.swd_rdbuff}
        _set(ack, 1); _set(value, dp.get(a32.value, 0))

    def _h_DigitalSwdWrite(self, h, ap, a32, ack, value):
        if not self._swd_connected():
            _set(ack, 7)
            return
        self.swd_writes.append(("ap" if ap.value else "dp", a32.value * 4, value.value))
        if not ap.value:
            if a32.value == 1:      # CTRL/STAT: ACK bits follow the requests
                req = value.value & 0x50000000
                self.swd_dp["ctrl_stat"] = req | (req << 1)
            elif a32.value == 2:
                self.swd_dp["select"] = value.value
        _set(ack, 1)

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

    def _h_AnalogInConfigure(self, h, reconfigure, start):
        if start.value:
            self.analog_offset = 0

    def _h_AnalogInStatusData(self, h, ch, buf, n):
        # record mode reads consecutive chunks: keep the sample index running across calls
        offset = getattr(self, "analog_offset", 0)
        for i in range(n.value):
            buf[i] = self.signal(ch.value, offset + i, self.sample_rate)
        if ch.value == 1 or n.value < self.scope_buffer:
            self.analog_offset = offset + n.value

    def _h_AnalogInStatusSample(self, h, ch, out): _set(out, 1.234)

    def _h_AnalogInStatusRecord(self, h, available, lost, corrupt):
        _set(available, self.record_chunk); _set(lost, 0); _set(corrupt, 0)

    def _h_AnalogInCounterInfo(self, h, count_max, sec_max):
        _set(count_max, 2 ** 32 if self.counter_supported else 0); _set(sec_max, 10.0 if self.counter_supported else 0.0)

    def _h_DigitalInCounterInfo(self, h, count_max, sec_max):
        _set(count_max, 2 ** 32 if self.counter_supported else 0); _set(sec_max, 10.0 if self.counter_supported else 0.0)

    def _h_AnalogInCounterStatus(self, h, count, freq, tick):
        self.counter_tick += 1
        _set(count, 1234.0); _set(freq, self.analog_counter_hz); _set(tick, self.counter_tick)

    def _h_DigitalInStatusRecord(self, h, available, lost, corrupt):
        _set(available, self.record_chunk); _set(lost, 0); _set(corrupt, 0)

    def _h_DigitalInCounterStatus(self, h, count, freq, tick):
        self.counter_tick += 1
        _set(count, 100000.0); _set(freq, self.digital_counter_hz); _set(tick, self.counter_tick)


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
