// pyvnc Web Viewer — boot diagnostics (classic script, runs before the
// ES module). Surfaces fatal errors in the overlay instead of leaving the
// page stuck at "starting…" with no explanation.
(function () {
  function show(msg) {
    var ov = document.getElementById('overlay');
    var tx = document.getElementById('overlay-text');
    var st = document.getElementById('status');
    var dot = document.getElementById('dot');
    if (!ov || !tx) return;
    tx.textContent = 'Error: ' + msg;
    ov.hidden = false;
    if (st) st.textContent = 'error';
    if (dot) dot.className = 'bad';
  }

  // Module-resolution / script-load failures arrive as error events on the
  // <script> element (captured, they don't bubble); JS errors have .message.
  window.addEventListener('error', function (e) {
    if (e && e.target && e.target !== window && e.target.src) {
      show('failed to load ' + e.target.src);
    } else if (e && e.message) {
      show(e.message +
        (e.filename ? ' @ ' + e.filename.split('/').pop() + ':' + e.lineno : ''));
    }
  }, true);

  window.addEventListener('unhandledrejection', function (e) {
    show(String((e.reason && (e.reason.message || e.reason)) ||
                'unhandled promise rejection'));
  });
})();
