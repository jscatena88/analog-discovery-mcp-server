"""UART, SPI and I2C tools against the fake libdwf."""

import pytest

from ad2 import bytesutil
from conftest import call_tool


# --------------------------------------------------------------------------- byte helpers

def test_parse_bytes_forms():
    assert bytesutil.parse_bytes("01 ab FF") == b"\x01\xab\xff"
    assert bytesutil.parse_bytes("01abff") == b"\x01\xab\xff"
    assert bytesutil.parse_bytes("0x01, 0xAB") == b"\x01\xab"
    assert bytesutil.parse_bytes([1, 171, 255]) == b"\x01\xab\xff"
    assert bytesutil.parse_bytes(None) == b""
    for bad in ("zz", "123", [256], [1.5]):
        with pytest.raises(Exception):
            bytesutil.parse_bytes(bad)
    assert bytesutil.format_bytes(b"\x01\xab") == {"hex": "01 ab", "bytes": [1, 171], "length": 2}


# --------------------------------------------------------------------------- UART

def test_uart_send_loops_back(server, fake_dwf):
    out = call_tool(server, "uart_send", data="Hello\r\n", tx_pin=0, rx_pin=8, baud_rate=115200)
    assert out["sent"]["text"] == "Hello\r\n" and out["sent"]["length"] == 7
    assert out["received"]["text"] == "Hello\r\n"
    assert out["received"]["hex"] == "48 65 6c 6c 6f 0d 0a"
    assert out["stop_reason"] == "idle" and out["parity_errors"] == 0
    assert fake_dwf.uart_tx_log == [b"Hello\r\n"]
    (rate,) = fake_dwf.calls_to("FDwfDigitalUartRateSet")
    assert rate[1].value == 115200
    assert fake_dwf.calls_to("FDwfDigitalUartTxSet")[0][1].value == 0
    assert fake_dwf.calls_to("FDwfDigitalUartRxSet")[0][1].value == 8


def test_uart_send_hex_and_settings_reused(server, fake_dwf):
    call_tool(server, "uart_send", data="01 02 ff", data_format="hex", receive_timeout_s=0)
    call_tool(server, "uart_send", data="aa", data_format="hex", receive_timeout_s=0)
    assert fake_dwf.uart_tx_log == [b"\x01\x02\xff", b"\xaa"]
    assert len(fake_dwf.calls_to("FDwfDigitalUartRateSet")) == 1
    call_tool(server, "uart_send", data="x", baud_rate=19200, receive_timeout_s=0)
    assert len(fake_dwf.calls_to("FDwfDigitalUartRateSet")) == 2


def test_uart_receive_returns_buffered_bytes(server, fake_dwf):
    call_tool(server, "uart_send", data="ping", receive_timeout_s=0)
    out = call_tool(server, "uart_receive", timeout_s=1.0, idle_timeout_s=0.05)
    assert out["received"]["text"] == "ping" and out["stop_reason"] == "idle"
    out = call_tool(server, "uart_receive", timeout_s=0.05, idle_timeout_s=0.02)
    assert out["received"]["length"] == 0 and out["stop_reason"] == "timeout"


def test_uart_receive_only_never_initializes_tx(server, fake_dwf):
    call_tool(server, "uart_receive", tx_pin=5, rx_pin=8, timeout_s=0.02, idle_timeout_s=0)
    assert not fake_dwf.calls_to("FDwfDigitalUartTx")       # Tx(None) init is skipped
    assert fake_dwf.calls_to("FDwfDigitalUartRx")[0][1] is None  # Rx init happened


def test_uart_rejects_bad_settings(server, fake_dwf):
    with pytest.raises(RuntimeError, match="parity must be"):
        call_tool(server, "uart_send", data="x", parity="strong")
    with pytest.raises(RuntimeError, match="different pins"):
        call_tool(server, "uart_send", data="x", tx_pin=3, rx_pin=3)
    with pytest.raises(RuntimeError, match="stop_bits"):
        call_tool(server, "uart_send", data="x", stop_bits=3)
    with pytest.raises(RuntimeError, match="data_format"):
        call_tool(server, "uart_send", data="x", data_format="base64")
    with pytest.raises(RuntimeError, match="hex bytes"):
        call_tool(server, "uart_send", data="hello", data_format="hex")
    assert not fake_dwf.calls_to("FDwfDigitalUartRateSet")


# --------------------------------------------------------------------------- SPI

def test_spi_transfer_loops_back(server, fake_dwf):
    out = call_tool(server, "spi_transfer", write="a5 01 ff", clock_pin=1, mosi_pin=0, miso_pin=8,
                    select_pin=2, frequency=2e6, mode=3)
    assert out["read"]["hex"] == "a5 01 ff" and out["words"] == 3
    assert out["mode"] == 3 and out["frequency_hz"] == 2e6
    (freq,) = fake_dwf.calls_to("FDwfDigitalSpiFrequencySet")
    assert freq[1].value == 2e6
    data_pins = {a[1].value: a[2].value for a in fake_dwf.calls_to("FDwfDigitalSpiDataSet")}
    assert data_pins == {0: 0, 1: 8}
    (sel,) = fake_dwf.calls_to("FDwfDigitalSpiSelectSet")
    assert (sel[1].value, sel[2].value) == (2, 1)
    (wr,) = fake_dwf.calls_to("FDwfDigitalSpiWriteRead")
    assert wr[2].value == 8 and wr[4].value == 3


def test_spi_read_pads_with_fill_byte(server, fake_dwf):
    out = call_tool(server, "spi_transfer", write=[0x9F], read_count=4, fill_byte=0xFF)
    assert out["written"]["hex"] == "9f ff ff ff"
    assert out["read"]["hex"] == "9f ff ff ff"
    call_tool(server, "spi_transfer", write=[1])
    assert len(fake_dwf.calls_to("FDwfDigitalSpiFrequencySet")) == 1   # settings reused


def test_spi_wide_words_are_packed_into_bytes(server, fake_dwf):
    out = call_tool(server, "spi_transfer", write=[0x1234, 0xBEEF], read_count=3, bits_per_word=16, fill_byte=0xFF)
    assert out["written"]["hex"] == "12 34 be ef ff ff"
    assert out["written_words"] == [0x1234, 0xBEEF, 0xFFFF]
    assert out["read_words"] == [0x1234, 0xBEEF, 0xFFFF] and out["words"] == 3
    (wr,) = fake_dwf.calls_to("FDwfDigitalSpiWriteRead")
    assert wr[2].value == 8 and wr[4].value == 6                 # engine sees 6 eight-bit words
    out = call_tool(server, "spi_transfer", write=[0x01020304], bits_per_word=32, msb_first=False)
    assert out["written"]["hex"] == "04 03 02 01" and out["read_words"] == [0x01020304]
    out = call_tool(server, "spi_transfer", write=[0xABCDEF], bits_per_word=24)
    assert out["written"]["hex"] == "ab cd ef"
    with pytest.raises(RuntimeError, match="between 0 and 65535"):
        call_tool(server, "spi_transfer", write=[0x10000], bits_per_word=16)
    with pytest.raises(RuntimeError, match="list of integers"):
        call_tool(server, "spi_transfer", write="12 34", bits_per_word=16)


def test_spi_rejects_bad_arguments(server, fake_dwf):
    with pytest.raises(RuntimeError, match="all be different"):
        call_tool(server, "spi_transfer", write=[1], clock_pin=1, mosi_pin=1)
    with pytest.raises(RuntimeError, match="does not fit in 4-bit"):
        call_tool(server, "spi_transfer", write=[0x1F], bits_per_word=4)
    with pytest.raises(RuntimeError, match="16, 24 or 32"):
        call_tool(server, "spi_transfer", write=[1], bits_per_word=12)
    with pytest.raises(RuntimeError, match="nothing to transfer"):
        call_tool(server, "spi_transfer")
    with pytest.raises(RuntimeError, match="mode must be"):
        call_tool(server, "spi_transfer", write=[1], mode=4)
    assert not fake_dwf.calls_to("FDwfDigitalSpiWriteRead")


# --------------------------------------------------------------------------- I2C

def test_i2c_scan_finds_fake_slave(server):
    out = call_tool(server, "i2c_scan", scl_pin=3, sda_pin=4)
    assert out == {"scanned": "0x08-0x77", "found": ["0x1d"], "count": 1}


def test_i2c_write_read_and_write_read(server, fake_dwf):
    out = call_tool(server, "i2c_write", address=0x1D, data="10 20", scl_pin=3, sda_pin=4)
    assert out["acknowledged"] is True and out["address"] == "0x1d"
    assert fake_dwf.i2c_writes == [(0x1D, b"\x10\x20")]
    out = call_tool(server, "i2c_read", address=0x1D, count=4, scl_pin=3, sda_pin=4)
    assert out["read"]["hex"] == "00 01 02 03"
    out = call_tool(server, "i2c_write_read", address=0x1D, data=[5], count=3, scl_pin=3, sda_pin=4)
    assert out["read"]["bytes"] == [5, 6, 7] and out["written"]["bytes"] == [5]
    assert len(fake_dwf.calls_to("FDwfDigitalI2cRateSet")) == 1


def test_i2c_nak_is_reported_not_raised(server):
    out = call_tool(server, "i2c_write", address=0x20, data=[1])
    assert out["acknowledged"] is False and out["nak_index"] == 1
    assert "no device answered" in out["note"]
    out = call_tool(server, "i2c_read", address=0x20, count=2)
    assert out["acknowledged"] is False and out["read"]["length"] == 0


def test_i2c_bus_not_free_gives_wiring_hint(server, fake_dwf):
    fake_dwf.i2c_bus_free = 0
    with pytest.raises(RuntimeError, match="pull-up"):
        call_tool(server, "i2c_scan")
    with pytest.raises(RuntimeError, match="DIO4/DIO3"):
        call_tool(server, "i2c_write", address=0x1D, data=[1], scl_pin=3, sda_pin=4)
    assert not fake_dwf.calls_to("FDwfDigitalI2cWrite")


def test_i2c_rejects_bad_arguments(server):
    with pytest.raises(RuntimeError, match="7-bit"):
        call_tool(server, "i2c_write", address=0x80, data=[1])
    with pytest.raises(RuntimeError, match="different pins"):
        call_tool(server, "i2c_scan", scl_pin=2, sda_pin=2)
    with pytest.raises(RuntimeError, match="at least one byte"):
        call_tool(server, "i2c_write_read", address=0x1D, data=[], count=1)


def test_i2c_spy_reassembles_messages(server, fake_dwf):
    fake_dwf.i2c_spy_fragments = [
        (1, 0, bytes([0x1D << 1, 0x05]), 0),          # start, write to 0x1d, register 5
        (2, 0, bytes([(0x1D << 1) | 1, 0xAA]), 0),    # restart, read
        (0, 1, bytes([0xBB]), 0),                     # more data, stop
        (1, 1, bytes([0x20 << 1]), 1),                # start, 0x20 NAKs the address, stop
    ]
    out = call_tool(server, "i2c_spy", duration_s=0.05)
    assert out["count"] == 3
    first, second, third = out["messages"]
    assert first == {"restart": False, "address": "0x1d", "direction": "write", "data": [5], "nak": 0,
                     "stop": False, "data_hex": "05"}
    assert second["restart"] is True and second["direction"] == "read"
    assert second["data"] == [0xAA, 0xBB] and second["stop"] is True
    assert third["address"] == "0x20" and third["nak"] == 1
    assert fake_dwf.calls_to("FDwfDigitalI2cSpyStart") and fake_dwf.calls_to("FDwfDigitalI2cReset")


def test_disconnect_resets_protocol_engines(server, fake_dwf):
    call_tool(server, "uart_send", data="x", receive_timeout_s=0)
    call_tool(server, "disconnect_device")
    for name in ("FDwfDigitalUartReset", "FDwfDigitalSpiReset", "FDwfDigitalI2cReset"):
        assert fake_dwf.calls_to(name), name
