"""UART on two DIO pins: send with optional reply capture, or receive-only listening."""

import ctypes
import time

from WF_SDK import device

from .bytesutil import format_bytes
from .digital import check_pins
from .errors import AD2Error

PARITY = {"none": 0, "even": 1, "odd": 2, "mark": 3, "space": 4}
STOP_BITS = (1.0, 1.5, 2.0)
RX_CHUNK = 8192


def _dwf():
    return device.dwf


def configure(session, tx_pin, rx_pin, baud_rate, data_bits, parity, stop_bits, init_tx: bool):
    """Apply settings when they change. TX is only driven (idle high) once init_tx has been requested,
    so a receive-only session leaves the TX pin alone."""
    dd = session.ensure()
    dwf = _dwf()
    (tx_pin,) = check_pins(dd, [tx_pin], "tx_pin")
    (rx_pin,) = check_pins(dd, [rx_pin], "rx_pin")
    if tx_pin == rx_pin:
        raise AD2Error("tx_pin and rx_pin must be different pins")
    if baud_rate <= 0:
        raise AD2Error("baud_rate must be positive")
    if not 5 <= data_bits <= 8:
        raise AD2Error("data_bits must be between 5 and 8")
    if parity not in PARITY:
        raise AD2Error(f"parity must be one of: {', '.join(PARITY)}")
    if float(stop_bits) not in STOP_BITS:
        raise AD2Error("stop_bits must be 1, 1.5 or 2")
    key = (tx_pin, rx_pin, float(baud_rate), int(data_bits), parity, float(stop_bits))
    if session.uart_settings != key:
        if dwf.FDwfDigitalUartRateSet(dd.handle, ctypes.c_double(baud_rate)) == 0:
            device.check_error()
        if dwf.FDwfDigitalUartTxSet(dd.handle, ctypes.c_int(tx_pin)) == 0:
            device.check_error()
        if dwf.FDwfDigitalUartRxSet(dd.handle, ctypes.c_int(rx_pin)) == 0:
            device.check_error()
        if dwf.FDwfDigitalUartBitsSet(dd.handle, ctypes.c_int(data_bits)) == 0:
            device.check_error()
        if dwf.FDwfDigitalUartParitySet(dd.handle, ctypes.c_int(PARITY[parity])) == 0:
            device.check_error()
        if dwf.FDwfDigitalUartStopSet(dd.handle, ctypes.c_double(stop_bits)) == 0:
            device.check_error()
        count = ctypes.c_int()
        parity_flag = ctypes.c_int()
        if dwf.FDwfDigitalUartRx(dd.handle, None, ctypes.c_int(0), ctypes.byref(count),
                                 ctypes.byref(parity_flag)) == 0:
            device.check_error()
        session.uart_settings = key
        session.uart_tx_ready = False
    if init_tx and not session.uart_tx_ready:
        if dwf.FDwfDigitalUartTx(dd.handle, None, ctypes.c_int(0)) == 0:
            device.check_error()
        session.uart_tx_ready = True
    return dd


def _poll(dd, limit: int):
    """One receive call: returns (bytes, parity_error_flag)."""
    buffer = (ctypes.c_ubyte * limit)()
    count = ctypes.c_int()
    parity_flag = ctypes.c_int()
    if _dwf().FDwfDigitalUartRx(dd.handle, buffer, ctypes.c_int(limit), ctypes.byref(count),
                                ctypes.byref(parity_flag)) == 0:
        device.check_error()
    return bytes(buffer[:count.value]), parity_flag.value != 0


def collect(dd, timeout_s: float, idle_timeout_s: float, max_bytes: int) -> dict:
    """Gather bytes until max_bytes, or until data stops for idle_timeout_s, or until timeout_s."""
    received = bytearray()
    parity_errors = 0
    reason = "timeout"
    start = time.monotonic()
    last_data = start
    while True:
        chunk, parity_error = _poll(dd, min(RX_CHUNK, max_bytes - len(received)))
        if parity_error:
            parity_errors += 1
        if chunk:
            received += chunk
            last_data = time.monotonic()
        if len(received) >= max_bytes:
            reason = "max_bytes"
            break
        now = time.monotonic()
        if received and idle_timeout_s > 0 and now - last_data >= idle_timeout_s:
            reason = "idle"
            break
        if now - start >= timeout_s:
            break
        time.sleep(0.002)
    out = format_bytes(received)
    out["text"] = received.decode("utf-8", errors="replace")
    return {"received": out, "parity_errors": parity_errors, "stop_reason": reason,
            "elapsed_s": round(time.monotonic() - start, 4)}


def send(session, payload: bytes, tx_pin, rx_pin, baud_rate, data_bits, parity, stop_bits,
         receive_timeout_s: float, idle_timeout_s: float = 0.1, max_bytes: int = RX_CHUNK) -> dict:
    if not payload:
        raise AD2Error("data is empty")
    if receive_timeout_s < 0:
        raise AD2Error("receive_timeout_s must be zero or positive")
    dd = configure(session, tx_pin, rx_pin, baud_rate, data_bits, parity, stop_bits, init_tx=True)
    stale, _ = _poll(dd, RX_CHUNK)  # discard anything that arrived before this exchange
    buffer = (ctypes.c_ubyte * len(payload))(*payload)
    if _dwf().FDwfDigitalUartTx(dd.handle, buffer, ctypes.c_int(len(payload))) == 0:
        device.check_error()
    result = {"sent": format_bytes(payload), "discarded_stale_bytes": len(stale)}
    result["sent"]["text"] = payload.decode("utf-8", errors="replace")
    if receive_timeout_s > 0:
        result.update(collect(dd, receive_timeout_s, idle_timeout_s, max_bytes))
    return result


def receive(session, tx_pin, rx_pin, baud_rate, data_bits, parity, stop_bits,
            timeout_s: float, idle_timeout_s: float, max_bytes: int) -> dict:
    if timeout_s <= 0 or timeout_s > 60:
        raise AD2Error("timeout_s must be between 0 and 60 seconds")
    if idle_timeout_s < 0:
        raise AD2Error("idle_timeout_s must be zero (disabled) or positive")
    if not 1 <= max_bytes <= 1_000_000:
        raise AD2Error("max_bytes must be between 1 and 1000000")
    dd = configure(session, tx_pin, rx_pin, baud_rate, data_bits, parity, stop_bits, init_tx=False)
    return collect(dd, timeout_s, idle_timeout_s, max_bytes)
