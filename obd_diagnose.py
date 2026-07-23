#!/usr/bin/env python3
"""Adaptive OBD2 diagnostic assistant.

Flow:
  1. Ask the owner for symptoms (free text) before anything else.
  2. Read a baseline snapshot + trouble codes from the ELM327.
  3. AI triage: decide whether a short guided live-data capture would help,
     and if so, which signals to watch and what the owner should do
     (idle, rev to ~2500 rpm, snap-throttle, oil-cap-off test, ...).
  4. Run the guided capture with a compact live display (min/max/avg + sparkline).
  5. AI final diagnosis: most likely problem, cost, cheapest fix, and clickable
     RockAuto / NAPA parts links + a YouTube how-to link.

Runs against real hardware by default, or with --simulate for a no-hardware demo.
"""

import argparse
import datetime
import json
import os
import random
import subprocess
import sys
import time
from urllib.parse import quote

import anthropic
from dotenv import load_dotenv

try:
    import serial  # pyserial — only needed for real hardware
except ImportError:
    serial = None

from obd_display import LiveMonitor
import obd_connect
import obd_transport
import obd_parts
import obd_modes
import obd_history
import obd_uds

DEFAULT_VEHICLE = "2010 Audi A4 2.0T (CAEB engine)"


# --------------------------------------------------------------------------- #
# Terminal hyperlinks
# --------------------------------------------------------------------------- #
def osc8(text, url):
    """Wrap text in an OSC 8 terminal hyperlink (clickable in WezTerm/modern terminals)."""
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


# --------------------------------------------------------------------------- #
# Signal registry — standard OBD2 Mode-01 PIDs. `f` maps raw data bytes -> value.
# --------------------------------------------------------------------------- #
SIGNALS = {
    "coolant":       {"pid": "05", "label": "Coolant Temp",        "unit": "°C",   "fmt": "{:.0f}",  "f": lambda b: b[0] - 40},
    "iat":           {"pid": "0F", "label": "Intake Air Temp",     "unit": "°C",   "fmt": "{:.0f}",  "f": lambda b: b[0] - 40},
    "load":          {"pid": "04", "label": "Engine Load",         "unit": "%",    "fmt": "{:.1f}",  "f": lambda b: b[0] * 100 / 255},
    "stft_b1":       {"pid": "06", "label": "Short Fuel Trim B1",  "unit": "%",    "fmt": "{:+.1f}", "f": lambda b: (b[0] - 128) * 100 / 128},
    "ltft_b1":       {"pid": "07", "label": "Long Fuel Trim B1",   "unit": "%",    "fmt": "{:+.1f}", "f": lambda b: (b[0] - 128) * 100 / 128},
    "rpm":           {"pid": "0C", "label": "Engine RPM",          "unit": "RPM",  "fmt": "{:.0f}",  "f": lambda b: (b[0] * 256 + b[1]) / 4},
    "speed":         {"pid": "0D", "label": "Vehicle Speed",       "unit": "km/h", "fmt": "{:.0f}",  "f": lambda b: b[0]},
    "maf":           {"pid": "10", "label": "MAF Air Flow",        "unit": "g/s",  "fmt": "{:.2f}",  "f": lambda b: (b[0] * 256 + b[1]) / 100},
    "throttle":      {"pid": "11", "label": "Throttle Position",   "unit": "%",    "fmt": "{:.1f}",  "f": lambda b: b[0] * 100 / 255},
    "map":           {"pid": "0B", "label": "Manifold Pressure",   "unit": "kPa",  "fmt": "{:.0f}",  "f": lambda b: b[0]},
    "timing":        {"pid": "0E", "label": "Timing Advance",      "unit": "°",    "fmt": "{:+.1f}", "f": lambda b: b[0] / 2 - 64},
    "o2_b1s2_v":     {"pid": "15", "label": "O2 B1S2 (post-cat)",  "unit": "V",    "fmt": "{:.3f}",  "f": lambda b: b[0] / 200},
    "lambda_b1s1":   {"pid": "34", "label": "Lambda B1S1 (pre)",   "unit": "λ",    "fmt": "{:.3f}",  "f": lambda b: (b[0] * 256 + b[1]) / 32768},
    "cmd_lambda":    {"pid": "44", "label": "Commanded Lambda",    "unit": "λ",    "fmt": "{:.3f}",  "f": lambda b: (b[0] * 256 + b[1]) / 32768},
    "cat_temp_b1s1": {"pid": "3C", "label": "Catalyst Temp B1S1",  "unit": "°C",   "fmt": "{:.0f}",  "f": lambda b: (b[0] * 256 + b[1]) / 10 - 40},
    "baro":          {"pid": "33", "label": "Barometric Press",    "unit": "kPa",  "fmt": "{:.0f}",  "f": lambda b: b[0]},
    "ctrl_voltage":  {"pid": "42", "label": "Module Voltage",      "unit": "V",    "fmt": "{:.2f}",  "f": lambda b: (b[0] * 256 + b[1]) / 1000},
}

# Signals shown in the baseline snapshot.
BASELINE_KEYS = [
    "coolant", "iat", "load", "stft_b1", "ltft_b1", "rpm", "maf", "throttle",
    "map", "timing", "o2_b1s2_v", "lambda_b1s1", "cmd_lambda", "cat_temp_b1s1",
    "baro", "ctrl_voltage",
]

# Signals the AI may request for a live capture (fast-changing, decision-relevant).
MONITORABLE = [
    "rpm", "load", "stft_b1", "ltft_b1", "maf", "throttle", "map", "timing",
    "o2_b1s2_v", "lambda_b1s1", "cmd_lambda", "cat_temp_b1s1",
]


# --------------------------------------------------------------------------- #
# Readers: real ELM327 over serial, or an offline simulator
# --------------------------------------------------------------------------- #
def _clean(resp):
    return resp.replace(" ", "").replace("\r", "").replace(">", "").strip().upper()


class Elm327Reader:
    """Talks to a real ELM327 adapter over a byte transport (serial or TCP).

    The transport (obd_transport) decides *how* bytes move — a USB / Bluetooth serial
    port, or a TCP socket to a WiFi adapter or on-device bridge — so `port` may be a
    device path or a `tcp:HOST:PORT` endpoint. Survives a dropped link: it re-opens the
    same transport once before giving up, so a long session does not die on one glitch.
    """

    def __init__(self, port, baud):
        self.port = port
        self.baud = baud
        try:
            self.transport = obd_transport.make_transport(port, baud)
        except obd_transport.TransportError as e:
            raise obd_connect.ObdConnectionError(
                f"could not open {port}"
                + (f" at {baud} baud" if baud and not obd_transport.parse_tcp(port) else "")
                + f": {e}") from e

    def _reopen(self):
        """Re-open the transport after a link drop. Returns True if the adapter is back."""
        try:
            self.transport.close()
        except Exception:
            pass
        for delay in (0.5, 1.5):
            time.sleep(delay)
            try:
                self.transport = obd_transport.make_transport(self.port, self.baud)
            except obd_transport.TransportError:
                continue
            for c in ("ATZ", "ATE0", "ATL0", "ATS0", "ATSP0"):
                self._cmd_once(c, timeout=2.0)
            return True
        return False

    def _cmd_once(self, command, timeout=2.0):
        self.transport.reset_input()
        self.transport.write((command + "\r").encode())
        buffer = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = self.transport.read_waiting()
            if chunk:
                buffer += chunk.decode(errors="ignore")
                if ">" in buffer:  # ELM327 finished responding
                    break
            else:
                time.sleep(0.05)
        return buffer.strip()

    def cmd(self, command, timeout=2.0):
        try:
            return self._cmd_once(command, timeout)
        except Exception as e:
            print(f"\n  [adapter link lost on {self.port}: {e} — reconnecting...]", flush=True)
            if not self._reopen():
                raise obd_connect.ObdConnectionError(
                    f"lost the connection to the OBD adapter on {self.port} and could not "
                    f"re-open it.\n  Check the cable / Bluetooth link and the ignition, then re-run."
                ) from e
            print("  [reconnected]", flush=True)
            return self._cmd_once(command, timeout)

    def init(self):
        for c in ("ATZ", "ATE0", "ATL0", "ATS0", "ATSP0"):
            self.cmd(c)

    def query(self, pid):
        """Mode-01 query. Returns data bytes as a list of ints, or None."""
        resp = _clean(self.cmd("01" + pid))
        expected = "41" + pid.upper()
        if not resp.startswith(expected):
            return None
        hexs = resp[len(expected):]
        if len(hexs) < 2 or len(hexs) % 2:
            return None
        return [int(hexs[i:i + 2], 16) for i in range(0, len(hexs), 2)]

    def dtcs(self):
        resp = _clean(self.cmd("03"))
        if not resp.startswith("43"):
            return []
        hexs = resp[2:]
        codes = []
        for i in range(0, len(hexs) - 3, 4):
            code_hex = hexs[i:i + 4]
            if code_hex == "0000":
                continue
            letter = {"0": "P", "1": "P", "2": "P", "3": "P",
                      "4": "C", "5": "C", "6": "C", "7": "C",
                      "8": "B", "9": "B", "A": "B", "B": "B",
                      "C": "U", "D": "U", "E": "U", "F": "U"}.get(code_hex[0], "P")
            codes.append(f"{letter}{code_hex}")
        return codes

    def fuel_status_text(self):
        data = self.query("03")  # Mode 01 PID 03 (fuel system status)
        if not data:
            return "unknown"
        return {1: "open loop (warming up)", 2: "closed loop",
                4: "open loop (load/decel)", 8: "open loop (fault)"}.get(data[0], f"raw 0x{data[0]:02X}")

    def raw(self, command):
        """Send any OBD command and return the response as a flat list of bytes, or None.
        Used by obd_modes for Modes 02/06/07/09/0A and readiness."""
        return obd_modes.parse_obd_bytes(self.cmd(command))

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


# Cars the simulator can pretend to be. Each carries a real, check-digit-valid
# VIN so the VIN-driven vehicle identification at startup has something genuine
# to decode. `audi` replays this project's captured Audi data; `honda` is a
# second identity so you can watch the identification pick a different car.
SIM_CARS = {
    "audi":  {"vin": "WAUHFAFL9AN064693", "vehicle": "2010 Audi A4 2.0T (CAEB engine)"},
    "honda": {"vin": "1HGCP2F49AA000137", "vehicle": "2010 Honda Accord 2.4"},
}
# VIN -> full description, used as a known-good shortcut when identifying a car
# (before falling back to a plain VIN decode). Seeded from the sim presets.
KNOWN_VEHICLE_VINS = {c["vin"]: c["vehicle"] for c in SIM_CARS.values()}


def _make_sim(args):
    car = SIM_CARS.get(getattr(args, "sim_car", "audi") or "audi", SIM_CARS["audi"])
    return SimReader(vin=car["vin"], vehicle=car["vehicle"])


def open_reader(args):
    """Get a reader for the run: real adapter, or the simulator.

    Auto-detects the adapter (USB or Bluetooth) unless --port pins one. If no
    adapter can be reached it explains why and offers simulate mode — but only
    with an explicit yes, because simulated numbers must never be mistaken for
    readings off the car. Returns (reader, simulated) or (None, False) to abort.
    """
    if args.simulate:
        return _make_sim(args), True
    try:
        return obd_connect.connect(port=args.port, baud=args.baud), False
    except obd_connect.ObdConnectionError as e:
        print(f"\n{e}\n")
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return None, False
        try:
            answer = input("Continue in SIMULATE mode instead (demo data, NOTHING is read "
                           "from your car)? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return None, False
        if answer.startswith("y"):
            print("\n  [SIMULATE MODE — demo vehicle. Do not treat any of this as your car's data.]")
            return _make_sim(args), True
        return None, False


class SimReader:
    """Offline simulator. Reproduces the captured real snapshot at idle and a
    PCV/vacuum-leak signature under revs (LTFT falls as RPM rises).

    `vin`/`vehicle` set the identity the sim reports (default: the reference
    Audi). The live numbers are the same reference capture regardless — only the
    identity changes — so the VIN-driven startup can be exercised with any car.
    """

    def __init__(self, vin=None, vehicle=None):
        self.rpm = 850.0
        self.target = 850.0
        self.vin = (vin or SIM_CARS["audi"]["vin"]).upper()
        self.vehicle = vehicle or SIM_CARS["audi"]["vehicle"]

    def set_action(self, instruction):
        s = instruction.lower()
        rev = any(w in s for w in ["rev", "2500", "3000", "accel", "throttle", "load", "snap"])
        self.target = 2500.0 if rev else 850.0

    def tick(self):
        self.rpm += (self.target - self.rpm) * 0.25 + random.uniform(-20, 20)

    def value(self, key):
        r = self.rpm
        frac = max(0.0, min(1.0, (r - 850) / (2500 - 850)))  # 0 at idle .. 1 at rev
        j = random.uniform
        return {
            "rpm": r,
            "load": 8.5 + frac * 35 + j(-1, 1),
            "stft_b1": j(-2.5, 2.5),
            "ltft_b1": 12.5 - frac * 9.0 + j(-0.6, 0.6),      # vacuum-leak signature
            "maf": 2.4 + frac * 18 + j(-0.3, 0.3),
            "throttle": 11.8 + frac * 30 + j(-0.5, 0.5),
            "map": 34 + frac * 55 + j(-1, 1),
            "timing": -2.5 + frac * 20 + j(-1, 1),
            "o2_b1s2_v": max(0.05, min(0.9, 0.45 + 0.35 * j(-1, 1))),
            "lambda_b1s1": 1.03 - frac * 0.03 + j(-0.005, 0.005),  # lean at idle
            "cmd_lambda": 1.0,
            "cat_temp_b1s1": 420 + frac * 180 + j(-5, 5),
            "coolant": 97.0, "iat": 42.0, "baro": 99.0,
            "ctrl_voltage": 13.71, "speed": 0.0,
        }.get(key)

    def dtcs(self):
        return ["P0420"]

    def fuel_status_text(self):
        return "closed loop"

    def raw(self, command):
        cmd = command.upper().replace(" ", "")
        if cmd == "0902":  # VIN — built from this sim's identity, not canned
            return [0x49, 0x02, 0x01] + [ord(c) for c in self.vin]
        return _SIM_MODE_DATA.get(cmd)

    def close(self):
        pass


# Canned multi-mode responses for the simulator (inline-4, single bank).
# The VIN (Mode 09) is answered per-instance from SimReader.vin, not here.
_SIM_MODE_DATA = {
    # Mode 06 supported-MID bitmasks (SAE J1979 MIDs): 0x01,0x02 (O2 monitors),
    # 0x21 (Catalyst B1), 0x41 (O2 heater B1S1).
    "0600": [0x46, 0x00, 0xC0, 0x00, 0x00, 0x01],   # MID 01,02 + range 0x20
    "0620": [0x46, 0x20, 0x80, 0x00, 0x00, 0x01],   # MID 0x21 + range 0x40
    "0640": [0x46, 0x40, 0x80, 0x00, 0x00, 0x00],   # MID 0x41
    # Mode 06 per-MID test records: 46 MID TID UAS TV(2) MIN(2) MAX(2)
    "0601": [0x46, 0x01, 0x01, 0x10, 0x01, 0x00, 0x00, 0x00, 0x03, 0xE8],
    "0602": [0x46, 0x02, 0x01, 0x10, 0x00, 0xC8, 0x00, 0x00, 0x03, 0xE8],
    # Catalyst Monitor Bank 1 — passing but only ~5% above the lower limit (weak cat).
    "0621": [0x46, 0x21, 0x81, 0x2E, 0x01, 0x40, 0x01, 0x30, 0xFF, 0xFF],
    # O2 Sensor Heater Monitor B1S1 — healthy pass.
    "0641": [0x46, 0x41, 0x01, 0x01, 0x00, 0x64, 0x00, 0x00, 0x00, 0xFA],
    # Mode 01 PID 01 readiness: MIL off, 1 DTC; Catalyst monitor NOT complete yet.
    "0101": [0x41, 0x01, 0x01, 0x07, 0xE5, 0x01],
    "07": [0x47, 0x00, 0x00],                 # no pending codes
    "0A": [0x4A, 0x04, 0x20],                 # permanent P0420
    # Mode 02 freeze frame (idle conditions when the code set)
    "020C00": [0x42, 0x0C, 0x00, 0x0D, 0x48],  # RPM 850
    "020500": [0x42, 0x05, 0x00, 0x89],        # coolant 97
    "020400": [0x42, 0x04, 0x00, 0x16],        # load 8.6
    "020600": [0x42, 0x06, 0x00, 0x80],        # STFT 0
    "020700": [0x42, 0x07, 0x00, 0x90],        # LTFT +12.5
    "021000": [0x42, 0x10, 0x00, 0x00, 0xF1],  # MAF 2.41
    "021100": [0x42, 0x11, 0x00, 0x1E],        # throttle 11.8
    "020D00": [0x42, 0x0D, 0x00, 0x00],        # speed 0
    # VAG UDS ReadDataByIdentifier (62 <did> <data>) — manufacturer-specific
    "22008E": [0x62, 0x00, 0x8E, 0x3A, 0x98],  # boost absolute 150.00 kPa
    "220091": [0x62, 0x00, 0x91, 0x3A, 0x98],  # boost specified 150.00 kPa
    "2200A7": [0x62, 0x00, 0xA7, 0x5A],        # intake air temp 42 °C
    "2205E8": [0x62, 0x05, 0xE8, 0x8F],        # oil temperature 95 °C
    "220483": [0x62, 0x04, 0x83, 0x01, 0xF4],  # fuel rail pressure 50.0 bar
    "22054B": [0x62, 0x05, 0x4B, 0x50],        # turbo wastegate duty 31.4 %
    "2201B5": [0x62, 0x01, 0xB5, 0x80, 0x00],  # lambda control B1 = 1.000
    "2205A7": [0x62, 0x05, 0xA7, 0x00, 0x00],  # misfire counter cyl 1 = 0
}


def read_signal(reader, key):
    """Return the physical value for a signal key, or None."""
    if hasattr(reader, "value"):          # simulator returns physical values directly
        return reader.value(key)
    sig = SIGNALS[key]
    data = reader.query(sig["pid"])
    if not data:
        return None
    try:
        return sig["f"](data)
    except (IndexError, ValueError, ZeroDivisionError):
        return None


def read_baseline(reader):
    """Return (text_block, values) — values maps signal key -> float|None, plus
    'dtcs' (list) and 'fuel_status' (str)."""
    lines = []
    values = {}
    for key in BASELINE_KEYS:
        sig = SIGNALS[key]
        v = read_signal(reader, key)
        values[key] = v
        if v is None:
            lines.append(f"{sig['label']}: No Data")
        else:
            lines.append(f"{sig['label']}: {sig['fmt'].format(v)} {sig['unit']}".strip())
    values["fuel_status"] = reader.fuel_status_text()
    lines.append(f"Fuel System Status: {values['fuel_status']}")
    codes = reader.dtcs()
    values["dtcs"] = codes
    lines.append("Trouble Codes: " + (", ".join(codes) if codes else "None"))
    return "\n".join(lines), values


# --------------------------------------------------------------------------- #
# AI schemas + prompts
# --------------------------------------------------------------------------- #
DATA_REQUEST_SCHEMA = {
    "type": "object",
    "properties": {
        "needs_more_data": {"type": "boolean"},
        "reason": {"type": "string"},
        "monitor_signals": {"type": "array", "items": {"type": "string", "enum": MONITORABLE}},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "instruction": {"type": "string"},
                    "hold_s": {"type": "integer"},
                },
                "required": ["instruction", "hold_s"],
                "additionalProperties": False,
            },
        },
        "ask_user": {"type": "string"},
    },
    "required": ["needs_more_data", "reason", "monitor_signals", "steps", "ask_user"],
    "additionalProperties": False,
}

DIAGNOSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "most_likely_problem": {"type": "string"},
        "estimated_repair_cost": {"type": "string"},
        "summary": {"type": "string"},
        "cheapest_fix": {"type": "string"},
        "parts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "part_number": {"type": "string"},
                    "search_query": {"type": "string"},
                },
                "required": ["name", "part_number", "search_query"],
                "additionalProperties": False,
            },
        },
        "video_search": {"type": "string"},
    },
    "required": ["most_likely_problem", "estimated_repair_cost", "summary",
                 "cheapest_fix", "parts", "video_search"],
    "additionalProperties": False,
}


class ClaudeEngine:
    """Diagnosis backend using Anthropic Claude with structured outputs."""
    name = "Claude"

    def __init__(self, api_key, model="claude-sonnet-5"):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def structured(self, prompt, schema):
        message = self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            thinking={"type": "disabled"},  # Sonnet 5 thinking counts against max_tokens
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in message.content if b.type == "text")
        return json.loads(text)


class OpenAIEngine:
    """Diagnosis backend using OpenAI with JSON-schema structured outputs."""
    name = "OpenAI"

    def __init__(self, api_key, model=None):
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key)
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o")

    def structured(self, prompt, schema):
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_schema", "json_schema": {
                "name": "result", "schema": schema, "strict": True}},
        )
        return json.loads(resp.choices[0].message.content)


def triage(engine, vehicle, baseline, symptoms):
    signal_menu = ", ".join(f"{k} ({SIGNALS[k]['label']})" for k in MONITORABLE)
    prompt = f"""You are triaging an OBD2 diagnosis for a {vehicle}.

Baseline snapshot (engine idling):
{baseline}

Owner-reported symptoms:
{symptoms or "(none provided)"}

Decide whether a short guided live-data capture (<= 30 seconds total) would materially
improve the diagnosis. If the baseline + symptoms already determine the fix, set
needs_more_data=false and leave the arrays empty and ask_user "".

Otherwise design the capture:
- monitor_signals: choose ONLY from this allowed set (use the key on the left):
  {signal_menu}
- steps: 1-4 things the owner does while data is logged. Each has an `instruction`
  (imperative, e.g. "Hold at idle", "Rev to ~2500 rpm and hold", "Let it return to idle",
  "Snap the throttle to wide open and release") and `hold_s` seconds to record (3-10).
- ask_user: an optional single manual test whose result discriminates causes
  (e.g. removing the oil filler cap to detect a PCV/crankcase-vacuum problem). "" if none.

Pick signals and steps that actually separate the likely causes given the codes and symptoms.
For catalyst/fuel-trim cases, watching fuel trims and lambda across an idle->rev sweep is
usually decisive."""
    req = engine.structured(prompt, DATA_REQUEST_SCHEMA)
    req["monitor_signals"] = [k for k in req.get("monitor_signals", []) if k in MONITORABLE]
    return req


def final_diagnosis(engine, vehicle, baseline, symptoms, capture_text):
    prompt = f"""I have a {vehicle}. Diagnose it from all the evidence below and respond as structured data.

Baseline snapshot:
{baseline}

Owner-reported symptoms:
{symptoms or "(none provided)"}

{capture_text or "(no live capture was performed)"}

Respond with:
- most_likely_problem: the single most likely problem, weighing the codes, live data, and symptoms.
- estimated_repair_cost: typical parts + labor range in USD.
- summary: 2-4 sentences of reasoning that cite the specific readings/trends/symptoms.
- cheapest_fix: the cheapest realistic DIY fix to try first.
- parts: parts a DIY-er would buy for the likely fix. For each: name (plain), part_number
  (a common OEM/aftermarket number if you are confident, else ""), and search_query
  (a concise store search string including the vehicle).
- video_search: a YouTube search string for a how-to of the cheapest fix on this vehicle.

Only include parts genuinely relevant to the diagnosis."""
    return engine.structured(prompt, DIAGNOSIS_SCHEMA)


# --------------------------------------------------------------------------- #
# Guided live capture
# --------------------------------------------------------------------------- #
def run_capture(reader, request, history, interval):
    keys = request["monitor_signals"] or ["rpm", "ltft_b1", "stft_b1", "lambda_b1s1", "o2_b1s2_v"]
    disp = [{"key": k, "label": SIGNALS[k]["label"], "unit": SIGNALS[k]["unit"], "fmt": SIGNALS[k]["fmt"]}
            for k in keys]
    lm = LiveMonitor(disp, history=history)

    steps = request["steps"] or [{"instruction": "Hold at idle", "hold_s": 6}]
    print("\nLive capture plan:")
    for i, step in enumerate(steps, 1):
        print(f"  {i}. {step['instruction']}  ({step['hold_s']}s)")
    try:
        input("\nGet to the car, then press Enter to start the capture... ")
    except EOFError:
        pass

    step_snaps = []
    for i, step in enumerate(steps, 1):
        instr = step["instruction"]
        hold = max(2, int(step.get("hold_s", 5)))
        if hasattr(reader, "set_action"):
            reader.set_action(instr)
        sums = {k: [] for k in keys}
        end = time.time() + hold
        while time.time() < end:
            if hasattr(reader, "tick"):
                reader.tick()
            sample = {k: read_signal(reader, k) for k in keys}
            for k, v in sample.items():
                if v is not None:
                    sums[k].append(v)
            lm.update(sample)
            remaining = max(0, int(round(end - time.time())))
            lm.set_status(f"Step {i}/{len(steps)}: {instr}   [{remaining}s]")
            sys.stdout.write(lm.render())
            sys.stdout.flush()
            time.sleep(interval)
        step_snaps.append((instr, {k: (sum(v) / len(v) if v else None) for k, v in sums.items()}))

    sys.stdout.write("\n")
    print(lm.render_summary_table())
    return lm, step_snaps


def summarize_capture(lm, step_snaps, user_answers):
    out = ["Guided live capture was performed.", "", "Per-step average readings:"]
    for instr, snap in step_snaps:
        vals = ", ".join(
            f"{SIGNALS[k]['label']} {SIGNALS[k]['fmt'].format(v)}{SIGNALS[k]['unit']}"
            for k, v in snap.items() if v is not None
        )
        out.append(f"  - {instr}: {vals}")
    out.append("")
    out.append("Overall min / max / avg / trend across the capture:")
    for k, s in lm.summary().items():
        # A signal the ECU didn't report (e.g. MAP on some VAG ECUs) has no
        # samples, so its stats are None — note it rather than crashing.
        if s["n"] == 0 or s["min"] is None:
            out.append(f"  - {s['label']}: no data (not reported by this ECU)")
            continue
        out.append(f"  - {s['label']}: min {s['min']:.3g} max {s['max']:.3g} "
                   f"avg {s['avg']:.3g} ({s['trend']})")
    if user_answers:
        out.append("")
        out.append("Owner observations during manual tests:")
        for q, a in user_answers:
            out.append(f"  - Q: {q}")
            out.append(f"    A: {a}")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Output rendering
# --------------------------------------------------------------------------- #
def render_diagnosis(data, vehicle):
    print("\n" + "=" * 60)
    print("### AI DIAGNOSIS ###\n")
    print(f"Most likely problem : {data['most_likely_problem']}")
    print(f"Estimated repair cost: {data['estimated_repair_cost']}")
    print(f"\n{data['summary']}")
    print(f"\nCheapest fix: {data['cheapest_fix']}")

    lines = []
    parts = data.get("parts", [])
    if parts:
        print("\n### Parts (clickable links) ###")
        for part in parts:
            links = obd_parts.store_links(part.get("name"), part.get("part_number"))
            print(f"\n  {obd_parts.part_label(part)}")
            print(f"    RockAuto: {osc8(links['rockauto'], links['rockauto'])}")
            print(f"    NAPA:     {osc8(links['napa'], links['napa'])}")
        lines = obd_parts.plain_parts_lines(parts)

    video_q = (data.get("video_search") or "").strip()
    video = obd_parts.youtube_link(video_q)
    if video:
        print("\n### How-to video (clickable) ###")
        print(f"\n  {osc8(video_q, video)}")
    return lines, video


def save_report(vehicle, symptoms, baseline, capture_text, data, plain_parts, video, note):
    stamp = datetime.datetime.now().strftime("%y%m%d_%H%M%S")
    reports_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
    os.makedirs(reports_dir, exist_ok=True)
    path = os.path.join(reports_dir, f"report_{stamp}.txt")
    with open(path, "w") as f:
        f.write(f"OBD2 Diagnostic Report — {vehicle}\n")
        f.write(f"Generated: {datetime.datetime.now().isoformat(timespec='seconds')}\n\n")
        f.write("== Owner symptoms ==\n" + (symptoms or "(none)") + "\n\n")
        f.write("== Baseline ==\n" + baseline + "\n\n")
        if capture_text:
            f.write("== Live capture ==\n" + capture_text + "\n\n")
        f.write("== Diagnosis ==\n")
        f.write(f"Most likely problem : {data['most_likely_problem']}\n")
        f.write(f"Estimated repair cost: {data['estimated_repair_cost']}\n\n")
        f.write(data["summary"] + "\n\n")
        f.write(f"Cheapest fix: {data['cheapest_fix']}\n\n")
        if plain_parts:
            f.write("== Parts ==\n" + "\n".join(plain_parts) + "\n\n")
        if video:
            f.write("== How-to video ==\n" + video + "\n\n")
        if note:
            f.write("== Owner note ==\n" + note + "\n")
    return path


def prompt_multiline(label):
    print(f"\n{label}\n(Type as many lines as you like; press Enter on a blank line to finish.)")
    lines = []
    while True:
        try:
            line = input("> ")
        except EOFError:
            break
        if not line.strip():
            break
        lines.append(line.strip())
    return "\n".join(lines)


# 1Password reference for the OpenAI key (override with OPENAI_OP_REF). The key
# is never stored in plaintext — it's read from 1Password at runtime, only if
# OpenAI is chosen and OPENAI_API_KEY isn't already set.
OPENAI_OP_REF = "op://Employee/openai api key ohbahdee obd/password"


def get_openai_key():
    """Return the OpenAI API key from OPENAI_API_KEY, else from 1Password."""
    key = os.getenv("OPENAI_API_KEY")
    if key:
        return key
    ref = os.getenv("OPENAI_OP_REF", OPENAI_OP_REF)
    # A service-account token in the environment can shadow the signed-in desktop
    # app and lack access to the Employee vault — drop it so op uses the account.
    child_env = {k: v for k, v in os.environ.items() if k != "OP_SERVICE_ACCOUNT_TOKEN"}
    for account in ("secondarydao.1password.com", "my.1password.com"):
        try:
            out = subprocess.run(
                ["op", "read", ref, "--account", account],
                capture_output=True, text=True, timeout=30, env=child_env,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def choose_provider():
    """Ask which AI backend to use for the diagnosis."""
    print("\nAI provider for diagnosis:")
    print("  1) Claude   (default)")
    print("  2) OpenAI")
    try:
        c = input("Choose [1/2]: ").strip().lower()
    except EOFError:
        c = ""
    return "openai" if c in ("2", "openai", "o", "gpt") else "claude"


def build_engine(provider):
    """Construct the chosen diagnosis engine, or raise with a clear message."""
    if provider == "openai":
        key = get_openai_key()
        if not key:
            raise EnvironmentError(
                "OpenAI selected but no key available. Set OPENAI_API_KEY in .env, "
                "or point OPENAI_OP_REF at a valid 1Password reference "
                "(current: " + os.getenv("OPENAI_OP_REF", OPENAI_OP_REF) + ").")
        return OpenAIEngine(key)
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise EnvironmentError("ANTHROPIC_API_KEY not found — set it in .env")
    return ClaudeEngine(key)


def default_data_request():
    """Fallback capture plan when the AI triage can't be reached (offline).
    A plain idle -> rev -> idle sweep on the standard signals is broadly useful."""
    return {
        "needs_more_data": True,
        "reason": "offline fallback — default idle/rev/idle sweep",
        "monitor_signals": ["rpm", "ltft_b1", "stft_b1", "lambda_b1s1",
                            "o2_b1s2_v", "maf", "load", "throttle"],
        "steps": [
            {"instruction": "Hold at idle", "hold_s": 6},
            {"instruction": "Rev to ~2500 rpm and hold", "hold_s": 6},
            {"instruction": "Let it return to idle", "hold_s": 6},
        ],
        "ask_user": ("With the engine idling, briefly remove the oil filler cap — "
                     "does idle change or is there strong vacuum? (helps detect a "
                     "PCV / vacuum-leak problem)"),
    }


def finalize(engine, script_dir, pending):
    """Produce the AI diagnosis from gathered data, render it, and save the
    report + history record. Used by both the live run and --diagnose-file.
    May raise on API errors — the caller decides how to handle that."""
    data = final_diagnosis(engine, pending["vehicle"], pending["evidence"],
                           pending["symptoms"], pending.get("capture_text", ""))
    plain_parts, video = render_diagnosis(data, pending["vehicle"])
    try:
        note = input("\nAdd any observation to save with the report (optional): ").strip()
    except EOFError:
        note = ""
    path = save_report(pending["vehicle"], pending["symptoms"], pending["evidence"],
                       pending.get("capture_text", ""), data, plain_parts, video, note)
    print(f"\nSaved report: {path}")
    obd_history.append_run(script_dir, {
        "ts": pending.get("ts") or datetime.datetime.now().isoformat(timespec="seconds"),
        "vehicle": pending["vehicle"], "vin": pending.get("vin"),
        "symptoms": pending["symptoms"], "metrics": pending.get("metrics", {}),
        "dtcs": pending.get("dtcs", []), "permanent": pending.get("permanent", []),
        "catalyst": pending.get("catalyst"),
        "most_likely_problem": data["most_likely_problem"],
        "cheapest_fix": data["cheapest_fix"],
    })


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Adaptive OBD2 diagnostic assistant")
    ap.add_argument("--vehicle", default=DEFAULT_VEHICLE, help=f"vehicle description (default: {DEFAULT_VEHICLE!r})")
    ap.add_argument("--port", default=None,
                    help="adapter port: a serial device (/dev/ttyUSB0, /dev/rfcomm0) or a "
                         "WiFi/TCP endpoint (tcp:192.168.0.10:35000) "
                         "(default: auto-detect USB and Bluetooth)")
    ap.add_argument("--baud", type=int, default=None,
                    help="baud rate (default: auto-detect, 38400/9600/...)")
    ap.add_argument("--simulate", action="store_true", help="run without hardware using a built-in demo vehicle")
    ap.add_argument("--diagnose-file", default=None,
                    help="produce a diagnosis from a saved pending_*.json capture "
                         "(no hardware needed) — use when you captured offline")
    ap.add_argument("--provider", choices=["claude", "openai"], default=None,
                    help="AI backend for the diagnosis (default: ask at startup)")
    ap.add_argument("--dids", default=None,
                    help="path to a UDS DID map JSON for manufacturer data "
                         "(defaults to audi_caeb_dids.json if present)")
    ap.add_argument("--history", type=int, default=48, help="live-display sparkline length")
    ap.add_argument("--interval", type=float, default=None, help="seconds between live samples")
    args = ap.parse_args()

    interval = args.interval if args.interval is not None else (0.12 if args.simulate else 0.3)

    load_dotenv()
    provider = args.provider or choose_provider()
    engine = build_engine(provider)
    print(f"Using {engine.name} for diagnosis.")
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # Offline-later path: produce the diagnosis from a previously-saved capture,
    # no hardware or new capture needed.
    if args.diagnose_file:
        with open(args.diagnose_file) as f:
            pending = json.load(f)
        print(f"\nProducing diagnosis from saved capture: {args.diagnose_file}\n")
        finalize(engine, script_dir, pending)
        try:
            os.remove(args.diagnose_file)
        except OSError:
            pass
        return

    print("=" * 60)
    print(f"  OBD2 Diagnostic Assistant — {args.vehicle}")
    if args.simulate:
        print("  [SIMULATE MODE — no hardware, demo data]")
    print("=" * 60)

    symptoms = prompt_multiline("Describe any symptoms or observations before we start:")

    reader, simulated = open_reader(args)
    if reader is None:
        return 1        # no adapter and no consent to simulate — nothing to do
    args.simulate = simulated
    if args.interval is None:
        interval = 0.12 if simulated else 0.3

    print("\nReading baseline snapshot...\n")
    baseline, baseline_vals = read_baseline(reader)
    print(baseline)

    print("\nReading diagnostic monitors (Modes 01/02/06/07/09/0A)...\n")
    freeze_signals = [
        (SIGNALS[k]["pid"], SIGNALS[k]["label"], SIGNALS[k]["unit"], SIGNALS[k]["fmt"], SIGNALS[k]["f"])
        for k in ["rpm", "coolant", "load", "stft_b1", "ltft_b1", "maf", "throttle", "speed"]
    ]
    monitors, mon_data = obd_modes.collect_monitors(reader, freeze_signals)
    print(monitors)

    evidence = baseline + "\n\n" + monitors

    # Manufacturer-specific data via UDS DIDs (VAG), if a DID map is available.
    uds_path = args.dids or os.path.join(script_dir, "audi_caeb_dids.json")
    if os.path.exists(uds_path):
        print("\nReading manufacturer-specific data (UDS DIDs)...\n")
        try:
            uds_results = obd_uds.read_dids(reader, obd_uds.load_dids(uds_path))
            uds_text = obd_uds.format_uds(uds_results)
            if uds_text:
                print(uds_text)
                evidence += "\n\n" + uds_text
        except Exception as e:
            print(f"  (UDS read skipped: {e})")

    # Prior visits for this vehicle (by VIN when available).
    vin = mon_data.get("vin")
    history = obd_history.load_history(script_dir, args.vehicle, vin)
    if history:
        print("\n" + obd_history.format_history_console(history))
        evidence += "\n\n" + obd_history.format_history_for_ai(history)

    print("\nTriaging (deciding whether live data would help)...")
    try:
        request = triage(engine, args.vehicle, evidence, symptoms)
        print(f"  -> {request['reason']}")
    except Exception:
        request = default_data_request()
        print("  (couldn't reach the AI — you may be offline; using a default "
              "idle/rev/idle capture so no data is lost)")

    capture_text = ""
    user_answers = []
    if request["needs_more_data"] and request["monitor_signals"]:
        lm, step_snaps = run_capture(reader, request, args.history, interval)
        ask = (request.get("ask_user") or "").strip()
        if ask:
            print()
            try:
                ans = input(f"{ask}\n> ").strip()
            except EOFError:
                ans = ""
            if ans:
                user_answers.append((ask, ans))
        capture_text = summarize_capture(lm, step_snaps, user_answers)
    else:
        print("  (baseline was sufficient — skipping live capture)")

    reader.close()

    # Assemble everything gathered and SAVE IT FIRST, so a network failure at
    # diagnosis time can never lose the capture (the hard part, done at the car).
    catalyst = next((r for r in mon_data["mode06"] if r["mid"] in (0x21, 0x22)), None)
    pending = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "vehicle": args.vehicle, "vin": vin, "symptoms": symptoms,
        "evidence": evidence, "capture_text": capture_text,
        "metrics": {k: baseline_vals.get(k)
                    for k in ["ltft_b1", "stft_b1", "lambda_b1s1", "o2_b1s2_v", "coolant"]},
        "dtcs": baseline_vals.get("dtcs", []),
        "permanent": mon_data.get("permanent", []),
        "catalyst": None if not catalyst else {
            "value": catalyst["value"], "min": catalyst["min"], "passed": catalyst["passed"]},
    }
    stamp = datetime.datetime.now().strftime("%y%m%d_%H%M%S")
    pending_path = os.path.join(script_dir, f"pending_{stamp}.json")
    with open(pending_path, "w") as f:
        json.dump(pending, f)

    print("\nProducing final diagnosis...")
    try:
        finalize(engine, script_dir, pending)
        os.remove(pending_path)   # diagnosis produced — the raw capture is no longer needed
    except Exception as e:
        print(f"\n[Couldn't produce the diagnosis (offline or API error): {e}]")
        print("Your captured data is saved. When you're back online, run:")
        print(f"  ./run.sh --diagnose-file {pending_path} --provider {provider}")


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except obd_connect.ObdConnectionError as e:
        print(f"\nOBD adapter problem:\n{e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
