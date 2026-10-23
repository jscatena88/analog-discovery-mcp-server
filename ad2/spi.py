"""SPI master on four DIO pins (full-duplex transfers, 1-8 bit words)."""

import ctypes

from WF_SDK import device
import dwfconstants as constants

from .bytesutil import format_bytes, parse_bytes
from .digital import check_pins
from .errors import AD2Error

MAX_WORDS = 4096


def _dwf():
    return device.dwf


def configure(session, clock_pin, mosi_pin, miso_pin, select_pin, frequency, mode, msb_first, select_active_low):
    dd = session.ensure()
    dwf = _dwf()
    pins = {}
    for name, pin in (("clock_pin", clock_pin), ("mosi_pin", mosi_pin), ("miso_pin", miso_pin),
                      ("select_pin", select_pin)):
        (pins[name],) = check_pins(dd, [pin], name)
    if len(set(pins.values())) != 4:
        raise AD2Error("clock_pin, mosi_pin, miso_pin and select_pin must all be different pins")
    if frequency <= 0:
        raise AD2Error("frequency must be positive")
    if mode not in (0, 1, 2, 3):
        raise AD2Error("mode must be 0, 1, 2 or 3 (CPOL/CPHA)")
    key = (pins["clock_pin"], pins["mosi_pin"], pins["miso_pin"], pins["select_pin"], float(frequency),
           int(mode), bool(msb_first), bool(select_active_low))
    if session.spi_settings != key:
        handle = dd.handle
        if dwf.FDwfDigitalSpiFrequencySet(handle, ctypes.c_double(frequency)) == 0:
            device.check_error()
        if dwf.FDwfDigitalSpiClockSet(handle, ctypes.c_int(pins["clock_pin"])) == 0:
            device.check_error()
        if dwf.FDwfDigitalSpiDataSet(handle, ctypes.c_int(0), ctypes.c_int(pins["mosi_pin"])) == 0:
            device.check_error()
        if dwf.FDwfDigitalSpiDataSet(handle, ctypes.c_int(1), ctypes.c_int(pins["miso_pin"])) == 0:
            device.check_error()
        for dq in (0, 1):
            if dwf.FDwfDigitalSpiIdleSet(handle, ctypes.c_int(dq), constants.DwfDigitalOutIdleZet) == 0:
                device.check_error()
        if dwf.FDwfDigitalSpiModeSet(handle, ctypes.c_int(mode)) == 0:
            device.check_error()
        if dwf.FDwfDigitalSpiOrderSet(handle, ctypes.c_int(1 if msb_first else 0)) == 0:
            device.check_error()
        if dwf.FDwfDigitalSpiSelectSet(handle, ctypes.c_int(pins["select_pin"]),
                                       ctypes.c_int(1 if select_active_low else 0)) == 0:
            device.check_error()
        # A zero-length write starts driving clock, data and select at their idle levels.
        if dwf.FDwfDigitalSpiWriteOne(handle, ctypes.c_int(1), ctypes.c_int(0), ctypes.c_int(0)) == 0:
            device.check_error()
        session.spi_settings = key
    return dd


def transfer(session, write, read_count, bits_per_word, fill_byte, clock_pin, mosi_pin, miso_pin,
             select_pin, frequency, mode, msb_first, select_active_low) -> dict:
    """Full-duplex transfer of max(len(write), read_count) words inside one select assertion."""
    if not 1 <= bits_per_word <= 8:
        raise AD2Error("bits_per_word must be between 1 and 8 (wider words are not supported yet)")
    if not 0 <= fill_byte <= 255:
        raise AD2Error("fill_byte must be between 0 and 255")
    if read_count < 0:
        raise AD2Error("read_count must be zero or positive")
    payload = parse_bytes(write, "write")
    words = max(len(payload), read_count)
    if words == 0:
        raise AD2Error("nothing to transfer: give bytes in write and/or a positive read_count")
    if words > MAX_WORDS:
        raise AD2Error(f"a single transfer is limited to {MAX_WORDS} words")
    limit = (1 << bits_per_word) - 1
    for value in list(payload) + [fill_byte]:
        if value > limit:
            raise AD2Error(f"value 0x{value:02x} does not fit in {bits_per_word}-bit words")
    dd = configure(session, clock_pin, mosi_pin, miso_pin, select_pin, frequency, mode, msb_first,
                   select_active_low)
    tx_bytes = payload + bytes([fill_byte]) * (words - len(payload))
    tx = (ctypes.c_ubyte * words)(*tx_bytes)
    rx = (ctypes.c_ubyte * words)()
    if _dwf().FDwfDigitalSpiWriteRead(dd.handle, ctypes.c_int(1), ctypes.c_int(bits_per_word), tx,
                                      ctypes.c_int(words), rx, ctypes.c_int(words)) == 0:
        device.check_error()
    return {
        "words": words,
        "bits_per_word": bits_per_word,
        "written": format_bytes(tx_bytes),
        "read": format_bytes(bytes(rx)),
        "frequency_hz": frequency,
        "mode": mode,
    }
