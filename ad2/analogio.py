"""AnalogIO: programmable power supplies and the USB / temperature monitor."""

import ctypes
import time

from WF_SDK import device

from .errors import AD2Error

# Measured supply readings lag a new setpoint by a few hundred milliseconds.
SETTLE_S = 0.3


def _dwf():
    return device.dwf


def find_node(dd, label: str, node_name: str) -> tuple[int, int] | None:
    io = dd.analog.IO
    for ch in range(io.channel_count):
        if io.channel_label[ch] == label:
            for nd in range(io.node_count[ch]):
                if io.node_name[ch][nd] == node_name:
                    return ch, nd
    return None


def _refresh(dd) -> None:
    if _dwf().FDwfAnalogIOStatus(dd.handle) == 0:
        device.check_error()


def _read(dd, ch: int, nd: int) -> float:
    value = ctypes.c_double()
    if _dwf().FDwfAnalogIOChannelNodeStatus(dd.handle, ctypes.c_int(ch), ctypes.c_int(nd), ctypes.byref(value)) == 0:
        device.check_error()
    return value.value


def _write(dd, ch: int, nd: int, value: float) -> None:
    if _dwf().FDwfAnalogIOChannelNodeSet(dd.handle, ctypes.c_int(ch), ctypes.c_int(nd), ctypes.c_double(value)) == 0:
        device.check_error()


def _setpoint(dd, ch: int, nd: int) -> float:
    value = ctypes.c_double()
    if _dwf().FDwfAnalogIOChannelNodeGet(dd.handle, ctypes.c_int(ch), ctypes.c_int(nd), ctypes.byref(value)) == 0:
        device.check_error()
    return value.value


def _get(dd, label: str, node_name: str) -> float | None:
    """Measured value (status) of a node, or None if the device lacks it."""
    loc = find_node(dd, label, node_name)
    return _read(dd, *loc) if loc else None


def _get_setpoint(dd, label: str, node_name: str) -> float | None:
    loc = find_node(dd, label, node_name)
    return _setpoint(dd, *loc) if loc else None


def master_enabled(dd) -> bool:
    """Master switch setpoint (what was requested)."""
    flag = ctypes.c_int()
    if _dwf().FDwfAnalogIOEnableGet(dd.handle, ctypes.byref(flag)) == 0:
        device.check_error()
    return bool(flag.value)


def master_active(dd) -> bool:
    """Master switch status (what the hardware reports)."""
    flag = ctypes.c_int()
    if _dwf().FDwfAnalogIOEnableStatus(dd.handle, ctypes.byref(flag)) == 0:
        device.check_error()
    return bool(flag.value)


def read_all(dd) -> dict:
    """Every AnalogIO reading, grouped by channel label (V+, V-, USB, Aux, ...)."""
    _refresh(dd)
    io = dd.analog.IO
    out = {}
    for ch in range(io.channel_count):
        nodes = {}
        for nd in range(io.node_count[ch]):
            nodes[io.node_name[ch][nd]] = {"value": _read(dd, ch, nd), "unit": io.node_unit[ch][nd]}
        out[io.channel_label[ch]] = {"name": io.channel_name[ch], "nodes": nodes}
    return out


def system_monitor(dd) -> dict:
    _refresh(dd)
    temperature = _get(dd, "USB", "Temperature")
    if temperature is None:
        temperature = _get(dd, "System", "Temp")
    return {
        "usb_voltage_v": _get(dd, "USB", "Voltage"),
        "usb_current_a": _get(dd, "USB", "Current"),
        "temperature_c": temperature,
        "aux_voltage_v": _get(dd, "Aux", "Voltage"),
        "aux_current_a": _get(dd, "Aux", "Current"),
    }


def supplies_status(dd) -> dict:
    """Setpoints (enabled, voltage_setpoint_v) and measured values (active, voltage_v, current_a)."""
    _refresh(dd)

    def measurable(label, node_name):
        loc = find_node(dd, label, node_name)
        return loc is not None and dd.analog.IO.max_read_range[loc[0]][loc[1]] > 0

    def rail(label):
        enabled = _get_setpoint(dd, label, "Enable")
        active = _get(dd, label, "Enable")
        out = {
            "enabled": None if enabled is None else bool(enabled),
            "voltage_setpoint_v": _get_setpoint(dd, label, "Voltage"),
            "active": None if active is None else bool(active),
            "voltage_v": _get(dd, label, "Voltage"),
        }
        # The AD2 cannot measure supply current (the node's readable range is 0); its status value
        # is just the limit setting. Report a measurement only where the device can make one.
        if measurable(label, "Current"):
            out["current_a"] = _get(dd, label, "Current")
        else:
            out["current_a"] = None
            out["current_note"] = "not measurable on this device; use the USB current in get_system_status"
        return out

    return {
        "master_enabled": master_enabled(dd),
        "master_active": master_active(dd),
        "positive": rail("V+"),
        "negative": rail("V-"),
    }


def set_supplies(dd, positive_volts=None, negative_volts=None,
                 positive_enabled=None, negative_enabled=None, master_enabled=True) -> dict:
    """Program V+ / V-. Voltages are validated against the device's settable range, never clamped."""
    io = dd.analog.IO
    for label, volts, enabled in (("V+", positive_volts, positive_enabled),
                                  ("V-", negative_volts, negative_enabled)):
        if volts is not None:
            loc = find_node(dd, label, "Voltage")
            if loc is None:
                raise AD2Error(f"This device has no {label} supply")
            lo, hi = io.min_set_range[loc[0]][loc[1]], io.max_set_range[loc[0]][loc[1]]
            if not lo <= volts <= hi:
                rail = "positive" if label == "V+" else "negative"
                raise AD2Error(
                    f"{label} voltage {volts} V is outside the settable range {lo} V to {hi} V. "
                    f"To turn the rail off use {rail}_enabled=false."
                )
            _write(dd, *loc, float(volts))
        if enabled is not None:
            loc = find_node(dd, label, "Enable")
            if loc is None:
                raise AD2Error(f"This device has no {label} supply")
            _write(dd, *loc, 1.0 if enabled else 0.0)
    if _dwf().FDwfAnalogIOEnableSet(dd.handle, ctypes.c_int(1 if master_enabled else 0)) == 0:
        device.check_error()
    if _dwf().FDwfAnalogIOConfigure(dd.handle) == 0:
        device.check_error()
    time.sleep(SETTLE_S)
    return supplies_status(dd)


def disable_supplies(dd, settle: bool = True) -> dict:
    for label in ("V+", "V-"):
        loc = find_node(dd, label, "Enable")
        if loc:
            _write(dd, *loc, 0.0)
    if _dwf().FDwfAnalogIOEnableSet(dd.handle, ctypes.c_int(0)) == 0:
        device.check_error()
    if _dwf().FDwfAnalogIOConfigure(dd.handle) == 0:
        device.check_error()
    if settle:
        time.sleep(SETTLE_S)
    return supplies_status(dd)
