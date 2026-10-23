"""Board discovery: observe unknown pins passively, classify them, detect UART traffic, and search
for SWD / JTAG ports while driving as little as possible."""

import itertools
import math
import statistics
import time

from . import jtag as jtag_mod, logic, swd as swd_mod
from .digital import check_pins, pin_count, read as read_pins
from .errors import AD2Error

STANDARD_BAUDS = (1200, 2400, 4800, 9600, 14400, 19200, 28800, 38400, 57600, 76800, 115200, 230400,
                  250000, 460800, 500000, 921600, 1000000, 1500000, 2000000, 3000000)


# --------------------------------------------------------------------------- observation

def _runs(bits):
    """(level, length) runs, excluding the partial first and last runs."""
    runs = []
    start = 0
    for i in range(1, len(bits)):
        if bits[i] != bits[i - 1]:
            runs.append((bits[i - 1], i - start))
            start = i
    runs.append((bits[-1], len(bits) - start))
    return runs[1:-1] if len(runs) > 2 else []


def classify_pin(bits, fs) -> dict:
    n = len(bits)
    transitions = sum(1 for i in range(1, n) if bits[i] != bits[i - 1])
    high_fraction = sum(bits) / n if n else 0.0
    out = {"transitions": transitions, "high_fraction": round(high_fraction, 4),
           "initial_level": bits[0] if n else None, "final_level": bits[-1] if n else None}
    if transitions == 0:
        out["kind"] = "static_high" if high_fraction > 0.5 else "static_low"
        out["note"] = ("constantly high: pulled up, driven high, or an idle UART/I2C line" if high_fraction > 0.5
                       else "constantly low: pulled down, driven low, or ground")
        return out
    runs = _runs(bits)
    lengths = [length for _, length in runs] or [1]
    min_pulse = robust_min(lengths) / fs
    out["min_pulse_s"] = min_pulse
    out["max_pulse_s"] = max(lengths) / fs
    rising = [i for i in range(1, n) if bits[i] and not bits[i - 1]]
    if len(rising) >= 2:
        out["frequency_hz_estimate"] = (len(rising) - 1) * fs / (rising[-1] - rising[0])
    if transitions < 4:
        out["kind"] = "few_edges"
        out["note"] = ("a handful of transitions: a slow control line, reset, enable, chip-select, or a floating "
                       "input picking up noise")
        return out
    cv = statistics.pstdev(lengths) / statistics.mean(lengths) if len(lengths) > 1 else 0.0
    baud_hint = nearest_baud(min_pulse)
    # Are run lengths multiples of one bit time? Measure against the baud-derived bit period when
    # there is one (the raw shortest run is a jittery yardstick), with a tolerance of 0.3 bit.
    unit = fs / baud_hint if baud_hint else robust_min(lengths)
    quantized = sum(1 for l in lengths if abs(l / unit - round(l / unit)) < 0.3) / len(lengths)
    # a UART line rests high between frames even when the data itself is mostly zeros
    idle_high = high_fraction > 0.35 and (out["initial_level"] == 1 or out["final_level"] == 1)
    if cv < 0.08:
        out["kind"] = "clock"
        out["note"] = f"regular square wave, about {out.get('frequency_hz_estimate', 0):.4g} Hz"
    elif baud_hint and idle_high and quantized > 0.8:
        out["kind"] = "uart_candidate"
        out["baud_hint"] = baud_hint
        out["note"] = f"idle-high with pulses in multiples of a {baud_hint} baud bit: likely UART TX/RX"
    elif quantized > 0.8:
        out["kind"] = "data"
        out["note"] = f"bursty data with a {1 / min_pulse:.4g} Hz bit rate: SPI data, I2C, or a serial protocol"
    else:
        out["kind"] = "activity"
        out["note"] = "irregular activity"
    return out


def robust_min(lengths) -> int:
    """Shortest run length that is not an isolated glitch: a length is accepted when it occurs at
    least twice or at least 2 % of the time, or when nothing longer is close to it."""
    if len(lengths) < 4:
        return min(lengths)
    counts = {}
    for l in lengths:
        counts[l] = counts.get(l, 0) + 1
    threshold = max(2, int(0.02 * len(lengths)))
    for l in sorted(counts):
        if counts[l] >= threshold:
            return l
    return min(lengths)


def nearest_baud(min_pulse_s, tolerance=0.12):
    if min_pulse_s <= 0:
        return None
    candidate = 1 / min_pulse_s
    best = min(STANDARD_BAUDS, key=lambda b: abs(math.log(b / candidate)))
    return best if abs(best - candidate) / best <= tolerance else None


def observe(session, pins=None, duration_s=1.0, sampling_frequency=1e6) -> dict:
    """Static levels plus a record of every pin; nothing is driven."""
    dd = session.ensure()
    pins = check_pins(dd, pins, "pins") or list(range(pin_count(dd)))
    levels = read_pins(dd)
    fs, samples, lost, _ = logic.record(session, sampling_frequency, duration_s, trigger_timeout_s=0)
    report = {}
    for p in pins:
        bits = [(v >> p) & 1 for v in samples]
        report[f"dio{p}"] = classify_pin(bits, fs)
    kinds = {}
    for name, info in report.items():
        kinds.setdefault(info["kind"], []).append(int(name[3:]))
    quiet = sorted(kinds.get("static_high", []) + kinds.get("static_low", []))
    return {
        "duration_s": len(samples) / fs,
        "sampling_frequency_hz": fs,
        "samples_lost": lost,
        "levels_now": levels["levels"],
        "pins": report,
        "by_kind": kinds,
        "quiet_pins": quiet,
        "advice": ("Only pins listed as quiet are safe candidates for driving (find_swd, find_jtag). "
                   "Pins with activity are outputs of the board; observe or decode them instead. "
                   "Measure unknown header voltages with measure_voltage before connecting them to DIO."),
    }


# --------------------------------------------------------------------------- pull-ups and drivers

def measure_pullups(session, pins, drive_time_s=50e-6) -> dict:
    """Pull each pin low briefly with the pattern generator, release it to high impedance, and time
    how long the line takes to rise. A fast rise means a real pull-up; a slow one means only leakage;
    no rise means a pull-down or ground; no fall means something drives the pin high."""
    import ctypes
    from WF_SDK import device
    import dwfconstants as constants
    from . import pattern as pattern_mod
    dd = session.ensure()
    pins = check_pins(dd, pins, "pins")
    if not pins:
        raise AD2Error("give at least one pin")
    if not 5e-6 <= drive_time_s <= 1e-3:
        raise AD2Error("drive_time_s must be between 5e-6 and 1e-3 seconds")
    results = {}
    idle = read_pins(dd)["levels"]
    for pin in pins:
        if not idle[f"dio{pin}"]:
            results[f"dio{pin}"] = {"kind": "idle_low", "rise_time_s": None,
                                    "note": "already low: pulled down, grounded or driven low; not pulsed "
                                            "(driving it high is not attempted)"}
            continue
        measured = None
        for fs in (10e6, 1e6):
            # arm the logic analyzer on the pin's falling edge, then fire a single low pulse
            _, actual_fs, n = logic.configure(session, fs, 0)
            masks = (0, 0, 0, 1 << pin)
            logic.set_trigger(dd, n, "logic", masks, pretrigger_samples=8, timeout_s=0.0)
            dwf = device.dwf
            if dwf.FDwfDigitalInConfigure(dd.handle, ctypes.c_bool(True), ctypes.c_bool(True)) == 0:
                device.check_error()
            low_bits = max(1, int(round(drive_time_s * 1e6)))
            pattern_mod.generate(session, pin, "custom", 1e6, data=[0] * low_bits, idle="high_impedance",
                                 run_time_s=low_bits / 1e6, repeat=1)
            try:
                logic._wait_done(dd, 0.5, f"pull-up test on dio{pin}")
            except AD2Error:
                pattern_mod.stop(session, pin)
                results[f"dio{pin}"] = {"kind": "driven_high_or_no_fall",
                                        "note": "the pin did not go low when pulled: something drives it high "
                                                "(a board output); do not drive this pin"}
                measured = True
                break
            pattern_mod.stop(session, pin)
            buffer = (ctypes.c_uint16 * n)()
            if dwf.FDwfDigitalInStatusData(dd.handle, buffer, ctypes.c_int(2 * n)) == 0:
                device.check_error()
            bits = [(v >> pin) & 1 for v in buffer]
            try:
                fall = next(i for i in range(1, n) if bits[i - 1] and not bits[i])
            except StopIteration:
                fall = 8
            release = fall + int(round(drive_time_s * actual_fs))
            rise = next((i for i in range(release, n) if bits[i]), None)
            if rise is None and fs == 10e6:
                continue                                    # too slow for this window; retry at 1 MS/s
            if rise is None:
                results[f"dio{pin}"] = {"kind": "no_pullup", "rise_time_s": None,
                                        "note": f"stayed low for more than {(n - release) / actual_fs * 1e3:.1f} ms after release: "
                                                "pulled down, grounded, or no pull-up at all"}
            else:
                t = max(0.0, (rise - release) / actual_fs)
                if t < 2e-6:
                    kind, note = "strong_pullup", "rises within 2 us: a real pull-up (roughly 10 k or less)"
                elif t < 50e-6:
                    kind, note = "weak_pullup", "rises in tens of microseconds: a weak pull-up (100 k class)"
                else:
                    kind, note = "leakage_only", ("rises very slowly: no pull-up, only leakage through an input; "
                                                  "I2C on this line needs an external pull-up")
                results[f"dio{pin}"] = {"kind": kind, "rise_time_s": t, "note": note}
            measured = True
            break
        if not measured:
            results[f"dio{pin}"] = {"kind": "unknown", "note": "no measurement"}
    session.logic_settings = None
    return {"drive_time_s": drive_time_s, "pins": results,
            "note": "each pin was pulled low once for drive_time_s through the DIO driver; skip pins that "
                    "observe_pins shows as active outputs"}


# --------------------------------------------------------------------------- UART

def decode_uart(bits, fs, baud, data_bits=8):
    """Software 8N1-style decoder on a sampled line. Returns (bytes, framing_errors)."""
    bit_t = fs / baud
    n = len(bits)
    out = bytearray()
    errors = 0
    i = 0
    while i < n - 1:
        if bits[i] == 1 and bits[i + 1] == 0:
            start = i + 1
            stop_idx = int(round(start + bit_t * (data_bits + 1.5)))
            if stop_idx >= n:
                break
            value = 0
            for k in range(data_bits):
                if bits[int(round(start + bit_t * (k + 1.5)))]:
                    value |= 1 << k
            if bits[stop_idx]:
                out.append(value)
            else:
                errors += 1
            i = int(round(start + bit_t * (data_bits + 1)))
        else:
            i += 1
    return bytes(out), errors


def detect_uart(session, pins=None, duration_s=1.0, sampling_frequency=1e6, baud_rate=None, data_bits=8) -> dict:
    """Find UART traffic on pins: baud from the shortest pulse, then decode frames and judge the text."""
    dd = session.ensure()
    pins = check_pins(dd, pins, "pins") or list(range(pin_count(dd)))
    fs, samples, lost, _ = logic.record(session, sampling_frequency, duration_s, trigger_timeout_s=0)
    results = []
    for p in pins:
        bits = [(v >> p) & 1 for v in samples]
        info = classify_pin(bits, fs)
        if info["transitions"] < 4 or (info["kind"] == "clock" and not baud_rate):
            continue      # a clock decodes as an endless run of 0x55, which looks like text
        bauds = [baud_rate] if baud_rate else ([info["baud_hint"]] if info.get("baud_hint") else [])
        if not bauds:
            bauds = [b for b in STANDARD_BAUDS if 2 * b <= fs and abs(1 / b - info["min_pulse_s"]) / (1 / b) < 0.5]
        best = None
        for baud in bauds:
            data, errors = decode_uart(bits, fs, baud, data_bits)
            if not data:
                continue
            printable = sum(1 for b in data if 32 <= b < 127 or b in (9, 10, 13)) / len(data)
            score = printable * len(data) / (1 + errors)
            if best is None or score > best["score"]:
                best = {"baud": baud, "bytes": len(data), "framing_errors": errors, "printable_fraction": round(printable, 3),
                        "sample_text": data[:200].decode("utf-8", errors="replace"), "sample_hex": data[:32].hex(" "),
                        "score": score}
        if best:
            distinct = len(set(best["sample_hex"].split()))
            confidence = "high" if best["printable_fraction"] > 0.9 and best["framing_errors"] <= best["bytes"] / 20 \
                and distinct >= 3 else "medium" if best["printable_fraction"] > 0.6 and distinct >= 2 else "low"
            best.pop("score")
            results.append({"pin": p, "kind": info["kind"], "confidence": confidence, **best,
                            "next_step": f"uart_receive(rx_pin={p}, baud_rate={best['baud']}) to read live"})
    results.sort(key=lambda r: ({"high": 0, "medium": 1, "low": 2}[r["confidence"]], -r["bytes"]))
    return {"duration_s": len(samples) / fs, "samples_lost": lost, "candidates": results,
            "note": None if results else "no UART-like frames decoded; try a longer duration, a higher sampling "
                                         "frequency for fast baud rates, or provoke output by resetting the board"}


# --------------------------------------------------------------------------- SWD / JTAG search

def find_swd(session, candidate_pins, rate=1e6, dormant_wakeup=False, exclude_pins=None) -> dict:
    """Try every ordered (SWCLK, SWDIO) pair; a plausible DPIDR with ACK OK is a hit."""
    dd = session.ensure()
    candidates = check_pins(dd, candidate_pins, "candidate_pins")
    exclude = set(check_pins(dd, exclude_pins, "exclude_pins"))
    candidates = [p for p in dict.fromkeys(candidates) if p not in exclude]
    unusable = [p for p in candidates if p > swd_mod.MAX_ENGINE_PIN]
    candidates = [p for p in candidates if p <= swd_mod.MAX_ENGINE_PIN]
    if len(candidates) < 2:
        raise AD2Error(f"need at least two candidate pins on DIO 0-{swd_mod.MAX_ENGINE_PIN} (the SWD engine's range)"
                       + (f"; {unusable} cannot be used" if unusable else ""))
    tried = 0
    found = []
    started = time.monotonic()
    for clk, io in itertools.permutations(candidates, 2):
        tried += 1
        result = swd_mod.idcode(session, clk, io, rate, dormant_wakeup)
        if result["responded"]:
            found.append({"swclk": clk, "swdio": io, **{k: v for k, v in result.items() if k not in ("pins", "responded")}})
    swd_mod.reset_engine(session)
    return {"candidates": candidates, "pairs_tried": tried, "elapsed_s": round(time.monotonic() - started, 2),
            "found": found,
            **({"skipped_pins": unusable, "skipped_note": f"the SWD engine only works on DIO 0-{swd_mod.MAX_ENGINE_PIN}; "
                                                          "rewire those signals to lower pins to test them"} if unusable else {}),
            "next_step": (f"swd_identify(swclk_pin={found[0]['swclk']}, swdio_pin={found[0]['swdio']})" if found else
                          "no SWD port found on these pins; check target power and try dormant_wakeup=true")}


def find_i2c(session, candidate_pins, rate=100e3, exclude_pins=None) -> dict:
    """Try every ordered (SCL, SDA) pair: a pair whose bus is free and answers an address scan is a hit.
    Both lines must idle high (pull-ups), so only static_high pins are worth trying."""
    from . import i2c as i2c_mod
    dd = session.ensure()
    candidates = check_pins(dd, candidate_pins, "candidate_pins")
    exclude = set(check_pins(dd, exclude_pins, "exclude_pins"))
    candidates = [p for p in dict.fromkeys(candidates) if p not in exclude]
    if len(candidates) < 2:
        raise AD2Error("need at least two candidate pins")
    found, tried, notes = [], 0, []
    for scl, sda in itertools.permutations(candidates, 2):
        tried += 1
        try:
            scan = i2c_mod.scan(session, scl, sda, rate)
        except AD2Error as e:
            notes.append(f"scl={scl} sda={sda}: {str(e).split('.')[0]}")
            continue
        if scan["found"]:
            found.append({"scl": scl, "sda": sda, "addresses": scan["found"], "method": scan.get("method")})
    session.i2c_settings = None
    return {"candidates": candidates, "pairs_tried": tried, "found": found, "bus_errors": notes[:8],
            "next_step": (f"i2c_write_read(address={int(found[0]['addresses'][0], 16)}, data='<register>', count=1, "
                          f"scl_pin={found[0]['scl']}, sda_pin={found[0]['sda']})" if found else
                          "no I2C device acknowledged; both lines need pull-ups and the slave must be powered")}


def _tap_responds(p: jtag_mod.Port) -> int | None:
    """After a TAP reset, shift a pattern through the DR in BYPASS/IDCODE and look for it delayed.
    Returns the delay in bits (1 = BYPASS-only device, 32 = IDCODE device) or None."""
    p.reset()
    p.goto("shift_dr")
    pattern = [1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 1, 0, 0, 0, 1, 0]
    out = p.shift(pattern + [0] * 48, "idle")
    for delay in range(1, 48):
        if out[delay:delay + len(pattern)] == pattern:
            return delay
    return None


def find_jtag(session, candidate_pins, exclude_pins=None, reset_pin=None, reset_delay_s=0.03, window_s=0.35,
              max_time_s=120) -> dict:
    """Search for a JTAG port on the candidate pins.

    Pass 1 (IDCODE): permutations of (TCK, TMS, TDO) with TDI unused; a valid IDCODE read is a hit.
    Pass 2 (BYPASS): permutations of (TCK, TMS, TDI, TDO); a shifted pattern coming back delayed is a hit.
    With reset_pin, the target is re-reset whenever its post-reset window would run out."""
    dd = session.ensure()
    candidates = check_pins(dd, candidate_pins, "candidate_pins")
    exclude = set(check_pins(dd, exclude_pins, "exclude_pins"))
    if reset_pin is not None:
        exclude.add(reset_pin)
    candidates = [p for p in dict.fromkeys(candidates) if p not in exclude]
    if len(candidates) < 3:
        raise AD2Error("need at least three candidate pins (TCK, TMS, TDO)")
    started = time.monotonic()
    tried = 0
    window_start = None
    found = []

    def fresh_window(p):
        nonlocal window_start
        if reset_pin is not None and (window_start is None or time.monotonic() - window_start > window_s - 0.06):
            jtag_mod.pulse_reset(session, p, reset_pin, reset_delay_s)
            window_start = time.monotonic()

    def budget_ok():
        if time.monotonic() - started > max_time_s:
            raise AD2Error(f"search exceeded max_time_s={max_time_s}; narrow candidate_pins or raise the limit")
        return True

    def plausible_idcode(code):
        ones = bin(code).count("1")
        return code & 1 and 6 <= ones <= 26 and (code >> 1) & 0x7FF not in (0, 0x7FF)

    def read_idcode(p):
        p.reset()
        p.goto("shift_dr")
        return jtag_mod._bits_to_int([p.clock(0, 1) for _ in range(32)])

    # pass 1: IDCODE over (TCK, TMS, TDO); TDI is held on a spare pin and never matters for the read
    unverified = []
    for tck, tms, tdo in itertools.permutations(candidates, 3):
        budget_ok()
        spare = next(p for p in candidates + list(range(pin_count(dd))) if p not in (tck, tms, tdo) and p not in exclude)
        tried += 1
        p = jtag_mod.Port(dd, tck, tms, spare, tdo)
        try:
            fresh_window(p)
            code = read_idcode(p)
            if not plausible_idcode(code) or read_idcode(p) != code:
                continue
        finally:
            p.release()
        hit = {"method": "idcode", "tck": tck, "tms": tms, "tdo": tdo, "tdi": None, **jtag_mod.decode_idcode(code)}
        # verify by locating TDI: a pattern shifted on the right pin returns through the 32-bit register
        for tdi in candidates:
            if tdi in (tck, tms, tdo):
                continue
            p = jtag_mod.Port(dd, tck, tms, tdi, tdo)
            try:
                fresh_window(p)
                if _tap_responds(p) is not None:
                    hit["tdi"] = tdi
                    break
            finally:
                p.release()
        if hit["tdi"] is not None:
            found.append(hit)
            break
        unverified.append({k: hit[k] for k in ("tck", "tms", "tdo", "idcode")})
    if not found:
        # pass 2: BYPASS detection over 4-pin permutations
        for tck, tms, tdi, tdo in itertools.permutations(candidates, 4):
            budget_ok()
            tried += 1
            p = jtag_mod.Port(dd, tck, tms, tdi, tdo)
            try:
                fresh_window(p)
                delay = _tap_responds(p)
                if delay is not None:
                    found.append({"method": "bypass", "tck": tck, "tms": tms, "tdi": tdi, "tdo": tdo,
                                  "dr_delay_bits": delay,
                                  "note": ("responds as a 1-bit BYPASS register after reset: a TAP without IDCODE"
                                           if delay == 1 else f"{delay}-bit reset-default register")})
                    break
            finally:
                p.release()
    session.jtag = None
    return {"candidates": candidates, "combinations_tried": tried, "elapsed_s": round(time.monotonic() - started, 2),
            "found": found, "rejected_idcode_reads": unverified[:8],
            "next_step": (f"jtag_idcode / jtag_probe_instructions with tck_pin={found[0]['tck']}, tms_pin={found[0]['tms']}, "
                          f"tdi_pin={found[0]['tdi']}, tdo_pin={found[0]['tdo']}" if found else
                          "no TAP found; check power, TRST (hold high), and that the pins are quiet; "
                          "use reset_pin for targets that only answer after reset")}
