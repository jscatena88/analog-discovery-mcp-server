"""SPI master on four DIO pins.

Words of 1-8 bits go through the engine directly. 16-, 24- and 32-bit words are packed into
bytes and sent as 8-bit words inside the same select assertion, which is bit-identical on the
wire (the engine's own wide-word calls dropped data in loopback tests).
"""

import ctypes

from WF_SDK import device
import dwfconstants as constants

from .bytesutil import format_bytes, parse_bytes
from .digital import check_pins
from .errors import AD2Error

MAX_WORDS = 4096
WIDE_WORDS = (16, 24, 32)


def parse_words(data, bits_per_word: int, name: str = "write") -> list[int]:
    """Words as a list of ints, or (for 8-bit words) any form parse_bytes accepts."""
    if bits_per_word <= 8:
        return list(parse_bytes(data, name))
    if data is None:
        return []
    if isinstance(data, str):
        raise AD2Error(f"{name} must be a list of integers when bits_per_word is {bits_per_word}")
    limit = (1 << bits_per_word) - 1
    words = []
    for item in data:
        if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= limit:
            raise AD2Error(f"{name} must contain integers between 0 and {limit} for {bits_per_word}-bit words, got {item!r}")
        words.append(item)
    return words


def pack_words(words, bits_per_word: int, msb_first: bool) -> bytes:
    size = bits_per_word // 8
    order = "big" if msb_first else "little"
    return b"".join(w.to_bytes(size, order) for w in words)


def unpack_words(data: bytes, bits_per_word: int, msb_first: bool) -> list[int]:
    size = bits_per_word // 8
    order = "big" if msb_first else "little"
    return [int.from_bytes(data[i:i + size], order) for i in range(0, len(data) - size + 1, size)]


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
    if not (1 <= bits_per_word <= 8 or bits_per_word in WIDE_WORDS):
        raise AD2Error("bits_per_word must be between 1 and 8, or 16, 24 or 32")
    if not 0 <= fill_byte <= 255:
        raise AD2Error("fill_byte must be between 0 and 255")
    if read_count < 0:
        raise AD2Error("read_count must be zero or positive")
    words_in = parse_words(write, bits_per_word)
    words = max(len(words_in), read_count)
    if words == 0:
        raise AD2Error("nothing to transfer: give data in write and/or a positive read_count")
    wide = bits_per_word in WIDE_WORDS
    bytes_per_word = bits_per_word // 8 if wide else 1
    if words * bytes_per_word > MAX_WORDS:
        raise AD2Error(f"a single transfer is limited to {MAX_WORDS} bytes on the wire")
    if not wide:
        limit = (1 << bits_per_word) - 1
        for value in words_in + [fill_byte]:
            if value > limit:
                raise AD2Error(f"value 0x{value:02x} does not fit in {bits_per_word}-bit words")
    dd = configure(session, clock_pin, mosi_pin, miso_pin, select_pin, frequency, mode, msb_first,
                   select_active_low)
    if wide:
        fill_word = int.from_bytes(bytes([fill_byte]) * bytes_per_word, "big")
        tx_words = words_in + [fill_word] * (words - len(words_in))
        tx_bytes = pack_words(tx_words, bits_per_word, msb_first)
        engine_bits = 8
    else:
        tx_bytes = bytes(words_in) + bytes([fill_byte]) * (words - len(words_in))
        engine_bits = bits_per_word
    count = len(tx_bytes)
    tx = (ctypes.c_ubyte * count)(*tx_bytes)
    rx = (ctypes.c_ubyte * count)()
    if _dwf().FDwfDigitalSpiWriteRead(dd.handle, ctypes.c_int(1), ctypes.c_int(engine_bits), tx,
                                      ctypes.c_int(count), rx, ctypes.c_int(count)) == 0:
        device.check_error()
    rx_bytes = bytes(rx)
    result = {
        "words": words,
        "bits_per_word": bits_per_word,
        "written": format_bytes(tx_bytes),
        "read": format_bytes(rx_bytes),
        "frequency_hz": frequency,
        "mode": mode,
    }
    if wide:
        result["written_words"] = tx_words
        result["read_words"] = unpack_words(rx_bytes, bits_per_word, msb_first)
    return result
