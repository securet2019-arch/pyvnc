// pyvnc Web Viewer — service worker.
// Only opens/focuses the viewer tab; the RFB/WebSocket session lives in the
// tab page, never here (service workers are killed when idle).

const VIEWER = chrome.runtime.getURL('viewer.html');

chrome.action.onClicked.addListener(async () => {
  const [tab] = await chrome.tabs.query({ url: VIEWER });
  if (tab) {
    chrome.tabs.update(tab.id, { active: true });
    chrome.windows.update(tab.windowId, { focused: true });
  } else {
    chrome.tabs.create({ url: VIEWER });
  }
});

chrome.runtime.onInstalled.addListener(({ reason }) => {
  if (reason === 'install') chrome.runtime.openOptionsPage();
});
