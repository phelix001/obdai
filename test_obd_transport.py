#!/usr/bin/env python3
"""Tests for the transport layer + TCP/WiFi adapters (obd_transport, obd_connect, reader).

Spec asserted: the obd_transport docstring contract — the reader can talk to an ELM327
over a serial port OR a `tcp:HOST:PORT` endpoint; a WiFi/TCP adapter is discovered,
probed, connected, read, and reconnected exactly like a serial one; malformed endpoints
and dead sockets fail with an ObdConnectionError, never a raw traceback; and the serial
path is byte-for-byte unchanged (covered by the existing pty tests in test_obd_connect).

The "WiFi adapter" is a real localhost TCP server speaking the same ELM327 dialect as the
pty FakeElm, so the whole path (connect -> ATZ -> banner -> Mode 01 query) runs for real.

Run:  venv/bin/python -m pytest test_obd_transport.py -q
"""

import socket
import threading

import pytest

import obd_connect
import obd_diagnose
import obd_transport


# --------------------------------------------------------------------------- #
# A fake ELM327 on a TCP socket (mirrors the pty FakeElm)
# --------------------------------------------------------------------------- #
class FakeTcpElm:
    """Minimal ELM327 emulator on a localhost TCP socket. `endpoint` is 'tcp:host:port'."""

    def __init__(self, banner="ELM327 v1.5", answer_ecu=True, mute=False):
        self.banner = banner
        self.answer_ecu = answer_ecu
        self.mute = mute
        self.commands = []
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.host, self.port = self._srv.getsockname()
        self.endpoint = f"tcp:{self.host}:{self.port}"
        self._stop = threading.Event()
        self._conns = []
        self._t = threading.Thread(target=self._accept, daemon=True)
        self._t.start()

    def _accept(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        buf = b""
        conn.settimeout(0.5)
        try:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(1024)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                while b"\r" in buf:
                    line, buf = buf.split(b"\r", 1)
                    self._respond(conn, line.decode(errors="ignore").strip().upper())
        except Exception:
            return          # daemon thread — never surface a shutdown-race exception

    def _respond(self, conn, cmd):
        if not cmd:
            return
        self.commands.append(cmd)
        if self.mute:
            return
        if cmd in ("ATZ", "ATI"):
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
            conn.sendall(out.encode())
        except OSError:
            pass

    def drop_clients(self):
        """Simulate the adapter/link dropping mid-session."""
        for c in self._conns:
            try:
                c.close()
            except OSError:
                pass
        self._conns = []

    def close(self):
        self._stop.set()
        self.drop_clients()
        try:
            self._srv.close()
        except OSError:
            pass


@pytest.fixture
def tcp_elm():
    fake = FakeTcpElm()
    yield fake
    fake.close()


# --------------------------------------------------------------------------- #
# parse_tcp
# --------------------------------------------------------------------------- #
def test_parse_tcp_forms():
    assert obd_transport.parse_tcp("tcp:192.168.0.10:35000") == ("192.168.0.10", 35000)
    assert obd_transport.parse_tcp("tcp://10.0.0.5:35000") == ("10.0.0.5", 35000)
    assert obd_transport.parse_tcp("/dev/ttyUSB0") is None
    assert obd_transport.parse_tcp(None) is None


def test_parse_tcp_rejects_malformed():
    with pytest.raises(obd_transport.TransportError):
        obd_transport.parse_tcp("tcp:noport")
    with pytest.raises(obd_transport.TransportError):
        obd_transport.parse_tcp("tcp:host:notaport")


# --------------------------------------------------------------------------- #
# Discovery + probing over TCP
# --------------------------------------------------------------------------- #
def test_find_adapter_on_tcp_endpoint(tcp_elm):
    info = obd_connect.find_adapter(port=tcp_elm.endpoint, verbose=False)
    assert info["port"] == tcp_elm.endpoint
    assert info["kind"] == "wifi"
    assert info["baud"] is None          # baud is meaningless over TCP
    assert "ELM327" in info["banner"]


def test_probe_port_tcp(tcp_elm):
    baud, banner = obd_connect.probe_port(tcp_elm.endpoint, [None])
    assert baud is None and "ELM327" in banner


def test_probe_tcp_refused_is_port_level():
    # Nothing is listening on this port -> connection refused, flagged port_level.
    with pytest.raises(obd_connect.ObdConnectionError) as e:
        obd_connect.probe_port("tcp:127.0.0.1:1", [None])
    assert e.value.port_level is True


def test_silent_tcp_device_is_rejected():
    fake = FakeTcpElm(mute=True)
    try:
        with pytest.raises(obd_connect.ObdConnectionError) as e:
            obd_connect.find_adapter(port=fake.endpoint, verbose=False, probe_timeout=0.5)
        assert "no response" in str(e.value)
    finally:
        fake.close()


# --------------------------------------------------------------------------- #
# Connect + read over TCP (the full stack)
# --------------------------------------------------------------------------- #
def test_connect_and_read_over_tcp(monkeypatch, tcp_elm):
    monkeypatch.setattr(obd_connect, "list_candidate_ports",
                        lambda: [{"device": tcp_elm.endpoint, "kind": "wifi", "desc": "WiFi"}])
    reader = obd_connect.connect(verbose=False)
    try:
        assert reader.query("0C") == [0x0D, 0x48]     # RPM bytes returned over TCP
        assert "ATE0" in tcp_elm.commands             # init ran
    finally:
        reader.close()


def test_reader_accepts_tcp_port_directly(tcp_elm):
    reader = obd_diagnose.Elm327Reader(tcp_elm.endpoint, None)
    reader.init()
    try:
        assert reader.query("0C") == [0x0D, 0x48]
    finally:
        reader.close()


def test_reader_reconnects_after_tcp_drop(monkeypatch, tcp_elm):
    reader = obd_diagnose.Elm327Reader(tcp_elm.endpoint, None)
    reader.init()
    assert reader.query("0C") == [0x0D, 0x48]

    tcp_elm.drop_clients()                            # link drops mid-session
    monkeypatch.setattr(obd_transport.socket, "create_connection",
                        obd_transport.socket.create_connection)  # keep real
    monkeypatch.setattr(obd_diagnose.time, "sleep", lambda s: None)
    # next command should transparently reconnect and succeed
    assert reader.query("0C") == [0x0D, 0x48]
    reader.close()


def test_reader_open_failure_tcp_is_obd_error():
    with pytest.raises(obd_connect.ObdConnectionError):
        obd_diagnose.Elm327Reader("tcp:127.0.0.1:1", None)


def test_reopen_waits_for_the_adapter_to_actually_answer(monkeypatch):
    """The Android BT bridge keeps the localhost TCP port up while Bluetooth is still
    recovering, so a reconnect can 'succeed' yet read nothing. _reopen must require a
    real response and keep retrying until the adapter answers — not proceed on silence."""
    import obd_transport

    class FakeTransport:
        def reset_input(self): pass
        def write(self, data): pass
        def read_waiting(self): return b""
        def close(self): pass

    monkeypatch.setattr(obd_transport, "make_transport", lambda port, baud: FakeTransport())
    monkeypatch.setattr(obd_diagnose.time, "sleep", lambda s: None)

    reader = obd_diagnose.Elm327Reader("tcp:127.0.0.1:9", None)   # opens via the fake
    ati = iter(["", "", "ELM327 v1.5\r>"])       # silent twice, then answers
    def fake_cmd_once(command, timeout=2.0):
        return next(ati) if command == "ATI" else ""
    monkeypatch.setattr(reader, "_cmd_once", fake_cmd_once)

    assert reader._reopen() is True              # only after the adapter answered

    # and if it never answers, _reopen gives up (surfaced as a lost-connection error)
    reader2 = obd_diagnose.Elm327Reader("tcp:127.0.0.1:9", None)
    monkeypatch.setattr(reader2, "_cmd_once", lambda command, timeout=2.0: "")
    assert reader2._reopen() is False


def test_malformed_tcp_port_is_obd_error():
    with pytest.raises(obd_connect.ObdConnectionError):
        obd_diagnose.Elm327Reader("tcp:garbage", None)


# --------------------------------------------------------------------------- #
# Transport units
# --------------------------------------------------------------------------- #
def test_tcp_transport_roundtrip(tcp_elm):
    host, port = tcp_elm.host, tcp_elm.port
    t = obd_transport.TcpTransport(host, port)
    try:
        t.write(b"ATZ\r")
        import time
        deadline = time.time() + 2
        buf = b""
        while time.time() < deadline and b">" not in buf:
            buf += t.read_waiting()
            time.sleep(0.02)
        assert b"ELM327" in buf
    finally:
        t.close()


def test_tcp_transport_read_raises_when_peer_closes(tcp_elm):
    import time
    t = obd_transport.TcpTransport(tcp_elm.host, tcp_elm.port)
    # wait until the server has actually accepted before dropping (avoid a setup race)
    deadline = time.time() + 2
    while not tcp_elm._conns and time.time() < deadline:
        time.sleep(0.01)
    tcp_elm.drop_clients()
    with pytest.raises(OSError):
        # the closed socket surfaces as readable+empty -> OSError, so the reader reconnects
        for _ in range(100):
            t.read_waiting()
            time.sleep(0.02)
    t.close()
