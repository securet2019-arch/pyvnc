#!/usr/bin/env python3
"""Loopback fake RFB server for manually testing the extension without the
real WSJT-X machine.

Usage:
    python extension/test/fake_server.py

It prints a websockify command to run in a second console, e.g.:
    python -m websockify 6081 127.0.0.1:<port>

Then in the extension options set Bridge port = 6081 and open the viewer tab:
expect the desktop name "fake" and a solid green screen.  Ctrl+C the fake
server to watch the viewer's reconnect cycle, restart it to see recovery.

Reuses FakeServer from test_vncviewer.py (repo root).  Manual test only.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from test_vncviewer import FakeServer          # noqa: E402


def serve_session(srv, conn):
    srv.handshake(conn)
    while srv.read_client_msg(conn) != 3:      # wait for update request
        pass
    srv.send_update(conn, (0, 200, 0))         # one full-screen green frame
    try:
        while srv.read_client_msg(conn) is not None:
            pass                               # idle until the client hangs up
    except OSError:
        pass


def main():
    srv = FakeServer()                         # binds an ephemeral port
    print(f'fake RFB server on 127.0.0.1:{srv.port} — now run:')
    print(f'    python -m websockify 6081 127.0.0.1:{srv.port}')
    print('and set Bridge port = 6081 in the extension options.')
    try:
        while True:
            conn, _ = srv.lsock.accept()
            print('session started')
            try:
                serve_session(srv, conn)
            except (OSError, AssertionError) as exc:
                print(f'session error: {exc}')
            conn.close()
            print('session ended — waiting for the viewer to reconnect')
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()


if __name__ == '__main__':
    main()
