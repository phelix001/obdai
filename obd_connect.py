#!/usr/bin/env python3
"""Find and open an ELM327 OBD2 adapter — USB or Bluetooth — without crashing.

The adapter is not always on the same device node: a USB cable shows up as
/dev/ttyUSB0 (or ttyACM0 with some clones), a Bluetooth adapter as /dev/rfcomm0
only after it has been bound to its MAC address. Baud rate varies by clone too
(38400 and 9600 are the common ones; some run at 115200 or 500000).

So instead of assuming one port at one baud, this module:
  1. enumerates every plausible serial port (USB + already-bound rfcomm),
  2. probes each one with ATZ/ATI and keeps the first that answers like an ELM327,
  3. if nothing answers, looks for a *paired* Bluetooth OBD adapter and offers
     to bind it to an rfcomm node, then re-probes,
  4. fails with an actionable message — never a traceback.

Run it directly to diagnose adapter problems:

    venv/bin/python obd_connect.py            # scan and report
    venv/bin/python obd_connect.py --port /dev/rfcomm0
"""

import glob
import os
import re
import shutil
import subprocess
import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pyserial absent — only --simulate will work
    serial = None
    list_ports = None


class ObdConnectionError(Exception):
    """No usable ELM327 adapter — carries a human-readable explanation.

    `port_level` marks a failure of the port itself (missing, busy, no permission)
    as opposed to "opened fine but nothing answered" — retrying such a port at a
    different baud is pointless.
    """

    def __init__(self, message, port_level=False):
        super().__init__(message)
        self.port_level = port_level


# Device-node patterns, in the order we prefer to try them.
PORT_GLOBS = [
    ("usb", "/dev/ttyUSB*"),
    ("usb", "/dev/ttyACM*"),
    ("bluetooth", "/dev/rfcomm*"),
]

# Bauds worth trying, fast ones first. Bluetooth SPP ignores baud entirely.
COMMON_BAUDS = [38400, 9600]
RARE_BAUDS = [115200, 500000, 57600, 230400, 19200]

# Bluetooth device names that look like an OBD2 adapter.
BT_NAME_HINTS = ("obd", "elm", "obdii", "obd2", "vgate", "viecar", "vlink",
                 "veepeak", "konnwei", "icar", "kiwi", "carista", "scan tool")

# An ELM327 (or clone) identifies itself in the ATZ/ATI banner.
_ELM_BANNER = re.compile(r"ELM\s*32|OBD\s*II|STN\d|v\d\.\d", re.IGNORECASE)

_PHANTOM_TTYS = re.compile(r"^/dev/ttyS\d+$")


# --------------------------------------------------------------------------- #
# Port discovery
# --------------------------------------------------------------------------- #
def list_candidate_ports():
    """Every serial device that could plausibly be an OBD adapter.

    Returns a list of dicts: {"device", "kind", "desc"} ordered usb-first.
    """
    seen = {}
    for kind, pattern in PORT_GLOBS:
        for dev in sorted(glob.glob(pattern)):
            seen.setdefault(dev, {"device": dev, "kind": kind, "desc": ""})

    # pyserial adds USB vendor/product strings, and catches nodes our globs miss.
    if list_ports is not None:
        for p in list_ports.comports():
            # Motherboards advertise 32 phantom /dev/ttyS* legacy UARTs that no
            # adapter is ever on and that take seconds each to time out. Keep a
            # ttyS only when the kernel reports real hardware behind it.
            if _PHANTOM_TTYS.match(p.device) and (p.hwid or "n/a").strip() in ("", "n/a"):
                continue
            entry = seen.setdefault(p.device, {
                "device": p.device,
                "kind": "bluetooth" if "rfcomm" in p.device else "usb",
                "desc": "",
            })
            entry["desc"] = (p.description or "").strip()

    order = {"usb": 0, "bluetooth": 1}
    return sorted(seen.values(), key=lambda e: (order.get(e["kind"], 2), e["device"]))


def _classify_open_error(port, err):
    """Turn a serial open failure into a short reason string."""
    errno = getattr(err, "errno", None)
    if errno == 2 or isinstance(err, FileNotFoundError):
        return "not present"
    if errno == 13 or isinstance(err, PermissionError):
        return "permission denied (add yourself to the 'dialout' group)"
    if errno == 16:
        return "busy (another program has the port open)"
    if errno == 5:
        return "I/O error (Bluetooth link down, or adapter unplugged)"
    return str(err)


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #
def _read_until_prompt(ser, timeout):
    """Read until the ELM327 '>' prompt or timeout. Returns whatever arrived."""
    buf = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        waiting = ser.in_waiting
        if waiting:
            buf += ser.read(waiting).decode(errors="ignore")
            if ">" in buf:
                break
        else:
            time.sleep(0.05)
    return buf


def probe_port(port, bauds, per_baud_timeout=2.5):
    """Is there an ELM327 on `port`? Returns (baud, banner) or raises ObdConnectionError.

    The exception message says *why* (missing / permission / no answer), which is
    what makes the final error report useful instead of a bare traceback.
    """
    if serial is None:
        raise ObdConnectionError("pyserial is not installed — `pip install pyserial`, or use --simulate.")

    last_reason = "no response"
    for baud in bauds:
        try:
            ser = serial.Serial(port, baud, timeout=0.3)
        except Exception as e:  # SerialException wraps OSError; catch broadly
            # A port-level problem (missing/permission/busy) won't change with baud.
            raise ObdConnectionError(_classify_open_error(port, e), port_level=True)
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            ser.write(b"\r")
            time.sleep(0.1)
            ser.reset_input_buffer()
            ser.write(b"ATZ\r")
            resp = _read_until_prompt(ser, per_baud_timeout)
            if not _ELM_BANNER.search(resp):
                ser.write(b"ATI\r")
                resp += _read_until_prompt(ser, 1.5)
            if _ELM_BANNER.search(resp):
                banner = " ".join(resp.replace(">", " ").split())
                return baud, banner
            last_reason = "garbled response" if resp.strip() else "no response"
        except Exception as e:
            last_reason = str(e)
        finally:
            try:
                ser.close()
            except Exception:
                pass
    raise ObdConnectionError(last_reason)


# --------------------------------------------------------------------------- #
# Bluetooth
# --------------------------------------------------------------------------- #
def _run(cmd, timeout=8):
    """Run a command, returning stdout ('' on any failure). Never raises."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return ""


def paired_bluetooth_adapters():
    """Paired Bluetooth devices whose name looks like an OBD adapter.

    Returns [{"mac", "name"}]. Empty if bluetoothctl is missing or nothing matches.
    """
    if not shutil.which("bluetoothctl"):
        return []
    out = _run(["bluetoothctl", "devices", "Paired"])
    if "Device" not in out:  # older bluez spells it differently
        out = _run(["bluetoothctl", "paired-devices"])
    found = []
    for line in out.splitlines():
        m = re.match(r"\s*Device\s+([0-9A-Fa-f:]{17})\s+(.*)", line)
        if not m:
            continue
        mac, name = m.group(1).upper(), m.group(2).strip()
        if any(h in name.lower() for h in BT_NAME_HINTS):
            found.append({"mac": mac, "name": name})
    return found


def rfcomm_channel(mac, default=1):
    """SPP channel advertised by the adapter, per sdptool. Falls back to `default`."""
    if not shutil.which("sdptool"):
        return default
    out = _run(["sdptool", "browse", "--tree", mac], timeout=15)
    block = ""
    for chunk in re.split(r"(?i)service name:", out):
        if "serial port" in chunk.lower() or "spp" in chunk.lower():
            block = chunk
            break
    m = re.search(r"Channel[^\d]{0,10}(\d+)", block or out)
    return int(m.group(1)) if m else default


def free_rfcomm_node():
    """First /dev/rfcommN node not already present."""
    for n in range(0, 10):
        if not os.path.exists(f"/dev/rfcomm{n}"):
            return n, f"/dev/rfcomm{n}"
    raise ObdConnectionError("all /dev/rfcomm0-9 nodes are in use")


def bind_rfcomm(mac, channel=None, node=None):
    """Bind a paired Bluetooth adapter to an rfcomm device node.

    Returns the device path. Raises ObdConnectionError with the exact command to
    run by hand if binding needs privileges we don't have.
    """
    if not shutil.which("rfcomm"):
        raise ObdConnectionError(
            "the 'rfcomm' tool is not installed — `sudo apt install bluez` "
            "(it is what turns a paired adapter into a /dev/rfcomm* port)")
    if channel is None:
        channel = rfcomm_channel(mac)
    if node is None:
        node, _ = free_rfcomm_node()
    dev = f"/dev/rfcomm{node}"
    cmd = ["rfcomm", "bind", str(node), mac, str(channel)]
    out = _run(cmd)
    if not os.path.exists(dev):
        out = _run(["sudo", "-n"] + cmd)          # non-interactive sudo, if allowed
    if not os.path.exists(dev):
        out = _run(["sudo"] + cmd, timeout=90)    # may prompt for a password
    if not os.path.exists(dev):
        raise ObdConnectionError(
            f"could not bind {mac} to {dev}"
            + (f" ({out.strip().splitlines()[-1]})" if out.strip() else "")
            + f"\n  Run this yourself, then retry:  sudo rfcomm bind {node} {mac} {channel}")
    return dev


# --------------------------------------------------------------------------- #
# Connect
# --------------------------------------------------------------------------- #
def _ask(prompt):
    """Yes/no prompt. Returns False when there is no interactive terminal."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    try:
        return input(prompt).strip().lower().startswith("y")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _bauds_to_try(kind, preferred=None, thorough=False):
    bauds = []
    if preferred:
        bauds.append(preferred)
    if kind == "bluetooth":
        # SPP is a virtual link — the baud is cosmetic, one try is enough.
        bauds += [b for b in [38400] if b not in bauds]
        return bauds
    bauds += [b for b in COMMON_BAUDS if b not in bauds]
    if thorough:
        bauds += [b for b in RARE_BAUDS if b not in bauds]
    return bauds


def find_adapter(port=None, baud=None, interactive=True, verbose=True, probe_timeout=2.5):
    """Locate a working ELM327. Returns {"port", "baud", "kind", "banner"}.

    `port` pins the search to one device (still auto-detecting baud); `baud`
    is tried first but other rates are attempted if it does not answer.
    Raises ObdConnectionError with a diagnosis if nothing is found.
    """
    if serial is None:
        raise ObdConnectionError("pyserial is not installed — `pip install pyserial`, or use --simulate.")

    def say(msg):
        if verbose:
            print(msg, flush=True)

    if port:
        kind = "bluetooth" if "rfcomm" in port else "usb"
        candidates = [{"device": port, "kind": kind, "desc": "specified with --port"}]
    else:
        candidates = list_candidate_ports()

    failures = {}       # device -> reason (last one wins; one line per port)
    dead_ports = set()  # ports that could not even be opened — no point retrying
    for thorough in (False, True):
        for c in candidates:
            if c["device"] in dead_ports:
                continue
            if thorough and c["kind"] == "bluetooth":
                continue  # SPP ignores baud — the first pass already settled it
            bauds = _bauds_to_try(c["kind"], baud, thorough)
            label = c["device"] + (f" ({c['desc']})" if c["desc"] else "")
            say(f"  probing {label} at {', '.join(str(b) for b in bauds)} ...")
            try:
                found_baud, banner = probe_port(c["device"], bauds, probe_timeout)
            except ObdConnectionError as e:
                failures[c["device"]] = str(e)
                if e.port_level:
                    dead_ports.add(c["device"])
                say(f"    -> {e}")
                continue
            say(f"    -> ELM327 found: {banner}")
            return {"port": c["device"], "baud": found_baud, "kind": c["kind"], "banner": banner}
        if port:
            break  # user pinned a port; a second sweep of the same one is pointless

    # Nothing answered on an existing port. Is there a paired Bluetooth adapter
    # that simply has no /dev/rfcomm* node yet?
    if not port:
        bt = paired_bluetooth_adapters()
        already_bound = any(c["kind"] == "bluetooth" for c in candidates)
        if bt and not already_bound:
            dev = bt[0]
            say(f"\n  Paired Bluetooth adapter found: {dev['name']} ({dev['mac']}) "
                f"— but it is not bound to a serial port yet.")
            if interactive and _ask(f"  Bind it now (needs sudo)? [y/N] "):
                node = bind_rfcomm(dev["mac"])   # raises with the manual command
                say(f"  Bound to {node}; probing ...")
                found_baud, banner = probe_port(node, _bauds_to_try("bluetooth", baud), probe_timeout)
                say(f"    -> ELM327 found: {banner}")
                return {"port": node, "baud": found_baud, "kind": "bluetooth", "banner": banner}
            failures[dev["name"]] = "paired but not bound to an rfcomm port"

    raise ObdConnectionError(_failure_report(port, candidates, failures))


def _failure_report(port, candidates, failures):
    lines = ["No OBD2 adapter responded."]
    if port:
        lines.append(f"  Port searched: {port} (pinned with --port)")
    elif not candidates:
        lines.append("  No serial ports exist at all (no /dev/ttyUSB*, /dev/ttyACM*, /dev/rfcomm*)")
        lines.append("  — so nothing is plugged in, and no Bluetooth adapter is bound.")
    for dev, reason in sorted(failures.items()):
        lines.append(f"    - {dev}: {reason}")

    lines.append("")
    lines.append("  Checks, in the order they usually fix it:")
    lines.append("    USB:       is the cable plugged into both the car and this machine?")
    lines.append("               `dmesg | tail` should show a ttyUSB/ttyACM device on plug-in.")
    lines.append("    Bluetooth: pair the adapter first (`bluetoothctl` -> scan on / pair / trust),")
    lines.append("               then `sudo rfcomm bind 0 <MAC> 1` and re-run.")
    lines.append("    Both:      the ignition must be ON (adapters are powered by the OBD port).")
    lines.append("    Permissions: `sudo usermod -aG dialout $USER`, then log out and back in.")
    lines.append("")
    lines.append("  No hardware to hand? Re-run with --simulate for the demo vehicle.")
    return "\n".join(lines)


def connect(port=None, baud=None, interactive=True, verbose=True):
    """Find an adapter and return an initialised reader (obd_diagnose.Elm327Reader).

    Raises ObdConnectionError if no adapter can be reached.
    """
    from obd_diagnose import Elm327Reader  # deferred: obd_diagnose imports this module

    if verbose:
        print("\nLooking for an OBD2 adapter...")
    info = find_adapter(port=port, baud=baud, interactive=interactive, verbose=verbose)
    reader = Elm327Reader(info["port"], info["baud"])
    reader.init()

    status = ecu_status(reader)
    if verbose:
        kind = "Bluetooth" if info["kind"] == "bluetooth" else "USB"
        print(f"Connected: {kind} adapter on {info['port']} @ {info['baud']} baud — {status['text']}")
    if not status["ecu_ok"]:
        print("  WARNING: the adapter answers but the ECU does not. Turn the ignition to ON "
              "(engine running for live data) — readings below may come back as 'No Data'.")
    return reader


def ecu_status(reader):
    """Is the car's ECU actually talking? Returns {"ecu_ok", "voltage", "text"}."""
    voltage = None
    try:
        m = re.search(r"(\d+\.\d+)\s*V", reader.cmd("ATRV"))
        if m:
            voltage = float(m.group(1))
    except Exception:
        pass
    try:
        ecu_ok = reader.query("00") is not None       # Mode 01 PID 00: supported PIDs
    except Exception:
        ecu_ok = False
    if ecu_ok:
        text = "ECU responding"
    else:
        text = "adapter OK, no ECU response (ignition off?)"
    if voltage is not None:
        text += f", {voltage:.1f} V at the OBD port"
    return {"ecu_ok": ecu_ok, "voltage": voltage, "text": text}


# --------------------------------------------------------------------------- #
# Standalone diagnostic CLI
# --------------------------------------------------------------------------- #
def main():
    import argparse
    ap = argparse.ArgumentParser(description="Find and test an ELM327 OBD2 adapter")
    ap.add_argument("--port", default=None, help="pin the search to one device node")
    ap.add_argument("--baud", type=int, default=None, help="try this baud first")
    args = ap.parse_args()

    ports = list_candidate_ports()
    print("Serial ports present:")
    for p in ports:
        print(f"  {p['device']:<16} {p['kind']:<10} {p['desc']}")
    if not ports:
        print("  (none)")

    bt = paired_bluetooth_adapters()
    print("\nPaired Bluetooth OBD adapters:")
    for d in bt:
        print(f"  {d['mac']}  {d['name']}")
    if not bt:
        print("  (none — pair one with bluetoothctl if you use a wireless adapter)")

    print()
    try:
        info = find_adapter(port=args.port, baud=args.baud)
    except ObdConnectionError as e:
        print(f"\n{e}")
        return 1
    print(f"\nAdapter ready: {info['port']} @ {info['baud']} baud ({info['kind']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
