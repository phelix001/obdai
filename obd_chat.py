#!/usr/bin/env python3
"""Interactive OBD2 diagnostic chat — an "at the car" assistant.

Talk to an AI mechanic while you work on the vehicle. It can read the car's
live data on demand (current sensors, trouble codes, on-board monitors,
manufacturer data) and run guided live captures when a test would help.

Example:
    you> I'm about to replace the MAF sensor, what should I check first?
    ... assistant advises and reads current MAF + fuel trims for you ...

Runs against a real ELM327 by default, or with --simulate for a no-hardware demo.
Same provider choice as the main tool (Claude or OpenAI).
"""

import argparse
import datetime
import glob
import json
import os
import shutil
import sys
import tempfile

from dotenv import load_dotenv

try:
    import serial  # noqa: F401  (imported by obd_diagnose; presence checked there)
except ImportError:
    serial = None

import obd_modes
import obd_uds
import obd_connect
import obd_images
import obd_vin
import obd_vehicle
from obd_diagnose import (
    SIGNALS, MONITORABLE, DEFAULT_VEHICLE, SIM_CARS, KNOWN_VEHICLE_VINS,
    open_reader, read_signal, run_capture, summarize_capture,
    choose_provider, build_engine,
)

# Sensors read by default when the assistant asks for "current values".
DEFAULT_READ = ["rpm", "coolant", "load", "stft_b1", "ltft_b1", "maf",
                "throttle", "lambda_b1s1", "o2_b1s2_v", "timing"]


# --------------------------------------------------------------------------- #
# Tools the assistant can call (executed against the OBD adapter)
# --------------------------------------------------------------------------- #
_SIGNAL_KEYS = list(SIGNALS.keys())

TOOL_DEFS = [
    {"name": "read_current",
     "description": "Read current live sensor values right now (engine running). "
                    "Use to check specific readings before/after/while doing work.",
     "schema": {"type": "object", "properties": {
         "signals": {"type": "array", "items": {"type": "string", "enum": _SIGNAL_KEYS},
                     "description": "Signal keys to read; omit for a standard set."}}}},
    {"name": "read_trouble_codes",
     "description": "Read stored, pending, and permanent diagnostic trouble codes (DTCs).",
     "schema": {"type": "object", "properties": {}}},
    {"name": "read_vin",
     "description": "Read the vehicle's VIN from the ECU (Mode 09), validate its ISO-3779 "
                    "check digit, and decode manufacturer / model year / plant. Use to confirm "
                    "you're working on the right car, or before ordering parts by VIN. Flags a "
                    "corrupt read instead of trusting a mangled VIN.",
     "schema": {"type": "object", "properties": {}}},
    {"name": "read_monitors",
     "description": "Read on-board monitor test results (Mode 06, including the catalyst "
                    "monitor) and readiness status (which self-tests have completed).",
     "schema": {"type": "object", "properties": {}}},
    {"name": "read_manufacturer_data",
     "description": "Read Audi/VAG manufacturer-specific data: boost pressure (absolute & "
                    "specified), oil temperature, fuel rail pressure, wastegate duty, lambda "
                    "control, misfire counters. Data standard OBD2 can't provide.",
     "schema": {"type": "object", "properties": {}}},
    {"name": "live_capture",
     "description": "Run a short guided live capture: the user performs steps (idle, rev to "
                    "~2500 rpm, snap throttle, etc.) while sensors are logged. Returns per-step "
                    "averages and overall min/max/avg. Use to see how readings change under "
                    "conditions (e.g. fuel trims across an idle->rev sweep).",
     "schema": {"type": "object", "properties": {
         "signals": {"type": "array", "items": {"type": "string", "enum": MONITORABLE},
                     "description": "Signals to log during the capture."},
         "steps": {"type": "array", "items": {"type": "object", "properties": {
             "instruction": {"type": "string"}, "hold_s": {"type": "integer"}},
             "required": ["instruction", "hold_s"]},
             "description": "1-4 steps for the user to perform."}}}},
]

CLAUDE_TOOLS = [{"name": t["name"], "description": t["description"],
                 "input_schema": t["schema"]} for t in TOOL_DEFS]
OPENAI_TOOLS = [{"type": "function", "function": {
    "name": t["name"], "description": t["description"], "parameters": t["schema"]}}
    for t in TOOL_DEFS]


def execute_tool(reader, name, args, history, interval, expected_vin4=None):
    """Run a tool against the OBD adapter and return a text result for the model.

    A dead adapter must not end the conversation: the failure is reported back to
    the assistant as tool output so it can tell the user what to check, and is
    explicitly labelled so it never gets mistaken for a reading.
    """
    try:
        return _run_tool(reader, name, args, history, interval, expected_vin4)
    except obd_connect.ObdConnectionError as e:
        return (f"TOOL FAILED — no connection to the OBD adapter: {e}\n"
                "No data was read from the car. Tell the user to check the adapter "
                "cable / Bluetooth link and that the ignition is on. Do not guess values.")
    except Exception as e:
        return (f"TOOL FAILED — {type(e).__name__}: {e}\n"
                "No data was read from the car. Do not guess values.")


def _run_tool(reader, name, args, history, interval, expected_vin4=None):
    args = args or {}
    if name == "read_current":
        keys = [k for k in (args.get("signals") or DEFAULT_READ) if k in SIGNALS]
        out = []
        for k in keys:
            v = read_signal(reader, k)
            sig = SIGNALS[k]
            out.append(f"{sig['label']}: " + ("No Data" if v is None
                       else f"{sig['fmt'].format(v)} {sig['unit']}".strip()))
        return "\n".join(out) or "No signals read."

    if name == "read_trouble_codes":
        stored = reader.dtcs()
        pend = obd_modes.read_pending_dtcs(reader)
        perm = obd_modes.read_permanent_dtcs(reader)
        return (f"Stored (Mode 03): {', '.join(stored) or 'none'}\n"
                f"Pending (Mode 07): {', '.join(pend) or 'none'}\n"
                f"Permanent (Mode 0A): {', '.join(perm) or 'none'}")

    if name == "read_vin":
        text, _ = obd_vin.read_and_check(reader, expected_vin4)
        return text

    if name == "read_monitors":
        text, _ = obd_modes.collect_monitors(reader, [])
        return text

    if name == "read_manufacturer_data":
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audi_caeb_dids.json")
        if not os.path.exists(path):
            return "No manufacturer DID map available for this vehicle."
        try:
            res = obd_uds.read_dids(reader, obd_uds.load_dids(path))
            return obd_uds.format_uds(res) or "No manufacturer data returned by the ECU."
        except Exception as e:
            return f"Manufacturer data read failed: {e}"

    if name == "live_capture":
        sigs = [k for k in (args.get("signals") or []) if k in MONITORABLE] or \
               ["rpm", "ltft_b1", "stft_b1", "lambda_b1s1", "o2_b1s2_v"]
        steps = args.get("steps") or [{"instruction": "Hold at idle", "hold_s": 6}]
        request = {"monitor_signals": sigs, "steps": steps}
        lm, step_snaps = run_capture(reader, request, history, interval)
        return summarize_capture(lm, step_snaps, [])

    return f"Unknown tool: {name}"


def system_prompt(vehicle):
    return f"""You are an expert, hands-on automotive diagnostic assistant for a {vehicle}. \
The user is doing repair or maintenance work on this exact car and is talking to you while at \
the vehicle. You can read the car's live data on demand with your tools:
- read_current: current sensor values
- read_trouble_codes: stored / pending / permanent DTCs
- read_vin: read + validate the VIN (confirms the car, catches a corrupt read before parts are ordered)
- read_monitors: Mode 06 on-board monitor tests (incl. catalyst) + readiness
- read_manufacturer_data: Audi/VAG-specific data (boost, oil temp, fuel rail pressure, misfire counters, ...)
- live_capture: a guided capture where the user idles/revs while data logs

How to help:
- When the user says they're about to do a job (e.g. "I'm replacing the MAF"), tell them \
concretely what to check before and after, then actually CALL the relevant tool to read it \
rather than guessing.
- Prefer real data over speculation — call a tool whenever a reading would settle the question.
- Interpret readings in plain, practical terms and give the next concrete step.
- Keep it conversational and concise. This is a chat at the car, not an essay.

Photos: the user can attach pictures (a part, a connector, a leak, a dash warning, \
another scanner's screen). When one arrives, say what you can actually see and what \
you cannot — a blurry or badly-lit shot is worth asking to retake ("/snap" or "/pic" \
sends another). Never claim to identify a part number or a crack you cannot clearly \
make out; combine what the photo shows with a live reading when that would confirm it."""


# --------------------------------------------------------------------------- #
# Session persistence
# --------------------------------------------------------------------------- #
def _sessions_dir():
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions")
    os.makedirs(d, exist_ok=True)
    return d


def session_media_dir(session_id):
    """Where this session's photos are kept — referenced by path from the transcript."""
    return os.path.join(_sessions_dir(), "media", session_id)


def new_session(vehicle, vin_last4, provider):
    now = datetime.datetime.now()
    return {
        "id": now.strftime("%y%m%d_%H%M%S"),
        "created": now.isoformat(timespec="seconds"),
        "updated": now.isoformat(timespec="seconds"),
        "vehicle": vehicle,
        "vin_last4": vin_last4,
        "provider": provider,
        "messages": [],
    }


def save_session(session, messages):
    session["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    session["messages"] = messages
    path = os.path.join(_sessions_dir(), f"session_{session['id']}.json")
    with open(path, "w") as f:
        json.dump(session, f)
    return path


def load_all_sessions():
    """All saved sessions, newest (most recently updated) first."""
    out = []
    for p in glob.glob(os.path.join(_sessions_dir(), "session_*.json")):
        try:
            with open(p) as f:
                out.append(json.load(f))
        except (OSError, json.JSONDecodeError):
            continue
    out.sort(key=lambda s: s.get("updated") or s.get("created") or "", reverse=True)
    return out


def load_session_file(path):
    with open(path) as f:
        return json.load(f)


def _turn_count(messages):
    """User turns typed by the person — not the tool-result messages that also
    carry role 'user', and counting turns that came with a photo attached."""
    n = 0
    for m in messages:
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            n += 1
        elif isinstance(c, list) and not any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            n += 1
    return n


def _last_assistant_text(messages):
    for m in reversed(messages):
        if m.get("role") == "assistant":
            c = m.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return "".join(b.get("text", "") for b in c
                               if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _session_label(s):
    ts = s.get("updated") or s.get("created") or ""
    try:
        dt = datetime.datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        dt = ts[:16]
    vin = s.get("vin_last4") or "----"
    veh = (s.get("vehicle") or "?")[:32]
    n = _turn_count(s.get("messages", []))
    return f"{dt}   {veh:<32}  VIN …{vin}   {n:>2} turns · {s.get('provider', '?')}"


def pick_session(sessions):
    """Curses picker. Returns 'new', a session dict, or None (quit).
    Navigate with ↑/↓, PageUp/PageDown, Home/End; Enter opens; q/Esc quits."""
    import curses

    items = ["＋  Start a NEW session"] + [_session_label(s) for s in sessions]

    def _run(stdscr):
        curses.curs_set(0)
        idx, top = 0, 0
        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            stdscr.addnstr(0, 0, "Continue a session?  (↑/↓  PgUp/PgDn  Home/End · "
                           "Enter=open · q=quit)", w - 1, curses.A_BOLD)
            visible = max(1, h - 2)
            if idx < top:
                top = idx
            if idx >= top + visible:
                top = idx - visible + 1
            for row, i in enumerate(range(top, min(len(items), top + visible))):
                attr = curses.A_REVERSE if i == idx else curses.A_NORMAL
                stdscr.addnstr(row + 2, 0, items[i].ljust(w - 1)[:w - 1], w - 1, attr)
            stdscr.refresh()
            c = stdscr.getch()
            if c in (curses.KEY_UP, ord('k')):
                idx = max(0, idx - 1)
            elif c in (curses.KEY_DOWN, ord('j')):
                idx = min(len(items) - 1, idx + 1)
            elif c == curses.KEY_NPAGE:
                idx = min(len(items) - 1, idx + visible)
            elif c == curses.KEY_PPAGE:
                idx = max(0, idx - visible)
            elif c == curses.KEY_HOME:
                idx = 0
            elif c == curses.KEY_END:
                idx = len(items) - 1
            elif c in (10, 13, curses.KEY_ENTER):
                return idx
            elif c in (ord('q'), 27):
                return -1

    try:
        sel = curses.wrapper(_run)
    except Exception:
        return "new"  # no usable terminal — just start fresh
    if sel is None or sel == -1:
        return None
    return "new" if sel == 0 else sessions[sel - 1]


# --------------------------------------------------------------------------- #
# Provider-specific chat loops
# --------------------------------------------------------------------------- #
def _prompt_user(pending=0):
    tag = f"\033[2m[{pending} photo{'s' if pending != 1 else ''}]\033[0m " if pending else ""
    try:
        return input(f"\n{tag}\033[1myou>\033[0m ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


PHOTO_HELP = """Photo commands:
  /pic                 attach the newest photo from your watch folders
  /pic <file|glob>     attach a specific image  (~ and wildcards work)
  /pic <n>             attach item <n> from the last /photos listing
  /photos              list the 10 newest photos found, with numbers
  /snap                take a frame from the camera (webcam / device camera)
  /phone               pull the newest photo off a USB-attached Android (adb)
  /drop                discard the photos queued for the next message
  /help                this list
A bare image path typed in a normal message is attached automatically.
Attachments are sent with your next message; send an empty line to send them alone."""


def _handle_photo_command(line, state):
    """Run a /photo command. Returns True if `line` was a command (consumed).

    `state` carries {"media_dir", "pending", "listing"} across the turn.
    """
    cmd, _, rest = line.partition(" ")
    cmd, rest = cmd.lower(), rest.strip()
    if cmd not in ("/pic", "/photo", "/photos", "/pics", "/snap", "/phone",
                   "/drop", "/help", "/?"):
        return False

    if cmd in ("/help", "/?"):
        print(PHOTO_HELP)
        return True

    if cmd == "/drop":
        n = len(state["pending"])
        state["pending"].clear()
        print(f"  Discarded {n} queued photo{'s' if n != 1 else ''}.")
        return True

    if cmd in ("/photos", "/pics"):
        found = obd_images.recent_images(10)
        state["listing"] = found
        if not found:
            print("  No photos found in: " + (", ".join(obd_images.watch_dirs())
                                              or "(no watch folders exist)"))
            return True
        print("  Newest photos (attach one with /pic <n>):")
        for i, p in enumerate(found, 1):
            print(f"   {i:>2}. {p}")
        return True

    # Captures land in a scratch dir first; only the downscaled copy is kept, so
    # the session folder never accumulates full-size originals.
    scratch = None
    label = None
    try:
        if cmd in ("/pic", "/photo"):
            if not rest:
                src = obd_images.newest_image()
            elif rest.isdigit() and state["listing"]:
                idx = int(rest)
                if not 1 <= idx <= len(state["listing"]):
                    raise obd_images.ImageError(
                        f"{idx} is not in the last listing (1-{len(state['listing'])}).")
                src = state["listing"][idx - 1]
            else:
                src = obd_images.from_path(rest)
        elif cmd == "/snap":
            scratch = tempfile.mkdtemp(prefix="obd_snap_")
            print("  Capturing...")
            src = obd_images.capture_camera(os.path.join(scratch, "snap.jpg"),
                                            device=rest or None)
            label = "camera"
        else:  # /phone
            scratch = tempfile.mkdtemp(prefix="obd_phone_")
            print("  Pulling the newest photo off the phone...")
            src = obd_images.pull_from_phone(scratch)
        att = obd_images.prepare(src, state["media_dir"], label=label or src)
    except obd_images.ImageError as e:
        print(f"  Could not attach a photo: {e}")
        return True
    finally:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)
    state["pending"].append(att)
    print(f"  [attached: {obd_images.describe(att)}]  — it goes with your next message.")
    return True


def collect_turn(state):
    """Read one user turn: photo commands queue attachments, prose sends them.

    Returns (text, attachments), or (None, []) to end the chat.
    """
    while True:
        line = _prompt_user(len(state["pending"]))
        if line is None or line.lower() in ("quit", "exit", "bye", "q"):
            return None, []
        if line.startswith("/") and _handle_photo_command(line, state):
            continue
        if not line and not state["pending"]:
            continue
        for path in obd_images.find_inline_images(line):
            try:
                att = obd_images.prepare(path, state["media_dir"], label=path)
            except obd_images.ImageError as e:
                print(f"  Could not attach {path}: {e}")
                continue
            state["pending"].append(att)
            print(f"  [attached: {obd_images.describe(att)}]")
        attachments = list(state["pending"])
        state["pending"].clear()
        return line, attachments


def run_turn(engine, reader, system, history, interval, messages,
             expected_vin4=None, on_tool=None):
    """Run one full assistant turn against `messages`, in place.

    Handles the model call plus any tool rounds until the assistant stops, for
    either provider. Appends the assistant (and tool-result) messages and returns
    the assistant's text. `on_tool(name)` is called as each tool runs so a UI or
    console can show "[reading: ...]". The user message must already be appended.

    This is the shared core used by both the terminal chat loop and the web UI —
    neither prints from here, so the caller decides how to surface the reply.
    """
    if engine.name == "Claude":
        return _claude_turn(engine, reader, system, history, interval,
                            messages, expected_vin4, on_tool)
    return _openai_turn(engine, reader, system, history, interval,
                        messages, expected_vin4, on_tool)


def _claude_turn(engine, reader, system, history, interval, messages, expected_vin4, on_tool):
    text_parts = []
    while True:
        resp = engine.client.messages.create(
            model=engine.model, max_tokens=1500, thinking={"type": "disabled"},
            system=system, tools=CLAUDE_TOOLS,
            messages=obd_images.inflate_for_claude(messages))
        # Store as plain dicts so the session is JSON-serializable.
        messages.append({"role": "assistant", "content": [b.model_dump() for b in resp.content]})
        for b in resp.content:
            if b.type == "text" and b.text.strip():
                text_parts.append(b.text.strip())
        if resp.stop_reason != "tool_use":
            break
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                if on_tool:
                    on_tool(b.name)
                out = execute_tool(reader, b.name, b.input, history, interval, expected_vin4)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
        messages.append({"role": "user", "content": results})
    return "\n\n".join(text_parts)


def _openai_turn(engine, reader, system, history, interval, messages, expected_vin4, on_tool):
    text_parts = []
    while True:
        resp = engine.client.chat.completions.create(
            model=engine.model, messages=obd_images.inflate_for_openai(messages),
            tools=OPENAI_TOOLS)
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))
        if msg.content and msg.content.strip():
            text_parts.append(msg.content.strip())
        if not msg.tool_calls:
            break
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if on_tool:
                on_tool(tc.function.name)
            out = execute_tool(reader, tc.function.name, args, history, interval, expected_vin4)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})
    return "\n\n".join(text_parts)


def chat_loop(engine, reader, system, history, interval, messages, persist, state):
    """Terminal chat loop — reads user turns, runs them, prints the reply."""
    def show_tool(name):
        print(f"\033[2m  [reading: {name}]\033[0m")

    while True:
        user, attachments = collect_turn(state)
        if user is None:
            break
        messages.append(obd_images.user_turn(user, attachments))
        text = run_turn(engine, reader, system, history, interval, messages,
                        state.get("expected_vin4"), on_tool=show_tool)
        if text:
            print("\n" + text)
        persist()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Interactive OBD2 diagnostic chat")
    ap.add_argument("--vehicle", default=None,
                    help="vehicle description (default: identify it from the car's VIN, "
                         "then confirm at startup)")
    ap.add_argument("--port", default=None,
                    help="serial port, e.g. /dev/ttyUSB0 or /dev/rfcomm0 "
                         "(default: auto-detect USB and Bluetooth adapters)")
    ap.add_argument("--baud", type=int, default=None,
                    help="baud rate (default: auto-detect)")
    ap.add_argument("--simulate", action="store_true", help="run without hardware (demo vehicle)")
    ap.add_argument("--sim-car", choices=sorted(SIM_CARS), default="audi",
                    help="which car the simulator reports (default: audi)")
    ap.add_argument("--provider", choices=["claude", "openai"], default=None)
    ap.add_argument("--new", action="store_true", help="start a new session (skip the picker)")
    ap.add_argument("--session", default=None, help="resume a specific session_*.json file")
    ap.add_argument("--history", type=int, default=48, help="live-display sparkline length")
    ap.add_argument("--interval", type=float, default=None, help="seconds between live samples")
    args = ap.parse_args()

    interval = args.interval if args.interval is not None else (0.12 if args.simulate else 0.3)
    load_dotenv()

    # Choose: resume an existing session, or start new.
    if args.session:
        chosen = load_session_file(args.session)
    elif args.new:
        chosen = "new"
    else:
        sessions = load_all_sessions()
        if sessions and sys.stdin.isatty() and sys.stdout.isatty():
            chosen = pick_session(sessions)          # 'new' | dict | None
        else:
            chosen = "new"
    if chosen is None:
        print("Cancelled.")
        return

    resuming = chosen != "new"
    session = chosen if resuming else None
    provider = session["provider"] if resuming else (args.provider or choose_provider())
    engine = build_engine(provider)

    # Connect before identifying the car so the VIN comes off the real adapter.
    reader, simulated = open_reader(args)
    if reader is None:
        return 1        # no adapter and no consent to simulate — nothing to do
    args.simulate = simulated
    if args.interval is None:
        interval = 0.12 if simulated else 0.3

    interactive_tty = sys.stdin.isatty() and sys.stdout.isatty()
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if resuming:
        vehicle = session["vehicle"]        # keep the car the session was opened on
        vin4 = session.get("vin_last4")
    else:
        # Identify the car from its VIN (+ prior visits), then let the user confirm.
        vehicle, vin = obd_vehicle.resolve(
            reader, script_dir, default=DEFAULT_VEHICLE, explicit=args.vehicle,
            known=KNOWN_VEHICLE_VINS, interactive=interactive_tty)
        vin4 = vin[-4:] if vin else None

    print("=" * 64)
    banner = f"  OBD2 Chat — {vehicle}   [{engine.name}]"
    if resuming:
        banner += f"   · resuming {session['id']}"
    print(banner)
    if simulated:
        print(f"  [SIMULATE MODE — synthetic demo data for a reference {vehicle}.")
        print("   Not a real car in front of you; don't treat readings as literal.]")
    print("  Ask anything; it reads the car when useful. Type 'quit' to exit.")
    print("  Show it something: /pic (newest photo) · /snap (camera) · /help")
    print("=" * 64)

    if resuming:
        messages = session["messages"]
        last = _last_assistant_text(messages)
        if last:
            print(f"\n\033[2m(resuming — last reply)\033[0m\n{last}")
    else:
        session = new_session(vehicle, vin4, provider)
        messages = [] if engine.name == "Claude" else \
            [{"role": "system", "content": system_prompt(vehicle)}]

    system = system_prompt(vehicle)

    # Photos live beside the transcript, so resuming a session still shows them.
    state = {"media_dir": session_media_dir(session["id"]), "pending": [], "listing": [],
             "expected_vin4": session.get("vin_last4")}
    n_pics = obd_images.count_images(messages)
    if n_pics:
        print(f"\033[2m({n_pics} photo{'s' if n_pics != 1 else ''} in this session)\033[0m")

    def persist():
        save_session(session, messages)

    try:
        chat_loop(engine, reader, system, args.history, interval, messages, persist, state)
    finally:
        reader.close()
        path = save_session(session, messages)
        print(f"\nSession saved: {path}")


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except obd_connect.ObdConnectionError as e:
        print(f"\nOBD adapter problem:\n{e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
