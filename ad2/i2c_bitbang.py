"""Bit-banged open-drain I2C master on the static DIO.

A line is driven low by enabling its output (preset to 0) and released by disabling it, so the
bus pull-ups define the high level and clock stretching by the slave is honoured. About 0.5 ms
per bit; used when the WaveForms I2C engine misbehaves on a bus.
"""

import ctypes
import time

from WF_SDK import device

from .errors import AD2Error


class Master:
    def __init__(self, dd, scl: int, sda: int):
        self.dd, self.scl, self.sda = dd, scl, sda
        self.dwf = device.dwf
        enable, value = ctypes.c_uint32(), ctypes.c_uint32()
        self.dwf.FDwfDigitalIOOutputEnableGet(dd.handle, ctypes.byref(enable))
        self.dwf.FDwfDigitalIOOutputGet(dd.handle, ctypes.byref(value))
        self.others = enable.value & ~((1 << scl) | (1 << sda))
        self.enable = self.others
        if self.dwf.FDwfDigitalIOOutputSet(dd.handle, ctypes.c_int(value.value & ~((1 << scl) | (1 << sda)))) == 0:
            device.check_error()
        self._apply()

    def _apply(self):
        if self.dwf.FDwfDigitalIOOutputEnableSet(self.dd.handle, ctypes.c_int(self.enable)) == 0:
            device.check_error()

    def _drive(self, pin: int, level: int):
        self.enable = (self.enable & ~(1 << pin)) if level else (self.enable | (1 << pin))
        self._apply()

    def _read(self, pin: int) -> int:
        if self.dwf.FDwfDigitalIOStatus(self.dd.handle) == 0:
            device.check_error()
        levels = ctypes.c_uint32()
        if self.dwf.FDwfDigitalIOInputStatus(self.dd.handle, ctypes.byref(levels)) == 0:
            device.check_error()
        return (levels.value >> pin) & 1

    def _scl_high(self):
        """Release SCL and wait for it to actually rise (clock stretching), bounded."""
        self._drive(self.scl, 1)
        for _ in range(200):
            if self._read(self.scl):
                return
            time.sleep(0.0005)
        raise AD2Error(f"SCL (DIO{self.scl}) held low by the slave for too long (clock stretch timeout)")

    def bus_free(self) -> bool:
        self._drive(self.scl, 1)
        self._drive(self.sda, 1)
        return bool(self._read(self.scl) and self._read(self.sda))

    def start(self):
        self._drive(self.sda, 1)
        self._scl_high()
        self._drive(self.sda, 0)
        self._drive(self.scl, 0)

    def stop(self):
        self._drive(self.sda, 0)
        self._scl_high()
        self._drive(self.sda, 1)

    def write_byte(self, value: int) -> bool:
        for i in range(7, -1, -1):
            self._drive(self.sda, (value >> i) & 1)
            self._scl_high()
            self._drive(self.scl, 0)
        self._drive(self.sda, 1)
        self._scl_high()
        ack = self._read(self.sda) == 0
        self._drive(self.scl, 0)
        return ack

    def read_byte(self, ack: bool) -> int:
        self._drive(self.sda, 1)
        value = 0
        for _ in range(8):
            self._scl_high()
            value = (value << 1) | self._read(self.sda)
            self._drive(self.scl, 0)
        self._drive(self.sda, 0 if ack else 1)
        self._scl_high()
        self._drive(self.scl, 0)
        self._drive(self.sda, 1)
        return value

    def release(self):
        self.enable = self.others
        self._apply()


def _nak_index(acks) -> int:
    """0 = all acknowledged, else 1-based index of the first NAK (1 = address)."""
    for i, ack in enumerate(acks):
        if not ack:
            return i + 1
    return 0


def write(dd, scl, sda, address, payload: bytes) -> int:
    m = Master(dd, scl, sda)
    try:
        if not m.bus_free():
            return -1
        m.start()
        acks = [m.write_byte(address << 1)]
        if acks[0]:
            for b in payload:
                acks.append(m.write_byte(b))
                if not acks[-1]:
                    break
        m.stop()
        return _nak_index(acks)
    finally:
        m.release()


def read(dd, scl, sda, address, count) -> tuple[int, bytes]:
    m = Master(dd, scl, sda)
    try:
        if not m.bus_free():
            return -1, b""
        m.start()
        if not m.write_byte((address << 1) | 1):
            m.stop()
            return 1, b""
        data = bytes(m.read_byte(i < count - 1) for i in range(count))
        m.stop()
        return 0, data
    finally:
        m.release()


def write_read(dd, scl, sda, address, payload: bytes, count) -> tuple[int, bytes]:
    m = Master(dd, scl, sda)
    try:
        if not m.bus_free():
            return -1, b""
        m.start()
        acks = [m.write_byte(address << 1)]
        if acks[0]:
            for b in payload:
                acks.append(m.write_byte(b))
                if not acks[-1]:
                    break
        nak = _nak_index(acks)
        if nak:
            m.stop()
            return nak, b""
        m.start()                                   # repeated start
        if not m.write_byte((address << 1) | 1):
            m.stop()
            return len(payload) + 2, b""
        data = bytes(m.read_byte(i < count - 1) for i in range(count))
        m.stop()
        return 0, data
    finally:
        m.release()


def scan(dd, scl, sda, first, last):
    """Returns (found addresses, bus_error flag)."""
    m = Master(dd, scl, sda)
    try:
        if not m.bus_free():
            return [], True
        found = []
        for address in range(first, last + 1):
            m.start()
            if m.write_byte(address << 1):
                found.append(address)
            m.stop()
        return found, False
    finally:
        m.release()
