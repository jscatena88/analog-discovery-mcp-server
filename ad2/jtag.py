"""JTAG over bit-banged static DIO: TAP state machine, IR/DR scans, IDCODE and chain length.

The WaveForms SDK has no JTAG engine, so every TCK cycle is a few USB round trips (a few ms per
bit). That is fine for identification, boundary-scan experiments and small register accesses,
not for programming.
"""

import ctypes

from WF_SDK import device

from .digital import check_pins
from .errors import AD2Error

# TAP state machine: state -> (next state when TMS=0, next state when TMS=1)
STATES = {
    "reset": ("idle", "reset"),
    "idle": ("idle", "select_dr"),
    "select_dr": ("capture_dr", "select_ir"),
    "capture_dr": ("shift_dr", "exit1_dr"),
    "shift_dr": ("shift_dr", "exit1_dr"),
    "exit1_dr": ("pause_dr", "update_dr"),
    "pause_dr": ("pause_dr", "exit2_dr"),
    "exit2_dr": ("shift_dr", "update_dr"),
    "update_dr": ("idle", "select_dr"),
    "select_ir": ("capture_ir", "reset"),
    "capture_ir": ("shift_ir", "exit1_ir"),
    "shift_ir": ("shift_ir", "exit1_ir"),
    "exit1_ir": ("pause_ir", "update_ir"),
    "pause_ir": ("pause_ir", "exit2_ir"),
    "exit2_ir": ("shift_ir", "update_ir"),
    "update_ir": ("idle", "select_dr"),
}
MAX_SCAN_BITS = 4096
JEDEC = {0x023B: "ARM", 0x0049: "Xilinx", 0x006E: "Altera/Intel", 0x0040: "Lattice", 0x0029: "Microchip",
         0x0017: "Texas Instruments", 0x0020: "STMicroelectronics", 0x0015: "NXP", 0x001F: "Atmel/Microchip",
         0x0041: "Infineon", 0x0001: "AMD/Spansion", 0x002F: "Renesas", 0x0033: "Analog Devices",
         0x004F: "SiFive", 0x0030: "Nordic", 0x0043: "Cypress", 0x0053: "Marvell", 0x0038: "Toshiba"}


def tms_path(start: str, target: str) -> list[int]:
    """Shortest TMS sequence from one TAP state to another (breadth-first over the state graph)."""
    if target not in STATES:
        raise AD2Error(f"state must be one of: {', '.join(STATES)}")
    if start == target:
        return []
    frontier = [(start, [])]
    seen = {start}
    while frontier:
        state, path = frontier.pop(0)
        for tms in (0, 1):
            nxt = STATES[state][tms]
            if nxt == target:
                return path + [tms]
            if nxt not in seen:
                seen.add(nxt)
                frontier.append((nxt, path + [tms]))
    raise AD2Error(f"no path from {start} to {target}")


class Port:
    """Bit-bang driver keeping the DigitalIO output word cached so each edge is one write."""

    def __init__(self, dd, tck, tms, tdi, tdo):
        self.dd = dd
        self.tck, self.tms, self.tdi, self.tdo = tck, tms, tdi, tdo
        self.state = "reset"
        self.dwf = device.dwf
        h = dd.handle
        enable = ctypes.c_uint32()
        if self.dwf.FDwfDigitalIOOutputEnableGet(h, ctypes.byref(enable)) == 0:
            device.check_error()
        value = ctypes.c_uint32()
        if self.dwf.FDwfDigitalIOOutputGet(h, ctypes.byref(value)) == 0:
            device.check_error()
        mask = (1 << tck) | (1 << tms) | (1 << tdi)
        self.enable = (enable.value | mask) & ~(1 << tdo)
        self.value = (value.value & ~mask) | (1 << tms)      # TCK low, TMS high, TDI low
        if self.dwf.FDwfDigitalIOOutputEnableSet(h, ctypes.c_int(self.enable)) == 0:
            device.check_error()
        self._write(self.value)

    def _write(self, value):
        # Auto-configure is on (the session default), so the output word applies immediately and the
        # extra FDwfDigitalIOConfigure round trip per edge is not needed.
        self.value = value
        if self.dwf.FDwfDigitalIOOutputSet(self.dd.handle, ctypes.c_int(value)) == 0:
            device.check_error()

    def _read_tdo(self) -> int:
        if self.dwf.FDwfDigitalIOStatus(self.dd.handle) == 0:
            device.check_error()
        levels = ctypes.c_uint32()
        if self.dwf.FDwfDigitalIOInputStatus(self.dd.handle, ctypes.byref(levels)) == 0:
            device.check_error()
        return (levels.value >> self.tdo) & 1

    def clock(self, tms: int, tdi: int) -> int:
        """One TCK cycle: present TMS/TDI, sample TDO, rising edge, falling edge."""
        value = self.value & ~((1 << self.tck) | (1 << self.tms) | (1 << self.tdi))
        value |= (tms << self.tms) | (tdi << self.tdi)
        self._write(value)
        tdo = self._read_tdo()
        self._write(value | (1 << self.tck))
        self._write(value)
        self.state = STATES[self.state][tms]
        return tdo

    def goto(self, target: str) -> list[int]:
        path = tms_path(self.state, target)
        for tms in path:
            self.clock(tms, 0)
        return path

    def reset(self):
        for _ in range(5):
            self.clock(1, 0)
        self.state = "reset"

    def shift(self, bits, end_state="idle") -> list[int]:
        """Shift `bits` (first element first) in the current shift state, leaving via Exit1 to end_state."""
        out = []
        for i, bit in enumerate(bits):
            last = i == len(bits) - 1
            out.append(self.clock(1 if last else 0, bit))
        self.goto(end_state)
        return out

    def release(self):
        """Return the four pins to inputs."""
        self.enable &= ~((1 << self.tck) | (1 << self.tms) | (1 << self.tdi))
        if self.dwf.FDwfDigitalIOOutputEnableSet(self.dd.handle, ctypes.c_int(self.enable)) == 0:
            device.check_error()
        if self.dwf.FDwfDigitalIOConfigure(self.dd.handle) == 0:
            device.check_error()


def pulse_reset(session, p: Port, reset_pin, reset_delay_s: float, low_s: float = 0.02):
    """Pulse the target's reset line low, release it to an input, then wait reset_delay_s.
    Some targets (the ESP8266 is one) only answer on JTAG for a short window after reset."""
    if reset_pin is None:
        return
    from . import digital
    dd = session.device_data
    (reset_pin,) = digital.check_pins(dd, [reset_pin], "reset_pin")
    if reset_pin in (p.tck, p.tms, p.tdi, p.tdo):
        raise AD2Error("reset_pin must differ from the JTAG pins")
    if not 0 <= reset_delay_s <= 5:
        raise AD2Error("reset_delay_s must be between 0 and 5 seconds")
    digital.write(dd, low=[reset_pin])
    import time
    time.sleep(low_s)
    digital.write(dd, inputs=[reset_pin])
    time.sleep(reset_delay_s)
    p.state = "reset"
    p.reset()


def port(session, tck_pin, tms_pin, tdi_pin, tdo_pin) -> Port:
    """Reuse the session's JTAG port when the pins match; otherwise set up a new one and reset the TAP."""
    dd = session.ensure()
    pins = {}
    for name, pin in (("tck_pin", tck_pin), ("tms_pin", tms_pin), ("tdi_pin", tdi_pin), ("tdo_pin", tdo_pin)):
        (pins[name],) = check_pins(dd, [pin], name)
    if len(set(pins.values())) != 4:
        raise AD2Error("tck_pin, tms_pin, tdi_pin and tdo_pin must all be different pins")
    key = (pins["tck_pin"], pins["tms_pin"], pins["tdi_pin"], pins["tdo_pin"])
    existing = session.jtag
    if existing is not None and (existing.tck, existing.tms, existing.tdi, existing.tdo) == key and existing.dd is dd:
        return existing
    p = Port(dd, *key)
    p.reset()
    session.jtag = p
    return p


def _bits_to_int(bits) -> int:
    return sum(b << i for i, b in enumerate(bits))


def _int_to_bits(value: int, length: int) -> list[int]:
    return [(value >> i) & 1 for i in range(length)]


def parse_value(value, length: int) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        text = value.strip().lower().replace("_", "")
        try:
            number = int(text, 16) if text.startswith("0x") else int(text, 0) if text.startswith(("0b", "0o")) else int(text, 16)
        except ValueError:
            raise AD2Error(f"value must be an integer or a hex string, got {value!r}")
    elif isinstance(value, bool) or not isinstance(value, int):
        raise AD2Error(f"value must be an integer or a hex string, got {value!r}")
    else:
        number = value
    if number < 0 or number >= (1 << length):
        raise AD2Error(f"value does not fit in {length} bits")
    return number


def tap_state(session, state, tck_pin, tms_pin, tdi_pin, tdo_pin) -> dict:
    p = port(session, tck_pin, tms_pin, tdi_pin, tdo_pin)
    if state == "reset":
        p.reset()
        return {"state": "reset", "tms_sequence": [1, 1, 1, 1, 1]}
    before = p.state
    path = p.goto(state)
    return {"state": p.state, "from": before, "tms_sequence": path}


def scan(session, register, length, value, end_state, tck_pin, tms_pin, tdi_pin, tdo_pin) -> dict:
    if register not in ("ir", "dr"):
        raise AD2Error("register must be 'ir' or 'dr'")
    if not 1 <= length <= MAX_SCAN_BITS:
        raise AD2Error(f"length must be between 1 and {MAX_SCAN_BITS} bits")
    if end_state not in STATES or end_state.startswith(("shift", "capture", "select", "exit")):
        raise AD2Error("end_state must be idle, reset, pause_dr, pause_ir, update_dr or update_ir")
    number = parse_value(value, length)
    p = port(session, tck_pin, tms_pin, tdi_pin, tdo_pin)
    p.goto(f"shift_{register}")
    out_bits = p.shift(_int_to_bits(number, length), end_state)
    out = _bits_to_int(out_bits)
    width = (length + 3) // 4
    return {"register": register, "length": length, "shifted_in": f"0x{number:0{width}x}",
            "shifted_out": f"0x{out:0{width}x}", "shifted_out_int": out,
            "shifted_out_bits": out_bits, "tap_state": p.state}


def decode_idcode(code: int) -> dict:
    manufacturer = (code >> 1) & 0x7FF
    return {
        "idcode": f"0x{code:08x}",
        "manufacturer_id": f"0x{manufacturer:03x}",
        "manufacturer": JEDEC.get(manufacturer, "unknown"),
        "part_number": f"0x{(code >> 12) & 0xFFFF:04x}",
        "version": (code >> 28) & 0xF,
    }


def idcode(session, max_devices, tck_pin, tms_pin, tdi_pin, tdo_pin, reset_pin=None, reset_delay_s=0.03,
           instruction=None, ir_length=None) -> dict:
    """Reset the TAP (which selects IDCODE/BYPASS in every device) and read the DR chain.
    Shifting ones in means the chain ends when 0xFFFFFFFF comes out. With `instruction`, that
    IR value is loaded first (single-device chains) for TAPs whose reset default is BYPASS."""
    if not 1 <= max_devices <= 32:
        raise AD2Error("max_devices must be between 1 and 32")
    if (instruction is None) != (ir_length is None):
        raise AD2Error("instruction and ir_length must be given together")
    p = port(session, tck_pin, tms_pin, tdi_pin, tdo_pin)
    pulse_reset(session, p, reset_pin, reset_delay_s)
    p.reset()
    if instruction is not None:
        if not 1 <= ir_length <= 64:
            raise AD2Error("ir_length must be between 1 and 64")
        number = parse_value(instruction, ir_length)
        p.goto("shift_ir")
        p.shift(_int_to_bits(number, ir_length), "idle")
    p.goto("shift_dr")
    devices = []
    saw_one = False
    for index in range(max_devices):
        first = p.clock(0, 1)
        if first == 0:
            devices.append({"index": index, "has_idcode": False,
                            "note": "device without an IDCODE register (BYPASS): 1-bit register"})
            continue
        saw_one = True
        bits = [first] + [p.clock(0, 1) for _ in range(31)]
        code = _bits_to_int(bits)
        if code == 0xFFFFFFFF:
            break
        devices.append({"index": index, "has_idcode": True, **decode_idcode(code)})
    p.goto("idle")
    note = None
    if not devices:
        note = ("no device answered: TDO read all ones. Check TCK/TMS/TDI/TDO pin assignment, target power, "
                "and that TDO is driven (some targets need a pull-up or an enabled debug port)")
    elif not saw_one:
        devices = []
        note = (f"TDO stayed low for {max_devices} clocks: it is probably not connected or not driven, "
                "rather than a chain of BYPASS-only devices. Check the TDO wiring and target power")
    return {"count": len(devices), "devices": devices, "pins": {"tck": p.tck, "tms": p.tms, "tdi": p.tdi, "tdo": p.tdo},
            **({"note": note} if note else {})}


def count_devices(session, tck_pin, tms_pin, tdi_pin, tdo_pin, max_devices=32, reset_pin=None,
                  reset_delay_s=0.03) -> dict:
    """Put every device in BYPASS (IR all ones), flush zeros through the chain, then shift ones
    and count how many clocks pass before the first one appears at TDO."""
    p = port(session, tck_pin, tms_pin, tdi_pin, tdo_pin)
    pulse_reset(session, p, reset_pin, reset_delay_s)
    p.reset()
    p.goto("shift_ir")
    p.shift([1] * 128, "idle")                 # longer than any realistic total IR length
    p.goto("shift_dr")
    for _ in range(max_devices + 1):
        p.clock(0, 0)
    count = None
    for i in range(max_devices + 2):
        if p.clock(0, 1) == 1:
            count = i
            break
    p.goto("idle")
    if count is None:
        raise AD2Error("TDO never went high: no chain detected (or more devices than max_devices)")
    return {"count": count, "method": "bypass", "pins": {"tck": p.tck, "tms": p.tms, "tdi": p.tdi, "tdo": p.tdo}}


def probe_instructions(session, ir_length, tck_pin, tms_pin, tdi_pin, tdo_pin, instructions=None,
                       max_dr_length=64, reset_pin=None, reset_delay_s=0.03, window_s=0.35) -> dict:
    """For each IR value, load it and measure the selected data register: its length (clocks until a
    shifted-in one reappears) and its capture value. With reset_pin, the target is re-reset whenever
    the post-reset window would run out. Single-device chains only."""
    import time
    if not 1 <= ir_length <= 32:
        raise AD2Error("ir_length must be between 1 and 32")
    if not 1 <= max_dr_length <= 1024:
        raise AD2Error("max_dr_length must be between 1 and 1024")
    if instructions is None:
        instructions = list(range(1 << ir_length))
    if len(instructions) > 256:
        raise AD2Error("probe at most 256 instructions per call")
    values = [parse_value(i, ir_length) for i in instructions]
    p = port(session, tck_pin, tms_pin, tdi_pin, tdo_pin)
    per_instruction = (ir_length + 2 * max_dr_length + 12) * 0.001   # rough bit-bang cost in seconds
    results = []
    window_start = None
    for value in values:
        if reset_pin is not None and (window_start is None or time.monotonic() - window_start + per_instruction > window_s):
            pulse_reset(session, p, reset_pin, reset_delay_s)
            window_start = time.monotonic()
        p.goto("shift_ir")
        ir_out = _bits_to_int(p.shift(_int_to_bits(value, ir_length), "idle"))
        p.goto("shift_dr")
        capture_bits = [p.clock(0, 0) for _ in range(max_dr_length)]
        length = None
        for i in range(max_dr_length + 1):
            if p.clock(0, 1) == 1:
                length = i
                break
        p.goto("idle")
        entry = {"instruction": f"0x{value:0{(ir_length + 3) // 4}x}", "ir_capture": f"0x{ir_out:0{(ir_length + 3) // 4}x}"}
        if length is None:
            entry.update(dr_length=None, note=f"no response within {max_dr_length} bits (TDO stayed low)")
        elif length == 0:
            entry.update(dr_length=0, note="TDO high immediately: TDI appears to pass straight through, or TDO is stuck high")
        else:
            entry["dr_length"] = length
            entry["capture_value"] = f"0x{_bits_to_int(capture_bits[:length]):0{(length + 3) // 4}x}"
            if length == 1:
                entry["note"] = "1-bit register (BYPASS or unused instruction)"
            elif length == 32 and capture_bits[0] == 1:
                entry["note"] = "32-bit register whose first bit is 1: looks like an IDCODE"
                entry["as_idcode"] = decode_idcode(_bits_to_int(capture_bits[:32]))
        results.append(entry)
    lengths = sorted({r["dr_length"] for r in results if r["dr_length"]})
    return {"ir_length": ir_length, "instructions": results, "distinct_dr_lengths": lengths,
            "pins": {"tck": p.tck, "tms": p.tms, "tdi": p.tdi, "tdo": p.tdo}}


def release(session) -> dict:
    if session.jtag is None:
        return {"released": False}
    session.jtag.release()
    session.jtag = None
    return {"released": True}
