# pyvnc Web Viewer (Chrome extension)

A Chrome (Manifest V3) port of the pyvnc viewer: watch the headless WSJT-X
machine from a browser tab. Uses the vendored **noVNC core** library
(`lib/novnc/`) for the RFB protocol — including Tight encoding and DES auth —
plus a small **websockify** bridge, because browser pages cannot open raw TCP
sockets and TightVNC has no WebSocket support:

```
Chrome tab (noVNC) ── ws://localhost:6080 ── websockify ── TCP ── 192.168.1.176:5900 (TightVNC)
```

No build step, no npm: the extension is plain HTML/JS modules and runs
unpacked straight from this directory.

## Install

1. `chrome://extensions` → enable **Developer mode** → **Load unpacked** →
   select this `extension/` directory.
2. The options page opens on first install (or later via the toolbar button →
   Settings, or the extension's *Details → Extension options*).
3. Start the bridge (see below), then click the extension's toolbar icon to
   open the viewer tab. Clicking it again focuses the existing tab.

## The bridge (required)

On this PC:

```console
pip install websockify
python -m websockify 6080 192.168.1.176:5900
```

or just double-click `run_bridge.bat`. Leave the console open; the extension's
default settings (`localhost:6080`) point at it. To autostart at logon, create
a scheduled task, e.g.:

```console
schtasks /create /tn "pyvnc-bridge" /tr "\"C:\Users\File\pyvnc\extension\run_bridge.bat\"" /sc onlogon
```

Note: the extension's host/port always point at **websockify**, not at the VNC
server — the VNC target is baked into the bridge command. To run the bridge on
the WSJT-X machine instead (Python is there), use
`python -m websockify 6080 localhost:5900` on it and set Bridge host to
`192.168.1.176` in the options.

## Options

| Option | Default | Meaning |
|---|---|---|
| Bridge host | `localhost` | where websockify listens |
| Bridge port | `6080` | websockify port |
| WebSocket path | `websockify` | plain websockify ignores it; kept for proxy compatibility |
| VNC password | *(empty)* | stored in plaintext in the browser profile; empty = in-page prompt only if the server asks |
| Scaling | `Fit to tab` | or native size with scrollbars |
| JPEG quality | `8` | noVNC `qualityLevel` 0–9 (vncviewer.py negotiates 8) |
| Compression | `6` | noVNC `compressionLevel` 0–9 (vncviewer.py negotiates 6) |
| View only | off | ignore local input |
| Auto-reconnect | on | backoff 1→30 s, reset after a healthy (>10 s) session — same policy as `vncviewer.py` |
| Shared session | on | RFB ClientInit shared flag |
| Bell beep | on | WebAudio beep on remote bell |

Changes save automatically; quality/compression/scaling/view-only apply live
to an open session, and changing host/port/path/password reconnects
immediately.

## Controls (viewer tab)

| Input | Action |
|---|---|
| toolbar **Fullscreen** | `requestFullscreen()` on the screen area (fit-scaling refits) |
| toolbar **Ctrl+Alt+Del** | sent to the remote (the real combo can't be forwarded) |
| toolbar **Paste to remote** | local clipboard → remote (needs clipboard access) |
| toolbar **Reconnect / Disconnect** | manual control; Disconnect stops auto-reconnect |
| keyboard / mouse / wheel | forwarded while the screen has focus |
| copy on the remote | arrives in the local clipboard |

Browser-reserved shortcuts (Ctrl+W/T/N, Alt+Tab, the Win key, F11) never reach
the remote — use the toolbar buttons. While reconnecting, the last frame stays
on screen under a semi-transparent status overlay, mirroring the Python
viewer's title-bar status.

## Troubleshooting

- **"reconnect failed" right away, bridge running:** Chrome is rolling out a
  *Local Network Access* permission gate. WebSockets are not yet gated, but if
  a future Chrome blocks `ws://localhost` from extension pages: try
  `127.0.0.1` as Bridge host, check the page's site settings for a
  local-network-access toggle, or run websockify with TLS (`--cert`) and use
  `wss://` (adjust `wsPath`/URL accordingly).
- **Black screen, status "connected":** check the bridge console — is the VNC
  target reachable (`192.168.1.176:5900`)? Compare with `python vncviewer.py`.
- **Authentication failed loop:** the viewer stops auto-reconnecting after a
  security failure; fix the password in Settings (or leave it empty for the
  prompt).

## Testing without the radio machine

`test/fake_server.py` reuses the fake RFB server from `../test_vncviewer.py`:

```console
python extension/test/fake_server.py        # prints the port it bound
python -m websockify 6081 127.0.0.1:<port>  # second console
```

Set Bridge port = `6081` in the options → the viewer tab should show a solid
green screen named "fake". Ctrl+C the fake server to watch the reconnect
backoff (1, 2, 4, 8… s, last frame kept); restart it to see recovery.

## Re-vendoring noVNC

`lib/novnc/core/` and `lib/novnc/vendor/` are `@novnc/novnc@1.7.0` verbatim
(MPL-2.0, see `lib/novnc/LICENSE.txt`; `vendor/pako` is noVNC's bundled zlib,
imported by `core/inflator.js`/`deflator.js` as `../vendor/...`). To upgrade,
on any Windows 10+ box:

```bat
cd extension
curl -L -o novnc.tgz https://registry.npmjs.org/@novnc/novnc/-/novnc-<version>.tgz
tar xzf novnc.tgz
rmdir /S /Q lib\novnc\core lib\novnc\vendor
xcopy /E /I package\core lib\novnc\core
xcopy /E /I package\vendor lib\novnc\vendor
copy /Y package\LICENSE.txt lib\novnc\
rmdir /S /Q package & del novnc.tgz
```

## Deltas vs. the Python viewer

- Fit-scaling also *upscales* small desktops (noVNC behavior); the Python
  `fit` only shrinks.
- No mid-message stall watchdog (no public hook in noVNC). Dropped/dead
  connections still auto-reconnect; a hard-stalled session needs the Reconnect
  button.
- Client→server clipboard exists here as the "Paste to remote" button; the
  Python viewer is server→client only.

## Files

| File | Purpose |
|---|---|
| `manifest.json` | MV3 manifest (`storage`, `clipboardWrite`; nothing else) |
| `background.js` | service worker: toolbar icon opens/focuses the viewer tab |
| `defaults.js` | settings schema + defaults, shared by viewer and options |
| `viewer.html/css/js` | the VNC session tab (noVNC wiring, reconnect logic) |
| `options.html/js` | settings page (autosave, validation) |
| `lib/novnc/` | vendored noVNC core + license |
| `run_bridge.bat` | one-click websockify bridge |
| `test/fake_server.py` | loopback fake RFB server for manual testing |
