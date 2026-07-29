// pyvnc Web Viewer — session lifecycle for the viewer tab.
//
// Behavior mirrors vncviewer.py:
//  - auto-reconnect with exponential backoff 1s -> 30s (run_supervised port)
//  - backoff resets to 1s when the just-ended session lasted > 10s
//  - the last frame stays on screen while reconnecting (snapshot canvas)
//  - in-page password prompt when the server asks and none is stored
//  - server -> local clipboard sync, bell beep, fit-to-window scaling

import RFB from './lib/novnc/core/rfb.js';
import { DEFAULTS, loadSettings } from './defaults.js';

// vncviewer.py: RECONNECT_MIN / RECONNECT_MAX / healthy-session threshold
const RECONNECT_MIN = 1, RECONNECT_MAX = 30, HEALTHY_S = 10;
// vncviewer.py: open(timeout=10) — noVNC has no connect timeout of its own
const CONNECT_TIMEOUT_S = 10;

const $ = id => document.getElementById(id);
const wrapEl = $('wrap'), screenEl = $('screen'), snapEl = $('snapshot');
const overlayEl = $('overlay'), overlayText = $('overlay-text');
const statusEl = $('status'), dotEl = $('dot'), nameEl = $('name');
const promptForm = $('prompt'), promptPw = $('prompt-pw');
const toastEl = $('toast'), stashEl = $('clipstash');

let settings;
let rfb = null;
let delay = RECONNECT_MIN;   // seconds; doubles on failed connect attempts
let connectedAt = 0;         // ms timestamp of the live session's connect (0 = none)
let timer = null;            // pending reconnect timeout
let connectWatchdog = null;  // fires if 'connect' doesn't arrive in time
let manualStop = false;      // user pressed Disconnect (or cancelled the password prompt)
let authFailed = false;      // server rejected credentials — don't retry in a loop
let desktopName = '';

/* ---------- small UI helpers ---------- */

function setStatus(text, cls) {
  statusEl.textContent = text;
  dotEl.className = cls || '';
}

function showOverlay(text) { overlayText.textContent = text; overlayEl.hidden = false; }
function hideOverlay() { overlayEl.hidden = true; }
function hideSnapshot() { snapEl.hidden = true; }

let toastTimer = null;
function toast(text, ms = 2500) {
  toastEl.textContent = text;
  toastEl.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toastEl.hidden = true; }, ms);
}

function updateTitle(extra) {
  document.title = desktopName ? `pyvnc — ${desktopName}` : (extra || 'pyvnc');
}

function updateDisconnectButton() {
  $('btn-disconnect').textContent = manualStop ? 'Connect' : 'Disconnect';
}

/* ---------- connection ---------- */

const wsUrl = () => {
  const path = settings.wsPath.replace(/^\/+|\/+$/g, '');
  return `ws://${settings.wsHost}:${settings.wsPort}/${path}`;
};

function applyTunables(r) {
  wrapEl.className = settings.scale;           // 'fit' | 'native'
  if (!r) return;
  r.scaleViewport = settings.scale === 'fit';
  r.clipViewport = false;
  r.qualityLevel = settings.quality;
  r.compressionLevel = settings.compression;
  r.viewOnly = settings.viewOnly;
  r.focusOnClick = true;
}

function connect() {
  clearTimeout(timer); timer = null;
  authFailed = false;
  teardown(true);                              // snapshot old frame first
  setStatus('connecting…', 'warn');
  showOverlay('Connecting…');

  const opts = { shared: settings.shared };
  if (settings.password) opts.credentials = { password: settings.password };

  try {
    rfb = new RFB(screenEl, wsUrl(), opts);
  } catch (err) {                              // malformed URL etc.
    rfb = null;
    scheduleRetry(err);
    return;
  }
  applyTunables(rfb);

  // Guard every handler with identity so events from a torn-down instance
  // can't double-schedule reconnects.
  const cur = rfb;
  const on = (ev, fn) => cur.addEventListener(ev, e => { if (rfb === cur) fn(e); });

  on('connect', onConnect);
  on('disconnect', onDisconnect);
  on('credentialsrequired', onCreds);
  on('clipboard', onClipboard);
  on('bell', beep);
  on('desktopname', e => {
    desktopName = e.detail.name;
    nameEl.textContent = desktopName;
    updateTitle();
  });
  on('securityfailure', e => {
    authFailed = true;
    setStatus('authentication failed', 'bad');
    showOverlay('Authentication failed — check the password in Settings.');
  });

  // a stalled WebSocket (blocked, filtered, black-holed) must not hang here
  // forever — fail the attempt so the normal backoff cycle kicks in
  clearTimeout(connectWatchdog);
  connectWatchdog = setTimeout(() => {
    if (connectedAt) return;
    teardown(false);
    scheduleRetry(new Error('no answer from the bridge within ' +
                            CONNECT_TIMEOUT_S + 's'));
  }, CONNECT_TIMEOUT_S * 1000);
}

function onConnect() {
  clearTimeout(connectWatchdog);
  connectedAt = Date.now();
  manualStop = false;
  hideOverlay();
  hideSnapshot();
  setStatus('connected', 'ok');
  updateTitle();
  updateDisconnectButton();
  rfb.focus();
}

function onDisconnect() {
  clearTimeout(connectWatchdog);
  teardown(true);                              // keep the last frame visible
  if (manualStop || !settings.reconnect) {
    setStatus('disconnected', 'bad');
    showOverlay('Disconnected');
    updateTitle('pyvnc (disconnected)');
    updateDisconnectButton();
    return;
  }
  if (authFailed) {
    setStatus('authentication failed', 'bad');
    updateTitle('pyvnc (auth failed)');
    return;                                    // overlay already shown by securityfailure
  }
  scheduleRetry();
}

// Exact port of vncviewer.py run_supervised()'s backoff policy.
function scheduleRetry(err) {
  if (manualStop || !settings.reconnect) {
    setStatus('disconnected', 'bad');
    showOverlay(err ? `connect error: ${err.message || err}` : 'Disconnected');
    updateDisconnectButton();
    return;
  }
  let wait, msg;
  if (connectedAt) {
    // a live session dropped: fast retry; a long-healthy session resets backoff
    if ((Date.now() - connectedAt) / 1000 > HEALTHY_S) delay = RECONNECT_MIN;
    wait = Math.min(delay, 2);
    msg = `connection lost — reconnecting in ${wait}s`;
  } else {
    // a connect attempt failed: back off 1, 2, 4, 8, … up to 30s
    wait = delay;
    msg = `reconnect failed${err ? ` (${err.message || err})` : ''} — retrying in ${wait}s`;
    delay = Math.min(delay * 2, RECONNECT_MAX);
  }
  connectedAt = 0;
  setStatus(`reconnecting in ${wait}s`, 'warn');
  showOverlay(msg);
  updateTitle('pyvnc (reconnecting…)');
  timer = setTimeout(connect, wait * 1000);
}

function teardown(keepFrame) {
  if (keepFrame) snapshot();
  const old = rfb;
  rfb = null;                                  // null first: guards ignore its events
  if (old) { try { old.disconnect(); } catch { /* already down */ } }
  screenEl.replaceChildren();
}

// Copy noVNC's canvas into an overlay canvas placed exactly over it, so the
// last frame survives the RFB teardown regardless of constructor internals.
function snapshot() {
  const src = screenEl.querySelector('canvas');
  if (!src) return;
  const wr = wrapEl.getBoundingClientRect();
  const sr = src.getBoundingClientRect();
  if (!sr.width || !sr.height) return;
  snapEl.width = src.width;
  snapEl.height = src.height;
  snapEl.style.left = (sr.left - wr.left + wrapEl.scrollLeft) + 'px';
  snapEl.style.top = (sr.top - wr.top + wrapEl.scrollTop) + 'px';
  snapEl.style.width = sr.width + 'px';
  snapEl.style.height = sr.height + 'px';
  snapEl.getContext('2d').drawImage(src, 0, 0);
  snapEl.hidden = false;
}

/* ---------- auth ---------- */

function onCreds() {
  if (settings.password) {
    rfb.sendCredentials({ password: settings.password });
    return;
  }
  // mirror the python viewer: prompt when the server asks and none is stored
  promptForm.hidden = false;
  promptPw.value = '';
  promptPw.focus();
}

promptForm.addEventListener('submit', e => {
  e.preventDefault();
  promptForm.hidden = true;
  if (rfb) rfb.sendCredentials({ password: promptPw.value });
});

$('prompt-cancel').addEventListener('click', () => {
  promptForm.hidden = true;
  manualStop = true;
  clearTimeout(timer); timer = null;
  clearTimeout(connectWatchdog);
  teardown(false);
  setStatus('disconnected', 'bad');
  showOverlay('Disconnected');
  updateDisconnectButton();
});

/* ---------- clipboard (server -> local; python viewer direction) ---------- */

async function onClipboard(e) {
  const text = e.detail.text;
  try {
    await navigator.clipboard.writeText(text);
    toast('Remote clipboard copied');
  } catch {
    // fallback: select the text in a hidden textarea so Ctrl+C grabs it
    stashEl.value = text;
    stashEl.focus();
    stashEl.select();
    toast('Remote clipboard received — press Ctrl+C to copy', 4000);
  }
}

/* ---------- bell (python: root.bell()) ---------- */

let audioCtx = null;
function unlockAudio() {
  if (!audioCtx) {
    try { audioCtx = new AudioContext(); } catch { return; }
  }
  if (audioCtx.state === 'suspended') audioCtx.resume();
}
addEventListener('pointerdown', unlockAudio, { once: true });
addEventListener('keydown', unlockAudio, { once: true });

function beep() {
  if (!settings.bell) return;
  unlockAudio();
  if (!audioCtx || audioCtx.state !== 'running') return;
  const t = audioCtx.currentTime;
  const o = audioCtx.createOscillator();
  const g = audioCtx.createGain();
  o.frequency.value = 880;
  g.gain.setValueAtTime(0.15, t);
  g.gain.exponentialRampToValueAtTime(0.001, t + 0.15);
  o.connect(g).connect(audioCtx.destination);
  o.start(t);
  o.stop(t + 0.15);
}

/* ---------- toolbar ---------- */

$('btn-cad').addEventListener('click', () => { if (rfb) rfb.sendCtrlAltDel(); });

$('btn-clip').addEventListener('click', async () => {
  if (!rfb) return;
  try {
    const text = await navigator.clipboard.readText();
    if (text) { rfb.clipboardPasteFrom(text); toast('Pasted to remote'); }
  } catch {
    toast('Chrome blocked clipboard read');
  }
});

$('btn-fs').addEventListener('click', () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else wrapEl.requestFullscreen();
});
document.addEventListener('fullscreenchange', () => {
  $('btn-fs').textContent = document.fullscreenElement ? 'Exit fullscreen' : 'Fullscreen';
});

$('btn-reconnect').addEventListener('click', () => {
  manualStop = false;
  delay = RECONNECT_MIN;
  updateDisconnectButton();
  connect();
});

$('btn-disconnect').addEventListener('click', () => {
  if (manualStop) {                            // currently stopped -> Connect
    manualStop = false;
    delay = RECONNECT_MIN;
    connect();
  } else {
    manualStop = true;
    clearTimeout(timer); timer = null;
    clearTimeout(connectWatchdog);
    teardown(false);
    setStatus('disconnected', 'bad');
    showOverlay('Disconnected');
    updateTitle('pyvnc (disconnected)');
  }
  updateDisconnectButton();
});

$('btn-opts').addEventListener('click', () => chrome.runtime.openOptionsPage());

/* ---------- live settings ---------- */

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== 'local' || !changes.settings) return;
  const old = settings;
  settings = { ...DEFAULTS, ...changes.settings.newValue };
  applyTunables(rfb);                          // scale/quality/compression/viewOnly live
  if (['wsHost', 'wsPort', 'wsPath', 'password'].some(k => old[k] !== settings[k])) {
    delay = RECONNECT_MIN;                     // endpoint changed -> reconnect now
    manualStop = false;
    updateDisconnectButton();
    connect();
  }
});

/* ---------- startup / shutdown ---------- */

addEventListener('beforeunload', () => teardown(false));

(async () => {
  settings = await loadSettings();
  applyTunables(null);                         // set wrap class before first connect
  connect();
})();
