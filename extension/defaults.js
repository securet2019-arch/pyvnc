// Shared settings schema. Stored as one object under key 'settings' in
// chrome.storage.local. Every read merges over these defaults so fields added
// later get sensible values automatically.

export const DEFAULTS = {
  wsHost: 'localhost',   // websockify listen host (the bridge runs on THIS pc)
  wsPort: 6080,          // websockify listen port
  wsPath: 'websockify',  // plain websockify ignores the path; noVNC convention
  password: '',          // empty = prompt in-page if the server asks
  scale: 'fit',          // 'fit' (scaleViewport) | 'native' (1:1, scrollbars)
  quality: 8,            // noVNC qualityLevel 0-9    (vncviewer.py: -32+8)
  compression: 6,        // noVNC compressionLevel 0-9 (vncviewer.py: -256+6)
  viewOnly: false,
  reconnect: true,       // auto-reconnect with backoff
  shared: true,          // RFB ClientInit shared flag (vncviewer.py sends \x01)
  bell: true
};

export async function loadSettings() {
  const stored = (await chrome.storage.local.get('settings')).settings;
  return { ...DEFAULTS, ...stored };
}
