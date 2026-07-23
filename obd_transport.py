#!/usr/bin/env python3
"""Byte transports for the ELM327 — how the bytes move, separated from the protocol.

`Elm327Reader` used to be welded to pyserial. Splitting the byte layer out lets the
same reader talk to an adapter over:

    SerialTransport  a serial port  — USB cable or a bound Bluetooth rfcomm node
    TcpTransport     a TCP socket   — a WiFi ELM327 (host:port), or, on Android, a
                                      native Bluetooth/USB bridge re-exposed on localhost

Every transport is the same four operations the reader needs:

    reset_input()          discard anything buffered before a fresh command
    write(bytes)           send a command
    read_waiting() -> bytes  whatever bytes are available right now (b'' if none)
    close()

Read semantics mirror pyserial's `in_waiting`/`read`: `read_waiting()` never blocks
waiting for more, so the reader's own deadline loop controls timing unchanged. A peer
that has closed the connection surfaces as an OSError from `read_waiting()`, which the
reader treats as a dropped link and reconnects.

This module imports nothing from the rest of OBDAI (no circular deps): it raises plain
`TransportError`; callers wrap that into their own `ObdConnectionError`.
"""

import select
import socket

try:
    import serial  # pyserial — only needed for serial (USB / rfcomm) transports
except ImportError:
    serial = None


class TransportError(Exception):
    """A transport could not be opened or used. Wrapped by callers into ObdConnectionError."""


class Transport:
    kind = "transport"

    def reset_input(self):
        raise NotImplementedError

    def write(self, data):
        raise NotImplementedError

    def read_waiting(self):
        """Bytes available now, or b''. Raises OSError if the link has dropped."""
        raise NotImplementedError

    def close(self):
        pass


class SerialTransport(Transport):
    """A pyserial port — a USB cable or a bound Bluetooth /dev/rfcomm* node."""
    kind = "serial"

    def __init__(self, port, baud, timeout=1):
        if serial is None:
            raise TransportError("pyserial not installed — `pip install pyserial`, or use --simulate.")
        try:
            self.ser = serial.Serial(port, baud, timeout=timeout)
        except Exception as e:               # SerialException wraps OSError
            raise TransportError(str(e)) from e

    def reset_input(self):
        self.ser.reset_input_buffer()

    def write(self, data):
        self.ser.write(data)

    def read_waiting(self):
        n = self.ser.in_waiting
        return self.ser.read(n) if n else b""

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


class TcpTransport(Transport):
    """A TCP socket to an ELM327 — a WiFi adapter, or an on-device native bridge.

    The baud rate is meaningless over TCP (the link is virtual), so it takes none.
    """
    kind = "tcp"

    def __init__(self, host, port, connect_timeout=2.0):
        try:
            self.sock = socket.create_connection((host, port), timeout=connect_timeout)
        except OSError as e:
            raise TransportError(str(e)) from e
        self.sock.settimeout(0.3)

    def reset_input(self):
        self.sock.setblocking(False)
        try:
            while True:
                if not self.sock.recv(4096):
                    break                    # peer closed; leave it for read_waiting to report
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            pass
        finally:
            try:
                self.sock.settimeout(0.3)
            except OSError:
                pass

    def write(self, data):
        self.sock.sendall(data)

    def read_waiting(self):
        r, _, _ = select.select([self.sock], [], [], 0)
        if not r:
            return b""
        try:
            data = self.sock.recv(4096)
        except BlockingIOError:
            return b""
        if data == b"":                      # readable + empty = peer closed the link
            raise OSError("connection closed by the adapter")
        return data

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def parse_tcp(spec):
    """('host', port) if `spec` is a TCP endpoint, else None.

    Accepts `tcp:HOST:PORT` and `tcp://HOST:PORT`.
    """
    if not isinstance(spec, str):
        return None
    s = spec.strip()
    if not s.lower().startswith("tcp:"):
        return None
    rest = s[4:].lstrip("/")
    host, sep, port = rest.rpartition(":")
    if not sep or not host:
        raise TransportError(f"malformed TCP endpoint {spec!r} — expected tcp:HOST:PORT")
    try:
        return host, int(port)
    except ValueError:
        raise TransportError(f"malformed TCP port in {spec!r}")


def make_transport(port, baud, connect_timeout=2.0):
    """Build the right transport for `port`: a TCP endpoint (tcp:HOST:PORT) or a serial device."""
    tcp = parse_tcp(port)
    if tcp:
        return TcpTransport(tcp[0], tcp[1], connect_timeout=connect_timeout)
    return SerialTransport(port, baud)
