#!/usr/bin/env python3
"""Tests for adapter discovery and connection (obd_connect).

Spec asserted: the docstring contract of obd_connect — an ELM327 is found on any
plausible port (USB or Bluetooth) at any common baud; when it cannot be found the
caller gets an ObdConnectionError carrying an actionable explanation, never a
traceback; and simulated data is only ever substituted with an explicit yes.

The "real adapter" is a pty running a small ELM327 emulator, so the serial path
(open -> ATZ -> banner -> Mode 01 query) is exercised for real without hardware.

Run:  venv/bin/python -m pytest test_obd_connect.py -q
"""

import os
import threading
import tty

import pytest

import obd_connect
import obd_diagnose


# --------------------------------------------------------------------------- #
# A fake ELM327 on a pty
# --------------------------------------------------------------------------- #
class FakeElm:
    """Minimal ELM327 emulator on a pty. `path` is the port to open."""

    def __init__(self, banner="ELM327 v1.5", answer_ecu=True, mute=False):
        self.master, self.slave = os.openpty()
        # Raw mode: no echo, no line discipline — a real serial link, not a terminal.
        tty.setraw(self.slave)
        # The slave fd stays open: dropping the last one hangs up the pty and the
        # emulator would die before pyserial ever opens the port.
        self.path = os.ttyname(self.slave)
        self.banner = banner
        self.answer_ecu = answer_ecu
        self.mute = mute            # a non-ELM device that never answers
        self.commands = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._serve, daemon=True)
        self._t.start()

    def _serve(self):
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = os.read(self.master, 1024)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                self._respond(line.decode(errors="ignore").strip().upper())

    def _respond(self, cmd):
        if not cmd:
            return
        self.commands.append(cmd)
        if self.mute:
            return
        if cmd == "ATZ" or cmd == "ATI":
            out = f"\r{self.banner}\r\r>"
        elif cmd == "ATRV":
            out = "\r14.2V\r\r>"
        elif cmd.startswith("AT"):
            out = "\rOK\r\r>"
        elif cmd == "0100":
            out = "\r41 00 BE 3E B8 11\r\r>" if self.answer_ecu else "\rNO DATA\r\r>"
        elif cmd == "010C":
            out = "\r41 0C 0D 48\r\r>" if self.answer_ecu else "\rNO DATA\r\r>"
        else:
            out = "\rNO DATA\r\r>"
        try:
            os.write(self.master, out.encode())
        except OSError:
            pass

    def close(self):
        self._stop.set()
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass


@pytest.fixture
def elm():
    fake = FakeElm()
    yield fake
    fake.close()


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_finds_adapter_on_pinned_port(elm):
    info = obd_connect.find_adapter(port=elm.path, verbose=False)
    assert info["port"] == elm.path
    assert info["baud"] in obd_connect.COMMON_BAUDS
    assert "ELM327" in info["banner"]


def test_autodetects_port_when_none_given(monkeypatch, elm):
    """No --port: the adapter is discovered by scanning candidate device nodes."""
    monkeypatch.setattr(obd_connect, "list_candidate_ports",
                        lambda: [{"device": elm.path, "kind": "usb", "desc": "fake"}])
    info = obd_connect.find_adapter(verbose=False)
    assert info["port"] == elm.path


def test_wrong_baud_hint_still_connects(elm):
    """A stale --baud must not be fatal: other rates are tried after it."""
    info = obd_connect.find_adapter(port=elm.path, baud=115200, verbose=False)
    assert info["port"] == elm.path


def test_bluetooth_port_is_probed(monkeypatch, elm):
    """An rfcomm node is a first-class candidate, not a special case."""
    monkeypatch.setattr(obd_connect, "list_candidate_ports",
                        lambda: [{"device": elm.path, "kind": "bluetooth", "desc": "BT"}])
    info = obd_connect.find_adapter(verbose=False)
    assert info["kind"] == "bluetooth"


def test_connect_returns_initialised_reader(monkeypatch, elm):
    monkeypatch.setattr(obd_connect, "list_candidate_ports",
                        lambda: [{"device": elm.path, "kind": "usb", "desc": ""}])
    reader = obd_connect.connect(verbose=False)
    try:
        assert reader.query("0C") == [0x0D, 0x48]      # RPM bytes came back
        assert "ATE0" in elm.commands                  # init actually ran
    finally:
        reader.close()


# --------------------------------------------------------------------------- #
# Failure paths — the crash this module exists to prevent
# --------------------------------------------------------------------------- #
def test_missing_port_raises_obd_error_not_serial_traceback():
    with pytest.raises(obd_connect.ObdConnectionError) as e:
        obd_connect.find_adapter(port="/dev/ttyUSB-nonexistent", verbose=False)
    assert "not present" in str(e.value)


def test_no_ports_at_all_gives_actionable_report(monkeypatch):
    monkeypatch.setattr(obd_connect, "list_candidate_ports", lambda: [])
    monkeypatch.setattr(obd_connect, "paired_bluetooth_adapters", lambda: [])
    with pytest.raises(obd_connect.ObdConnectionError) as e:
        obd_connect.find_adapter(verbose=False)
    msg = str(e.value)
    assert "No OBD2 adapter responded" in msg
    assert "--simulate" in msg          # the way out is stated
    assert "rfcomm" in msg              # so is the Bluetooth path


def test_silent_device_is_rejected_not_treated_as_adapter():
    """A serial device that answers nothing must not be mistaken for an ELM327."""
    fake = FakeElm(mute=True)
    try:
        with pytest.raises(obd_connect.ObdConnectionError) as e:
            obd_connect.find_adapter(port=fake.path, verbose=False, probe_timeout=0.3)
        assert "no response" in str(e.value)
    finally:
        fake.close()


def test_port_level_failure_is_flagged_for_skipping():
    with pytest.raises(obd_connect.ObdConnectionError) as e:
        obd_connect.probe_port("/dev/ttyUSB-nonexistent", [38400, 9600])
    assert e.value.port_level is True


def test_permission_denied_is_explained():
    err = PermissionError(13, "Permission denied")
    assert "dialout" in obd_connect._classify_open_error("/dev/ttyUSB0", err)


def test_unbound_paired_adapter_is_reported(monkeypatch):
    """Paired-but-not-bound is the classic Bluetooth failure: say so, don't hang."""
    monkeypatch.setattr(obd_connect, "list_candidate_ports", lambda: [])
    monkeypatch.setattr(obd_connect, "paired_bluetooth_adapters",
                        lambda: [{"mac": "00:11:22:33:44:55", "name": "OBDII"}])
    with pytest.raises(obd_connect.ObdConnectionError) as e:
        obd_connect.find_adapter(interactive=False, verbose=False)
    assert "not bound" in str(e.value)


# --------------------------------------------------------------------------- #
# Bluetooth helpers
# --------------------------------------------------------------------------- #
def test_paired_adapter_parsing(monkeypatch):
    monkeypatch.setattr(obd_connect.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(obd_connect, "_run", lambda *a, **k: (
        "Device AA:BB:CC:DD:EE:FF OBDII\n"
        "Device 11:22:33:44:55:66 Sony WH-1000XM4\n"
        "Device 99:88:77:66:55:44 Vgate iCar Pro\n"))
    found = obd_connect.paired_bluetooth_adapters()
    assert [d["name"] for d in found] == ["OBDII", "Vgate iCar Pro"]  # headphones excluded


def test_rfcomm_channel_falls_back_when_sdptool_absent(monkeypatch):
    monkeypatch.setattr(obd_connect.shutil, "which", lambda n: None)
    assert obd_connect.rfcomm_channel("AA:BB:CC:DD:EE:FF") == 1


def test_bind_without_rfcomm_tool_says_how_to_fix(monkeypatch):
    monkeypatch.setattr(obd_connect.shutil, "which", lambda n: None)
    with pytest.raises(obd_connect.ObdConnectionError) as e:
        obd_connect.bind_rfcomm("AA:BB:CC:DD:EE:FF")
    assert "bluez" in str(e.value)


def test_phantom_serial_ports_are_not_scanned(monkeypatch):
    """32 phantom /dev/ttyS* UARTs would otherwise stall every scan for minutes."""
    class P:
        def __init__(self, device, hwid, description="n/a"):
            self.device, self.hwid, self.description = device, hwid, description

    monkeypatch.setattr(obd_connect, "glob", type("g", (), {"glob": staticmethod(lambda p: [])}))
    monkeypatch.setattr(obd_connect.list_ports, "comports",
                        lambda: [P("/dev/ttyS0", "n/a"),
                                 P("/dev/ttyS4", "PNP0501", "16550A"),
                                 P("/dev/ttyUSB0", "USB VID:PID=1a86:7523", "CH340")])
    devices = [c["device"] for c in obd_connect.list_candidate_ports()]
    assert devices == ["/dev/ttyS4", "/dev/ttyUSB0"]


# --------------------------------------------------------------------------- #
# Reader resilience
# --------------------------------------------------------------------------- #
def test_reader_reconnects_after_link_drop(monkeypatch, elm):
    reader = obd_diagnose.Elm327Reader(elm.path, 38400)
    reader.init()
    assert reader.query("0C") == [0x0D, 0x48]

    calls = {"n": 0}
    real_cmd_once = reader._cmd_once

    def flaky(command, timeout=2.0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(5, "Input/output error")   # link drops mid-session
        return real_cmd_once(command, timeout)

    monkeypatch.setattr(reader, "_cmd_once", flaky)
    monkeypatch.setattr(obd_connect.time, "sleep", lambda s: None)
    assert reader.query("0C") == [0x0D, 0x48]        # recovered, same reading
    reader.close()


def test_reader_raises_obd_error_when_reconnect_fails(monkeypatch, elm):
    reader = obd_diagnose.Elm327Reader(elm.path, 38400)
    monkeypatch.setattr(reader, "_cmd_once",
                        lambda *a, **k: (_ for _ in ()).throw(OSError(5, "I/O error")))
    monkeypatch.setattr(reader, "_reopen", lambda: False)
    with pytest.raises(obd_connect.ObdConnectionError) as e:
        reader.cmd("0100")
    assert "lost the connection" in str(e.value)
    reader.close()


def test_reader_open_failure_is_an_obd_error():
    with pytest.raises(obd_connect.ObdConnectionError):
        obd_diagnose.Elm327Reader("/dev/ttyUSB-nonexistent", 38400)


def test_ecu_status_flags_ignition_off():
    """Adapter alive but ECU silent must be reported, not shown as real readings."""
    fake = FakeElm(answer_ecu=False)
    try:
        reader = obd_diagnose.Elm327Reader(fake.path, 38400)
        reader.init()
        status = obd_connect.ecu_status(reader)
        assert status["ecu_ok"] is False
        assert status["voltage"] == pytest.approx(14.2)
        reader.close()
    finally:
        fake.close()


# --------------------------------------------------------------------------- #
# Simulate fallback: only ever with an explicit yes
# --------------------------------------------------------------------------- #
class Args:
    def __init__(self, **kw):
        self.port = self.baud = None
        self.simulate = False
        self.interval = None
        self.__dict__.update(kw)


def _no_adapter(monkeypatch):
    def boom(**kwargs):
        raise obd_connect.ObdConnectionError("nothing plugged in")
    monkeypatch.setattr(obd_diagnose.obd_connect, "connect", boom)


def test_simulate_flag_uses_simulator_without_touching_hardware():
    reader, simulated = obd_diagnose.open_reader(Args(simulate=True))
    assert simulated is True
    assert isinstance(reader, obd_diagnose.SimReader)


def test_no_adapter_non_interactive_aborts_instead_of_simulating(monkeypatch):
    _no_adapter(monkeypatch)
    monkeypatch.setattr(obd_diagnose.sys.stdin, "isatty", lambda: False)
    reader, simulated = obd_diagnose.open_reader(Args())
    assert reader is None and simulated is False


def test_no_adapter_declining_simulate_aborts(monkeypatch):
    _no_adapter(monkeypatch)
    monkeypatch.setattr(obd_diagnose.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(obd_diagnose.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "")      # bare Enter = no
    reader, simulated = obd_diagnose.open_reader(Args())
    assert reader is None and simulated is False


def test_no_adapter_accepting_simulate_returns_simulator(monkeypatch):
    _no_adapter(monkeypatch)
    monkeypatch.setattr(obd_diagnose.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(obd_diagnose.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    reader, simulated = obd_diagnose.open_reader(Args())
    assert simulated is True
    assert isinstance(reader, obd_diagnose.SimReader)
