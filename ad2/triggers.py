"""Trigger source names shared by the scope, logic analyzer, generators and trigger pins."""

import ctypes

from WF_SDK import device
import dwfconstants as constants

from .errors import AD2Error

# Sources any instrument can be started from.
SOURCES = {
    "none": constants.trigsrcNone,
    "pc": constants.trigsrcPC,                        # send_pc_trigger
    "scope": constants.trigsrcDetectorAnalogIn,       # the scope's trigger detector (level/edge on ch1/ch2)
    "logic": constants.trigsrcDetectorDigitalIn,      # the logic analyzer's pin trigger
    "scope_start": constants.trigsrcAnalogIn,         # scope acquisition starting
    "logic_start": constants.trigsrcDigitalIn,        # logic acquisition starting
    "pattern": constants.trigsrcDigitalOut,           # pattern generator starting
    "wavegen1": constants.trigsrcAnalogOut1,
    "wavegen2": constants.trigsrcAnalogOut2,
    "external1": constants.trigsrcExternal1,
    "external2": constants.trigsrcExternal2,
    "external3": constants.trigsrcExternal3,
    "external4": constants.trigsrcExternal4,
}
SLOPES = {
    "rising": constants.DwfTriggerSlopeRise,
    "falling": constants.DwfTriggerSlopeFall,
    "either": constants.DwfTriggerSlopeEither,
}


def source(name: str, allowed=None):
    allowed = allowed or SOURCES
    if name not in allowed:
        raise AD2Error(f"trigger_source must be one of: {', '.join(allowed)}")
    return SOURCES[name]


def slope(name: str):
    if name not in SLOPES:
        raise AD2Error(f"trigger_slope must be one of: {', '.join(SLOPES)}")
    return SLOPES[name]


def set_pin_output(dd, pin: int, name: str) -> dict:
    """Drive trigger pin T1/T2 from an instrument event, or make it an input again with 'none'."""
    if pin not in (1, 2):
        raise AD2Error("pin must be 1 (T1) or 2 (T2)")
    src = source(name, {k: v for k, v in SOURCES.items() if not k.startswith("external")})
    if device.dwf.FDwfDeviceTriggerSet(dd.handle, ctypes.c_int(pin - 1), src) == 0:
        device.check_error()
    return {"pin": f"T{pin}", "source": name, "direction": "input" if name == "none" else "output"}


def pc_trigger(dd) -> dict:
    if device.dwf.FDwfDeviceTriggerPC(dd.handle) == 0:
        device.check_error()
    return {"sent": True}
