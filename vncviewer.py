#!/usr/bin/env python3
"""
pyvnc - a small VNC (RFB 3.8) viewer with real Tight-encoding support.

Pure Python + Pillow. Features:
  * Encodings: Raw, CopyRect and Tight (fill / jpeg / copy / palette /
    gradient sub-encodings, 4 persistent zlib streams)
  * VNC password authentication (DES challenge-response, no crypto deps)
  * Desktop resize (DesktopSize pseudo-encoding)
  * Scales the display to fit your screen (--scale), mouse mapped back
  * Fullscreen toggle with F11 (letterboxed, aspect preserved)
  * Keyboard + mouse input (incl. wheel), server clipboard sync
  * Resilient connection: tolerates slow servers mid-message, TCP keepalive
    detects dead peers, automatic reconnect with backoff (last frame stays
    on screen, status shown in the title bar)
  * tkinter UI (stdlib) - no other dependencies

Usage:
    python vncviewer.py                      # 192.168.1.176:5900, fit to screen
    python vncviewer.py 192.168.1.176 -p secret
    python vncviewer.py --scale 1.0          # no scaling (native pixels)
    python vncviewer.py --selftest           # run built-in crypto/protocol self test
"""

import argparse
import io
import queue
import socket
import struct
import sys
import threading
import time
import zlib

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow is required:  pip install pillow")

# --------------------------------------------------------------------------
# RFB protocol constants
# --------------------------------------------------------------------------

ENC_RAW        = 0
ENC_COPYRECT   = 1
ENC_TIGHT      = 7
ENC_DESKTOPSZ  = -223          # pseudo-encoding: framebuffer resized

SEC_NONE       = 1
SEC_VNCAUTH    = 2

# Tight compression-control upper-nibble values (after >> 4)
TIGHT_EXPLICIT_FILTER = 0x04
TIGHT_FILL            = 0x08
TIGHT_JPEG            = 0x09
TIGHT_FILTER_COPY     = 0x00
TIGHT_FILTER_PALETTE  = 0x01
TIGHT_FILTER_GRADIENT = 0x02

MAX_RECT_DATA = 1 << 24        # sanity bound for one compressed blob (16 MiB)

# The socket uses a short (1 s) recv timeout purely as a wakeup so the reader
# thread can notice a shutdown request.  A timeout is NEVER, by itself, proof
# of a dead connection: a busy server may legitimately pause for several
# seconds in the middle of a framebuffer update.  Only give up when a message
# makes no progress at all for MSG_TIMEOUT seconds (the reconnect logic then
# re-establishes the session).  Half-open connections (crashed host, dropped
# network) are detected by TCP keepalive at the OS level.
SOCK_POLL      = 1.0           # recv wakeup interval (clean shutdown)
MSG_TIMEOUT    = 30.0          # no progress mid-message -> connection is dead
RECONNECT_MIN  = 1.0           # first reconnect delay; doubles up to ...
RECONNECT_MAX  = 30.0          # ... this cap between attempts


class RFBError(Exception):
    """Fatal protocol / connection error."""


# --------------------------------------------------------------------------
# DES (pure Python) - needed for VNC authentication
# --------------------------------------------------------------------------

_IP = [58,50,42,34,26,18,10,2,60,52,44,36,28,20,12,4,
       62,54,46,38,30,22,14,6,64,56,48,40,32,24,16,8,
       57,49,41,33,25,17,9,1,59,51,43,35,27,19,11,3,
       61,53,45,37,29,21,13,5,63,55,47,39,31,23,15,7]

_FP = [40,8,48,16,56,24,64,32,39,7,47,15,55,23,63,31,
       38,6,46,14,54,22,62,30,37,5,45,13,53,21,61,29,
       36,4,44,12,52,20,60,28,35,3,43,11,51,19,59,27,
       34,2,42,10,50,18,58,26,33,1,41,9,49,17,57,25]

_E = [32,1,2,3,4,5,4,5,6,7,8,9,8,9,10,11,12,13,
      12,13,14,15,16,17,16,17,18,19,20,21,20,21,22,23,24,25,
      24,25,26,27,28,29,28,29,30,31,32,1]

_P = [16,7,20,21,29,12,28,17,1,15,23,26,5,18,31,10,
      2,8,24,14,32,27,3,9,19,13,30,6,22,11,4,25]

_PC1 = [57,49,41,33,25,17,9,1,58,50,42,34,26,18,
        10,2,59,51,43,35,27,19,11,3,60,52,44,36,
        63,55,47,39,31,23,15,7,62,54,46,38,30,22,
        14,6,61,53,45,37,29,21,13,5,28,20,12,4]

_PC2 = [14,17,11,24,1,5,3,28,15,6,21,10,
        23,19,12,4,26,8,16,7,27,20,13,2,
        41,52,31,37,47,55,30,40,51,45,33,48,
        44,49,39,56,34,53,46,42,50,36,29,32]

_SHIFTS = [1,1,2,2,2,2,2,2,1,2,2,2,2,2,2,1]

_SBOX = [
    [14,4,13,1,2,15,11,8,3,10,6,12,5,9,0,7,
     0,15,7,4,14,2,13,1,10,6,12,11,9,5,3,8,
     4,1,14,8,13,6,2,11,15,12,9,7,3,10,5,0,
     15,12,8,2,4,9,1,7,5,11,3,14,10,0,6,13],
    [15,1,8,14,6,11,3,4,9,7,2,13,12,0,5,10,
     3,13,4,7,15,2,8,14,12,0,1,10,6,9,11,5,
     0,14,7,11,10,4,13,1,5,8,12,6,9,3,2,15,
     13,8,10,1,3,15,4,2,11,6,7,12,0,5,14,9],
    [10,0,9,14,6,3,15,5,1,13,12,7,11,4,2,8,
     13,7,0,9,3,4,6,10,2,8,5,14,12,11,15,1,
     13,6,4,9,8,15,3,0,11,1,2,12,5,10,14,7,
     1,10,13,0,6,9,8,7,4,15,14,3,11,5,2,12],
    [7,13,14,3,0,6,9,10,1,2,8,5,11,12,4,15,
     13,8,11,5,6,15,0,3,4,7,2,12,1,10,14,9,
     10,6,9,0,12,11,7,13,15,1,3,14,5,2,8,4,
     3,15,0,6,10,1,13,8,9,4,5,11,12,7,2,14],
    [2,12,4,1,7,10,11,6,8,5,3,15,13,0,14,9,
     14,11,2,12,4,7,13,1,5,0,15,10,3,9,8,6,
     4,2,1,11,10,13,7,8,15,9,12,5,6,3,0,14,
     11,8,12,7,1,14,2,13,6,15,0,9,10,4,5,3],
    [12,1,10,15,9,2,6,8,0,13,3,4,14,7,5,11,
     10,15,4,2,7,12,9,5,6,1,13,14,0,11,3,8,
     9,14,15,5,2,8,12,3,7,0,4,10,1,13,11,6,
     4,3,2,12,9,5,15,10,11,14,1,7,6,0,8,13],
    [4,11,2,14,15,0,8,13,3,12,9,7,5,10,6,1,
     13,0,11,7,4,9,1,10,14,3,5,12,2,15,8,6,
     1,4,11,13,12,3,7,14,10,15,6,8,0,5,9,2,
     6,11,13,8,1,4,10,7,9,5,0,15,14,2,3,12],
    [13,2,8,4,6,15,11,1,10,9,3,14,5,0,12,7,
     1,15,13,8,10,3,7,4,12,5,6,11,0,14,9,2,
     7,11,4,1,9,12,14,2,0,6,10,13,15,3,5,8,
     2,1,14,7,4,10,8,13,15,12,9,0,3,5,6,11],
]

_BITREV = bytes(sum(((b >> i) & 1) << (7 - i) for i in range(8))
                for b in range(256))


def _permute(value, table, inbits):
    out = 0
    for pos in table:
        out = (out << 1) | ((value >> (inbits - pos)) & 1)
    return out


def _des_subkeys(key64):
    k = _permute(key64, _PC1, 64)
    c = (k >> 28) & 0x0FFFFFFF
    d = k & 0x0FFFFFFF
    keys = []
    for s in _SHIFTS:
        c = ((c << s) | (c >> (28 - s))) & 0x0FFFFFFF
        d = ((d << s) | (d >> (28 - s))) & 0x0FFFFFFF
        keys.append(_permute((c << 28) | d, _PC2, 56))
    return keys


def _des_feistel(r, k):
    x = _permute(r, _E, 32) ^ k
    out = 0
    for i in range(8):
        six = (x >> (42 - 6 * i)) & 0x3F
        row = ((six & 0x20) >> 4) | (six & 0x01)
        col = (six >> 1) & 0x0F
        out = (out << 4) | _SBOX[i][row * 16 + col]
    return _permute(out, _P, 32)


def des_encrypt_block(key64, block64):
    b = _permute(block64, _IP, 64)
    l, r = (b >> 32) & 0xFFFFFFFF, b & 0xFFFFFFFF
    for k in _des_subkeys(key64):
        l, r = r, l ^ _des_feistel(r, k)
    return _permute((r << 32) | l, _FP, 64)


def vnc_auth_key(password):
    """VNC quirk: password -> 8-byte key, bits of every byte reversed."""
    raw = password.encode('latin-1', 'replace')[:8].ljust(8, b'\x00')
    return bytes(_BITREV[b] for b in raw)


def vnc_encrypt_challenge(password, challenge):
    key = int.from_bytes(vnc_auth_key(password), 'big')
    return b''.join(
        des_encrypt_block(key, int.from_bytes(challenge[i:i + 8], 'big'))
        .to_bytes(8, 'big')
        for i in (0, 8)
    )


# --------------------------------------------------------------------------
# RFB client (network side, runs in its own thread)
# --------------------------------------------------------------------------

class VNCClient:
    def __init__(self):
        self.sock = None
        self._rbuf = b''
        self._wlock = threading.Lock()
        self._stop = threading.Event()
        self.fb_lock = threading.Lock()      # guards self.fb
        self.fb = None                       # PIL Image 'RGB', full framebuffer
        self.width = self.height = 0
        self.name = ''
        self.sec_type = None
        self._minor = 8
        self._zstreams = [zlib.decompressobj() for _ in range(4)]
        self.events = queue.Queue()          # -> UI thread

    # -- low level io ------------------------------------------------------

    def _read(self, n, interruptible=False):
        """Read exactly n bytes.

        With interruptible=True (used only while waiting for the NEXT server
        message) a recv timeout raises TimeoutError so the caller can re-check
        the stop flag; a static desktop can legitimately sit silent for hours.

        Mid-message reads (interruptible=False) treat recv timeouts as mere
        wakeups and keep waiting — a slow server must not kill the session —
        but give up if no byte has arrived for MSG_TIMEOUT seconds.
        """
        deadline = time.monotonic() + MSG_TIMEOUT
        while len(self._rbuf) < n:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                if interruptible:
                    raise TimeoutError
                if self._stop.is_set():
                    raise RFBError('viewer is closing')
                if time.monotonic() > deadline:
                    raise RFBError('server stalled mid-message '
                                   f'for {MSG_TIMEOUT:.0f}s')
                continue
            if not chunk:
                raise RFBError('server closed the connection')
            self._rbuf += chunk
            deadline = time.monotonic() + MSG_TIMEOUT
        out, self._rbuf = self._rbuf[:n], self._rbuf[n:]
        return out

    def _send(self, data):
        with self._wlock:
            if self.sock is None:
                raise OSError('not connected')
            self.sock.sendall(data)

    def _compact_len(self):
        b0 = self._read(1)[0]
        n = b0 & 0x7F
        if b0 & 0x80:
            b1 = self._read(1)[0]
            n |= (b1 & 0x7F) << 7
            if b1 & 0x80:
                n |= self._read(1)[0] << 14
        if n > MAX_RECT_DATA:
            raise RFBError(f'absurd Tight data length: {n}')
        return n

    # -- handshake ----------------------------------------------------------

    def open(self, host, port, timeout=10):
        """Connect + version + security-type negotiation (no auth yet)."""
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # Detect half-open connections (host crash, network drop) instead of
        # waiting forever on a silent socket: probe after 60 s idle, every 10 s.
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        try:
            self.sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 60_000, 10_000))
        except (AttributeError, OSError):
            for opt, val in (('TCP_KEEPIDLE', 60), ('TCP_KEEPINTVL', 10)):
                try:
                    self.sock.setsockopt(socket.IPPROTO_TCP,
                                         getattr(socket, opt), val)
                except (AttributeError, OSError):
                    pass
        banner = self._read(12)
        if not banner.startswith(b'RFB ') or not banner.endswith(b'\n'):
            raise RFBError(f'not an RFB server: {banner!r}')
        self._minor = int(banner[8:11])
        if self._minor >= 8:
            self._minor = 8
        elif self._minor not in (3, 7):
            raise RFBError(f'unsupported RFB version {banner!r}')
        self._send(b'RFB 003.00%d\n' % self._minor)

        if self._minor == 3:
            self.sec_type = struct.unpack('!I', self._read(4))[0]
            if self.sec_type == 0:
                ln = struct.unpack('!I', self._read(4))[0]
                raise RFBError('server refused: ' +
                               self._read(ln).decode('latin-1', 'replace'))
            if self.sec_type not in (SEC_NONE, SEC_VNCAUTH):
                raise RFBError(f'unsupported security type {self.sec_type}')
        else:
            n = self._read(1)[0]
            if n == 0:
                ln = struct.unpack('!I', self._read(4))[0]
                raise RFBError('server refused: ' +
                               self._read(ln).decode('latin-1', 'replace'))
            offered = set(self._read(n))
            if SEC_VNCAUTH in offered:
                self.sec_type = SEC_VNCAUTH
            elif SEC_NONE in offered:
                self.sec_type = SEC_NONE
            else:
                raise RFBError(f'no supported security type in {sorted(offered)}')
            self._send(bytes([self.sec_type]))

    def authenticate(self, password):
        if self.sec_type == SEC_VNCAUTH:
            if password is None:
                raise RFBError('password required')
            challenge = self._read(16)
            self._send(vnc_encrypt_challenge(password, challenge))
        if self._minor == 3 and self.sec_type == SEC_NONE:
            return                                    # 3.3: no result msg
        result = struct.unpack('!I', self._read(4))[0]
        if result != 0:
            reason = 'authentication failed'
            if self._minor >= 8:
                ln = struct.unpack('!I', self._read(4))[0]
                reason = self._read(ln).decode('latin-1', 'replace') or reason
            raise RFBError(reason)

    def init(self):
        self._send(b'\x01')                          # ClientInit: shared
        w, h = struct.unpack('!HH', self._read(4))
        self._read(16)                               # server pixel format
        ln = struct.unpack('!I', self._read(4))[0]
        self.name = self._read(ln).decode('latin-1', 'replace')
        if not (0 < w <= 8192 and 0 < h <= 8192):
            raise RFBError(f'bad desktop size {w}x{h}')
        if (w, h) != (self.width, self.height) or self.fb is None:
            with self.fb_lock:
                self.fb = Image.new('RGB', (w, h))
        # else: reconnect with same geometry -> keep the old frame on screen;
        # the full update we request next repaints every pixel anyway.
        self.width, self.height = w, h

        # 32bpp / depth 24 / little-endian / true-color -> pixels arrive BGRX
        self._send(struct.pack('!BxxxBBBBHHHBBBxxx',
                               0, 32, 24, 0, 1, 255, 255, 255, 16, 8, 0))
        encodings = [ENC_RAW, ENC_COPYRECT, ENC_TIGHT, ENC_DESKTOPSZ,
                     -32 + 8,      # Tight JPEG quality level 8
                     -256 + 6]     # Tight zlib compression level 6
        self._send(struct.pack('!BxH', 2, len(encodings)) +
                   b''.join(struct.pack('!i', e) for e in encodings))
        self.sock.settimeout(SOCK_POLL)              # enables clean shutdown

    # -- client -> server messages -------------------------------------------

    def request_update(self, incremental=True):
        self._send(struct.pack('!BBHHHH', 3, 1 if incremental else 0,
                               0, 0, self.width, self.height))

    def send_key(self, keysym, down):
        self._send(struct.pack('!BBxxI', 4, 1 if down else 0, keysym))

    def send_pointer(self, mask, x, y):
        x = min(max(0, x), self.width - 1)
        y = min(max(0, y), self.height - 1)
        self._send(struct.pack('!BBHH', 5, mask, x, y))

    # -- reader loop -----------------------------------------------------------

    def _reset(self):
        """Drop the current connection state so a fresh session can start."""
        try:
            if self.sock is not None:
                self.sock.close()
        except OSError:
            pass
        self.sock = None
        self._rbuf = b''
        self._zstreams = [zlib.decompressobj() for _ in range(4)]

    def run_supervised(self, host, port, password):
        """Reader-thread entry point: keep a session alive, reconnecting as
        needed.  The FIRST connection must already be established (main() did
        it so it could prompt for a password / report immediate errors).
        Never returns until close() is called; reports state via self.events.
        """
        delay = RECONNECT_MIN
        have_session = True                      # main() connected us already
        while not self._stop.is_set():
            if not have_session:
                try:
                    self._reset()
                    self.open(host, port)
                    self.authenticate(password)
                    self.init()
                except (RFBError, OSError) as exc:
                    self.events.put(('status',
                                     f'reconnect failed: {exc} — '
                                     f'retrying in {delay:.0f}s'))
                    if self._stop.wait(delay):
                        break
                    delay = min(delay * 2, RECONNECT_MAX)
                    continue
                self.events.put(('reconnected',))
            started = time.monotonic()
            try:
                self._read_loop()
            except (RFBError, OSError, zlib.error) as exc:
                if self._stop.is_set():
                    break
                self.events.put(('status',
                                 f'connection lost ({exc}) — reconnecting…'))
            have_session = False
            if time.monotonic() - started > 10:
                delay = RECONNECT_MIN            # session was healthy
            if self._stop.wait(min(delay, 2.0)):
                break
        self._reset()
        self.events.put(('closed',))

    def _read_loop(self):
        """One session: request updates and dispatch server messages until
        close() is called (returns) or the connection breaks (raises)."""
        self.request_update(incremental=False)
        while not self._stop.is_set():
            try:
                mtype = self._read(1, interruptible=True)[0]
            except TimeoutError:
                continue
            if mtype == 0:
                self._framebuffer_update()
                self.request_update(incremental=True)
            elif mtype == 1:
                self._skip_colormap()
            elif mtype == 2:
                self.events.put(('bell',))
            elif mtype == 3:
                self._server_cut_text()
            else:
                raise RFBError(f'unknown server message {mtype}')

    def close(self):
        self._stop.set()
        try:
            if self.sock is not None:
                self.sock.close()
        except OSError:
            pass

    # -- server messages -------------------------------------------------------

    def _skip_colormap(self):
        self._read(1)
        _first, n = struct.unpack('!HH', self._read(4))
        self._read(n * 6)

    def _server_cut_text(self):
        self._read(3)
        ln = struct.unpack('!I', self._read(4))[0]
        text = self._read(ln).decode('latin-1', 'replace')
        self.events.put(('cuttext', text))

    def _framebuffer_update(self):
        self._read(1)
        nrects = struct.unpack('!H', self._read(2))[0]
        for _ in range(nrects):
            x, y, w, h, enc = struct.unpack('!HHHHi', self._read(12))
            if enc == ENC_DESKTOPSZ:
                self._resize(w, h)
                continue
            if enc == ENC_RAW:
                self._raw_rect(x, y, w, h)
            elif enc == ENC_COPYRECT:
                self._copy_rect(x, y, w, h)
            elif enc == ENC_TIGHT:
                self._tight_rect(x, y, w, h)
            else:
                raise RFBError(f'server sent unadvertised encoding {enc}')
            self.events.put(('rect', x, y, w, h))
        self.events.put(('flush',))

    def _resize(self, w, h):
        with self.fb_lock:
            self.width, self.height = w, h
            self.fb = Image.new('RGB', (w, h))
        self.events.put(('resize', w, h))
        self.request_update(incremental=False)

    def _raw_rect(self, x, y, w, h):
        data = self._read(w * h * 4)
        region = Image.frombytes('RGB', (w, h), data, 'raw', 'BGRX')
        with self.fb_lock:
            self.fb.paste(region, (x, y))

    def _copy_rect(self, x, y, w, h):
        sx, sy = struct.unpack('!HH', self._read(4))
        with self.fb_lock:
            self.fb.paste(self.fb.crop((sx, sy, sx + w, sy + h)), (x, y))

    # -- Tight encoding --------------------------------------------------------

    def _tight_rect(self, x, y, w, h):
        ctl = self._read(1)[0]
        for i in range(4):                           # zlib stream reset flags
            if (ctl >> i) & 1:
                self._zstreams[i] = zlib.decompressobj()
        sub = ctl >> 4

        if sub == TIGHT_FILL:                        # solid color
            r, g, b = self._read(3)
            with self.fb_lock:
                self.fb.paste(Image.new('RGB', (w, h), (r, g, b)), (x, y))
            return

        if sub == TIGHT_JPEG:
            data = self._read(self._compact_len())
            region = Image.open(io.BytesIO(data)).convert('RGB')
            with self.fb_lock:
                self.fb.paste(region, (x, y))
            return

        if sub > TIGHT_JPEG:
            raise RFBError(f'bad Tight sub-encoding {sub}')

        # basic compression
        if sub & TIGHT_EXPLICIT_FILTER:
            fid = self._read(1)[0]
        else:
            fid = TIGHT_FILTER_COPY
        stream = self._zstreams[sub & 0x03]

        if fid == TIGHT_FILTER_PALETTE:
            ncolors = self._read(1)[0] + 1
            palette = list(self._read(ncolors * 3))
            raw = stream.decompress(self._read(self._compact_len()))
            region = self._tight_palette(w, h, ncolors, palette, raw)
        elif fid == TIGHT_FILTER_COPY:
            raw = stream.decompress(self._read(self._compact_len()))
            if len(raw) != w * h * 3:
                raise RFBError('short Tight copy data')
            region = Image.frombytes('RGB', (w, h), raw)
        elif fid == TIGHT_FILTER_GRADIENT:
            raw = stream.decompress(self._read(self._compact_len()))
            if len(raw) != w * h * 3:
                raise RFBError('short Tight gradient data')
            region = Image.frombytes('RGB', (w, h),
                                     self._tight_gradient(w, h, raw))
        else:
            raise RFBError(f'unknown Tight filter {fid}')

        with self.fb_lock:
            self.fb.paste(region, (x, y))

    @staticmethod
    def _tight_palette(w, h, ncolors, palette, raw):
        if ncolors == 2:                             # packed bitmap, MSB first
            row_bytes = (w + 7) // 8
            if len(raw) != row_bytes * h:
                raise RFBError('short Tight mono data')
            idx = bytearray(w * h)
            for yy in range(h):
                base, obase = yy * row_bytes, yy * w
                for xx in range(w):
                    if raw[base + (xx >> 3)] & (0x80 >> (xx & 7)):
                        idx[obase + xx] = 1
        else:
            if len(raw) != w * h:
                raise RFBError('short Tight indexed data')
            idx = raw
        palette = (palette + [0] * 768)[:768]
        img = Image.frombytes('P', (w, h), bytes(idx))
        img.putpalette(palette)
        return img.convert('RGB')

    @staticmethod
    def _tight_gradient(w, h, raw):
        """Undo the gradient (predictive) filter: est = left+above-upleft."""
        out = bytearray(len(raw))
        stride = w * 3
        for yy in range(h):
            row, up = yy * stride, (yy - 1) * stride
            for xx in range(w):
                i = row + xx * 3
                for c in range(3):
                    p = i + c
                    left = out[p - 3] if xx else 0
                    above = out[up + xx * 3 + c] if yy else 0
                    upleft = out[up + xx * 3 + c - 3] if (xx and yy) else 0
                    est = left + above - upleft
                    if est < 0:
                        est = 0
                    elif est > 255:
                        est = 255
                    out[p] = (raw[p] + est) & 0xFF
        return bytes(out)


# --------------------------------------------------------------------------
# tkinter UI (main thread)
# --------------------------------------------------------------------------

KEYSYMS = {
    'BackSpace': 0xFF08, 'Tab': 0xFF09, 'Return': 0xFF0D, 'Escape': 0xFF1B,
    'Insert': 0xFF63, 'Delete': 0xFFFF, 'Home': 0xFF50, 'End': 0xFF57,
    'Prior': 0xFF55, 'Next': 0xFF56,                      # PgUp / PgDn
    'Left': 0xFF51, 'Up': 0xFF52, 'Right': 0xFF53, 'Down': 0xFF54,
    'Shift_L': 0xFFE1, 'Shift_R': 0xFFE2,
    'Control_L': 0xFFE3, 'Control_R': 0xFFE4,
    'Alt_L': 0xFFE9, 'Alt_R': 0xFFEA,
    'Meta_L': 0xFFE7, 'Meta_R': 0xFFE8,
    'Super_L': 0xFFEB, 'Super_R': 0xFFEC,
    'Win_L': 0xFFEB, 'Win_R': 0xFFEC,
    'Caps_Lock': 0xFFE5, 'Num_Lock': 0xFF7F, 'Scroll_Lock': 0xFF14,
    'Pause': 0xFF13, 'Print': 0xFF61, 'Menu': 0xFF67,
    'KP_Enter': 0xFF8D, 'KP_Add': 0xFFAB, 'KP_Subtract': 0xFFAD,
    'KP_Multiply': 0xFFAA, 'KP_Divide': 0xFFAF, 'KP_Decimal': 0xFFAE,
    'KP_Space': 0x20,
}
KEYSYMS.update({f'F{i}': 0xFFBD + i for i in range(1, 13)})
KEYSYMS.update({f'KP_{i}': 0xFFB0 + i for i in range(10)})


def keysym_for(event):
    ks = event.keysym
    if len(ks) == 1:
        return ord(ks)
    return KEYSYMS.get(ks)


class ViewerApp:
    RESCALE_INTERVAL = 1 / 25                    # cap full rescales at ~25 fps

    def __init__(self, root, client, scale='fit'):
        import tkinter as tk
        self.tk = tk
        self.root = root
        self.client = client
        self.photo = None
        self.mouse_mask = 0
        self._wheel_remainder = 0
        self.scale_mode = scale                  # 'fit' or a float factor
        self.scale = 1.0
        self.fullscreen = False
        self._dirty = False
        self._last_rescale = 0.0

        self.label = tk.Label(root, bd=0, highlightthickness=0, bg='black')
        self.label.pack(fill='both', expand=True)
        self._calc_scale()
        self._set_title()
        self._refresh()
        root.geometry(f'{self.dw}x{self.dh}')
        root.resizable(False, False)

        root.bind('<KeyPress>', lambda e: self._key(e, True))
        root.bind('<KeyRelease>', lambda e: self._key(e, False))
        self.label.bind('<Motion>', self._motion)
        for b in (1, 2, 3):
            self.label.bind(f'<ButtonPress-{b}>', lambda e, b=b: self._button(e, b, True))
            self.label.bind(f'<ButtonRelease-{b}>', lambda e, b=b: self._button(e, b, False))
        self.label.bind('<MouseWheel>', self._wheel)       # Windows
        self.label.bind('<Button-4>', lambda e: self._wheel_notch(1))   # X11
        self.label.bind('<Button-5>', lambda e: self._wheel_notch(-1))
        root.protocol('WM_DELETE_WINDOW', self._quit)

        root.after(5, self._poll)
        root.focus_set()

    # -- rendering ---------------------------------------------------------

    def _calc_scale(self):
        c = self.client
        if self.fullscreen:
            avail_w = self.root.winfo_screenwidth()
            avail_h = self.root.winfo_screenheight()
            self.scale = min(avail_w / c.width, avail_h / c.height)
        elif self.scale_mode == 'fit':
            avail_w = self.root.winfo_screenwidth() - 60
            avail_h = self.root.winfo_screenheight() - 120
            self.scale = min(avail_w / c.width, avail_h / c.height, 1.0)
        else:
            self.scale = float(self.scale_mode)
        self.dw = max(1, round(c.width * self.scale))
        self.dh = max(1, round(c.height * self.scale))

    def _set_title(self):
        title = f'{self.client.name} - pyvnc'
        if self.scale != 1.0:
            title += f'  ({round(self.scale * 100)}%)'
        self.root.title(title)

    def _refresh(self):
        """Re-render the (possibly scaled) framebuffer into the window."""
        from PIL import ImageTk
        with self.client.fb_lock:
            fb = self.client.fb
            disp = fb if self.scale == 1.0 else fb.resize(
                (self.dw, self.dh), Image.Resampling.BILINEAR)
        self.photo = ImageTk.PhotoImage(disp)
        self.label.configure(image=self.photo)
        self._dirty = False
        self._last_rescale = time.monotonic()

    def _blit_rect(self, x, y, w, h):
        from PIL import ImageTk
        with self.client.fb_lock:
            region = ImageTk.PhotoImage(self.client.fb.crop((x, y, x + w, y + h)))
        # Tk photo 'copy' replaces the pixels in place - fast partial update
        self.photo.tk.call(str(self.photo), 'copy', str(region),
                           '-from', 0, 0, w, h, '-to', x, y)
        del region

    def _poll(self):
        c = self.client
        try:
            while True:
                ev = c.events.get_nowait()
                kind = ev[0]
                if kind == 'rect':
                    if self.scale == 1.0:
                        self._blit_rect(*ev[1:5])
                    else:
                        self._dirty = True          # rescale throttled below
                elif kind == 'flush':
                    pass
                elif kind == 'resize':
                    self._calc_scale()
                    self._set_title()
                    self._refresh()
                    self.root.geometry(f'{self.dw}x{self.dh}')
                elif kind == 'cuttext':
                    self.root.clipboard_clear()
                    self.root.clipboard_append(ev[1])
                elif kind == 'bell':
                    self.root.bell()
                elif kind == 'status':
                    # (re)connection trouble: keep the last frame on screen,
                    # show what's happening in the title bar
                    self.root.title(f'pyvnc — {ev[1]}')
                elif kind == 'reconnected':
                    self._calc_scale()           # geometry may have changed
                    self._set_title()
                    self._refresh()
                    self.root.geometry(f'{self.dw}x{self.dh}')
                elif kind == 'closed':
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        except Exception:
            # a blit raced with a resize; rebuild the whole image
            try:
                self._refresh()
            except Exception:
                pass
        if self._dirty and self.scale != 1.0:
            if time.monotonic() - self._last_rescale >= self.RESCALE_INTERVAL:
                try:
                    self._refresh()
                except Exception:
                    pass
        self.root.after(5, self._poll)

    # -- input -------------------------------------------------------------

    def _key(self, event, down):
        if event.keysym == 'F11':                # local fullscreen toggle,
            if down:                             # not forwarded to the remote
                self._toggle_fullscreen()
            return
        ks = keysym_for(event)
        if ks is not None:
            try:
                self.client.send_key(ks, down)
            except OSError:
                pass

    def _toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        self.root.attributes('-fullscreen', self.fullscreen)
        self._calc_scale()
        self._set_title()
        self._refresh()
        if not self.fullscreen:
            self.root.geometry(f'{self.dw}x{self.dh}')

    def _label_to_remote(self, lx, ly):
        """Label widget coords -> remote framebuffer coords (letterbox aware)."""
        ox = max(0, (self.label.winfo_width() - self.dw) // 2)
        oy = max(0, (self.label.winfo_height() - self.dh) // 2)
        return int((lx - ox) / self.scale), int((ly - oy) / self.scale)

    def _motion(self, event):
        x, y = self._label_to_remote(event.x, event.y)
        try:
            self.client.send_pointer(self.mouse_mask, x, y)
        except OSError:
            pass

    def _button(self, event, button, down):
        bit = 1 << (button - 1)
        self.mouse_mask = (self.mouse_mask | bit) if down else (self.mouse_mask & ~bit)
        self._motion(event)

    def _wheel(self, event):
        self._wheel_remainder += event.delta
        while self._wheel_remainder >= 120:
            self._wheel_notch(1)
            self._wheel_remainder -= 120
        while self._wheel_remainder <= -120:
            self._wheel_notch(-1)
            self._wheel_remainder += 120

    def _wheel_notch(self, direction):
        bit = 0x08 if direction > 0 else 0x10       # wheel up / wheel down
        x, y = self.root.winfo_pointerxy()
        x -= self.label.winfo_rootx()
        y -= self.label.winfo_rooty()
        x, y = self._label_to_remote(x, y)
        try:
            self.client.send_pointer(self.mouse_mask | bit, x, y)
            self.client.send_pointer(self.mouse_mask, x, y)
        except OSError:
            pass

    def _quit(self):
        self.client.close()
        self.root.destroy()


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def selftest():
    # FIPS-81 DES known-answer test
    key = int('0123456789ABCDEF', 16)
    pt = int('4E6F772069732074', 16)          # "Now is t"
    ct = des_encrypt_block(key, pt)
    assert ct == int('3FA40E8A984D4815', 16), hex(ct)
    # bit reversal sanity
    assert _BITREV[0x80] == 0x01 and _BITREV[0x01] == 0x80
    assert _BITREV[0x3C] == 0x3C
    # VNC challenge encryption is 2-block ECB of the same key
    out = vnc_encrypt_challenge('password', bytes(range(16)))
    assert len(out) == 16
    print('selftest OK (DES known-answer + VNC auth helpers)')


def _make_dpi_aware():
    """Render in physical pixels (crisp scaling) instead of letting Windows
    bitmap-stretch the window on high-DPI displays."""
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def main():
    _make_dpi_aware()
    ap = argparse.ArgumentParser(description='Tight VNC viewer (RFB 3.8)')
    ap.add_argument('host', nargs='?', default='192.168.1.176')
    ap.add_argument('-p', '--password', default=None)
    ap.add_argument('--port', type=int, default=5900)
    ap.add_argument('--scale', default='fit', metavar="FACTOR|'fit'",
                    help="display scale factor (e.g. 0.5), or 'fit' to shrink "
                         "the remote desktop to your screen (default)")
    ap.add_argument('--selftest', action='store_true')
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return

    scale = args.scale
    if scale != 'fit':
        try:
            scale = float(scale)
            if not (0.05 <= scale <= 4.0):
                raise ValueError
        except ValueError:
            ap.error(f'--scale must be a number (e.g. 0.5) or "fit", '
                     f'got {args.scale!r}')

    import tkinter as tk
    from tkinter import messagebox, simpledialog

    root = tk.Tk()
    root.withdraw()

    client = VNCClient()
    try:
        client.open(args.host, args.port)
        password = args.password
        if client.sec_type == SEC_VNCAUTH and password is None:
            password = simpledialog.askstring(
                'VNC password',
                f'Password for {args.host}:{args.port}', show='*')
            if password is None:
                return
        client.authenticate(password)
        client.init()
    except (RFBError, OSError) as exc:
        messagebox.showerror('pyvnc', f'Cannot connect to '
                                    f'{args.host}:{args.port}\n\n{exc}')
        return

    app = ViewerApp(root, client, scale)
    root.deiconify()

    reader = threading.Thread(target=client.run_supervised,
                              args=(args.host, args.port, password),
                              daemon=True)
    reader.start()

    try:
        root.mainloop()
    finally:
        client.close()


if __name__ == '__main__':
    main()
