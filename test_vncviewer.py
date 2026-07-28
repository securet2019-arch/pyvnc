#!/usr/bin/env python3
"""Connection-resilience tests for vncviewer.py, using a fake RFB server.

Run standalone (python test_vncviewer.py) or under pytest.  No network or
display needed: the server is a local socket speaking just enough RFB 3.8.

Covered:
  1. A server that pauses >1 s in the MIDDLE of a framebuffer rect must not
     kill the session (regression test for the old 1-second-timeout bug).
  2. When the server drops the connection, the client reconnects on its own
     and resumes receiving updates.
  3. When the server is gone entirely, the client reports status and still
     shuts down cleanly.
"""

import queue
import socket
import struct
import threading
import time

from vncviewer import VNCClient

W, H = 64, 48


# --------------------------------------------------------------------------
# fake RFB server helpers
# --------------------------------------------------------------------------

class FakeServer:
    def __init__(self):
        self.lsock = socket.socket()
        self.lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.lsock.bind(('127.0.0.1', 0))
        self.lsock.listen(2)
        self.port = self.lsock.getsockname()[1]

    def handshake(self, conn):
        """Server side of the RFB 3.8 handshake with security type None."""
        conn.sendall(b'RFB 003.008\n')
        assert conn.recv(12) == b'RFB 003.008\n'
        conn.sendall(bytes([1, 1]))                 # one type offered: None
        assert conn.recv(1) == b'\x01'
        conn.sendall(struct.pack('!I', 0))          # security result: OK
        conn.recv(1)                                # ClientInit
        pixfmt = (bytes([32, 24, 0, 1]) + struct.pack('!HHH', 255, 255, 255)
                  + bytes([16, 8, 0]) + b'\x00' * 3)
        name = b'fake'
        conn.sendall(struct.pack('!HH', W, H) + pixfmt
                     + struct.pack('!I', len(name)) + name)

    @staticmethod
    def read_client_msg(conn):
        """Consume one client->server message; return its type or None."""
        t = conn.recv(1)
        if not t:
            return None
        t = t[0]
        if t == 0:                                  # SetPixelFormat
            conn.recv(19)
        elif t == 2:                                # SetEncodings
            n = struct.unpack('!H', conn.recv(3)[1:])[0]
            conn.recv(n * 4)
        elif t == 3:                                # FramebufferUpdateRequest
            conn.recv(9)
        elif t == 4:                                # KeyEvent
            conn.recv(7)
        elif t == 5:                                # PointerEvent
            conn.recv(5)
        elif t == 6:                                # ClientCutText
            ln = struct.unpack('!I', conn.recv(7)[3:])[0]
            conn.recv(ln)
        return t

    @staticmethod
    def send_update(conn, color, pause_at=None, pause=0.0):
        """One full-screen Raw update.  If pause_at is set, split the pixel
        data there and sleep `pause` seconds in between (slow-server test)."""
        px = bytes([color[2], color[1], color[0], 0]) * (W * H)   # BGRX
        msg = (struct.pack('!BBH', 0, 0, 1)
               + struct.pack('!HHHHi', 0, 0, W, H, 0) + px)
        if pause_at is None:
            conn.sendall(msg)
        else:
            conn.sendall(msg[:pause_at])
            time.sleep(pause)
            conn.sendall(msg[pause_at:])

    def close(self):
        try:
            self.lsock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# client helpers
# --------------------------------------------------------------------------

def start_client(port):
    """Connect like main() does, then start the supervised reader thread."""
    client = VNCClient()
    client.open('127.0.0.1', port)
    client.authenticate(None)
    client.init()
    thread = threading.Thread(target=client.run_supervised,
                              args=('127.0.0.1', port, None), daemon=True)
    thread.start()
    return client, thread


def collect_until(client, predicate, timeout=15):
    """Drain client.events until predicate(ev) holds; return all events seen."""
    seen = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            ev = client.events.get(timeout=0.2)
        except queue.Empty:
            continue
        seen.append(ev)
        if predicate(ev):
            return seen
    raise AssertionError(f'timed out waiting for event; got {seen}')


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------

def test_slow_mid_message_pause_is_tolerated():
    """Server stalls 2 s (twice the socket poll timeout) halfway through the
    pixel data of a rect.  The old code declared the connection dead here;
    the fixed code must simply wait and deliver the rect intact."""
    srv = FakeServer()

    def serve():
        conn, _ = srv.lsock.accept()
        srv.handshake(conn)
        # wait for the client's update request, then drip the update out
        while srv.read_client_msg(conn) != 3:
            pass
        srv.send_update(conn, (10, 200, 30), pause_at=500, pause=2.0)
        # keep the session open but idle until the client goes away
        try:
            while srv.read_client_msg(conn) is not None:
                pass
        except OSError:
            pass
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    client, _ = start_client(srv.port)
    try:
        seen = collect_until(client, lambda ev: ev[0] == 'rect')
        bad = [ev for ev in seen if ev[0] == 'status']
        assert not bad, f'session reported trouble on a merely slow server: {bad}'
        assert client.fb.getpixel((W // 2, H // 2)) == (10, 200, 30)
    finally:
        client.close()
        srv.close()


def test_reconnect_after_server_drop():
    """Server accepts a session, serves one update, drops the connection.
    The client must reconnect by itself and resume updates on session #2."""
    srv = FakeServer()

    def serve():
        # session 1: one green update, then hang up
        conn, _ = srv.lsock.accept()
        srv.handshake(conn)
        while srv.read_client_msg(conn) != 3:
            pass
        srv.send_update(conn, (0, 255, 0))
        time.sleep(0.3)
        conn.close()
        # session 2: client should reconnect; blue update this time
        conn, _ = srv.lsock.accept()
        srv.handshake(conn)
        while srv.read_client_msg(conn) != 3:
            pass
        srv.send_update(conn, (0, 0, 255))
        try:
            while srv.read_client_msg(conn) is not None:
                pass
        except OSError:
            pass
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    client, _ = start_client(srv.port)
    try:
        collect_until(client, lambda ev: ev[0] == 'rect')          # session 1
        seen = collect_until(client, lambda ev: ev[0] == 'reconnected')
        assert any(ev[0] == 'status' for ev in seen), \
            f'expected a "connection lost" status before reconnect: {seen}'
        collect_until(client, lambda ev: ev[0] == 'rect')          # session 2
        assert client.fb.getpixel((W // 2, H // 2)) == (0, 0, 255)
    finally:
        client.close()
        srv.close()


def test_clean_shutdown_when_server_gone():
    """Server drops the session and stops listening: the client reports the
    problem via status events and still shuts down promptly on close()."""
    srv = FakeServer()

    def serve():
        conn, _ = srv.lsock.accept()
        srv.handshake(conn)
        while srv.read_client_msg(conn) != 3:
            pass
        srv.send_update(conn, (255, 0, 0))
        time.sleep(0.3)
        conn.close()
        srv.close()                     # nobody will ever listen again

    threading.Thread(target=serve, daemon=True).start()
    client, _ = start_client(srv.port)
    try:
        collect_until(client, lambda ev: ev[0] == 'rect')
        collect_until(client, lambda ev: ev[0] == 'status'
                      and 'reconnect failed' in ev[1])
    finally:
        t0 = time.monotonic()
        client.close()
        collect_until(client, lambda ev: ev[0] == 'closed', timeout=5)
        assert time.monotonic() - t0 < 5, 'close() did not shut down promptly'


if __name__ == '__main__':
    for fn in (test_slow_mid_message_pause_is_tolerated,
               test_reconnect_after_server_drop,
               test_clean_shutdown_when_server_gone):
        t0 = time.monotonic()
        fn()
        print(f'PASS {fn.__name__}  ({time.monotonic() - t0:.1f}s)')
    print('all connection-resilience tests passed')
