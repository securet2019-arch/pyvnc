// pyvnc Web Viewer — options page logic.
// One form control per DEFAULTS key (element id === key), autosave on input.

import { DEFAULTS, loadSettings } from './defaults.js';

const FIELDS = Object.keys(DEFAULTS);
const form = document.getElementById('form');
const savedEl = document.getElementById('saved');
const cmdEl = document.getElementById('bridge-cmd');

function fieldEl(k) { return document.getElementById(k); }

function setField(k, v) {
  const el = fieldEl(k);
  if (el.type === 'checkbox') el.checked = !!v;
  else el.value = v;
}

function readField(k) {
  const el = fieldEl(k);
  if (el.type === 'checkbox') return el.checked;
  if (el.type === 'number') return el.value === '' ? NaN : Number(el.value);
  return el.value;
}

function updateCmd(port) {
  cmdEl.textContent = Number.isInteger(port)
    ? `python -m websockify ${port} 192.168.1.176:5900`
    : 'python -m websockify <port> 192.168.1.176:5900';
}

function validate(s) {
  const intIn = (v, lo, hi) => Number.isInteger(v) && v >= lo && v <= hi;
  return s.wsHost !== '' &&
         intIn(s.wsPort, 1, 65535) &&
         intIn(s.quality, 0, 9) &&
         intIn(s.compression, 0, 9);
}

async function load() {
  const s = await loadSettings();
  FIELDS.forEach(k => setField(k, s[k]));
  updateCmd(s.wsPort);
}

let debounce = null;
let flashTimer = null;

form.addEventListener('input', () => {
  clearTimeout(debounce);
  debounce = setTimeout(save, 300);
});

async function save() {
  const s = {};
  for (const k of FIELDS) s[k] = readField(k);
  s.wsHost = s.wsHost.trim();
  s.wsPath = s.wsPath.trim().replace(/^\/+|\/+$/g, '') || 'websockify';
  updateCmd(s.wsPort);
  if (!validate(s)) {
    savedEl.textContent = 'invalid values — not saved';
    savedEl.className = 'err';
    return;
  }
  await chrome.storage.local.set({ settings: s });
  savedEl.textContent = 'Saved';
  savedEl.className = 'ok';
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => { savedEl.textContent = ''; }, 1500);
}

document.getElementById('btn-open').addEventListener('click', () => {
  chrome.tabs.create({ url: chrome.runtime.getURL('viewer.html') });
});

load();
