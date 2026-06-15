// Progressive-enhancement layer for the vouch review console.
//
// Nothing here is required: every action is a plain <form method=post>, so the
// review gate works with JavaScript disabled. This script adds two things on
// top when JS is available:
//
//   1. a single WebSocket to /ws that flips the "live" pill on and reloads the
//      queue/audit view within ~1s when another reviewer acts (issue #194's
//      "two windows stay in sync" criterion);
//   2. keyboard shortcuts (j/k to move, a to approve, r to focus reject, ? for
//      help) so a reviewer can clear a queue without touching the mouse.
//
// There is deliberately NO token handling here. When the server runs with
// --auth, the credential lives in an HttpOnly cookie the browser sets during
// the bootstrap redirect; JS can't read it (so an XSS can't exfiltrate it) and
// doesn't need to — the browser attaches the cookie automatically to same-
// origin form posts, fetches, and the WebSocket handshake.
(function () {
  "use strict";

  // --- realtime channel ---------------------------------------------------
  var pill = document.getElementById("live");
  function setLive(on) {
    if (!pill) return;
    pill.classList.toggle("live-on", on);
    pill.classList.toggle("live-off", !on);
  }

  var reloadTimer = null;
  function scheduleReload() {
    // Debounce: a burst of broadcasts (e.g. someone approving several in a
    // row) collapses into one reload.
    if (reloadTimer) clearTimeout(reloadTimer);
    reloadTimer = setTimeout(function () { window.location.reload(); }, 250);
  }

  function connect() {
    if (!("WebSocket" in window)) return;
    var proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    // The HttpOnly cookie rides the same-origin handshake automatically — no
    // token in the URL.
    var wsUrl = proto + "//" + window.location.host + "/ws";
    var ws;
    try {
      ws = new WebSocket(wsUrl);
    } catch (e) {
      return;
    }
    ws.onopen = function () { setLive(true); };
    ws.onclose = function () {
      setLive(false);
      // Reconnect with a small backoff so a server restart heals itself.
      setTimeout(connect, 1500);
    };
    ws.onmessage = function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === "refresh") scheduleReload();
    };
  }
  connect();

  // --- keyboard shortcuts -------------------------------------------------
  var rows = Array.prototype.slice.call(document.querySelectorAll(".queue-row[data-proposal-id]"));
  var cursor = -1;

  function focusRow(i) {
    if (!rows.length) return;
    cursor = Math.max(0, Math.min(i, rows.length - 1));
    rows.forEach(function (r, idx) { r.classList.toggle("cursor", idx === cursor); });
    rows[cursor].scrollIntoView({ block: "nearest" });
    rows[cursor].focus();
  }

  function submitAct(row, act) {
    var form = row.querySelector("form.js-act[data-act='" + act + "']");
    if (!form) return;
    if (act === "reject") {
      var input = form.querySelector("input[name='reason']");
      if (input) { input.focus(); return; } // require a reason, don't auto-submit
    }
    form.submit();
  }

  document.addEventListener("keydown", function (e) {
    // Don't hijack typing in an input/textarea.
    var tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || e.metaKey || e.ctrlKey) return;

    switch (e.key) {
      case "j": focusRow(cursor + 1); e.preventDefault(); break;
      case "k": focusRow(cursor - 1); e.preventDefault(); break;
      case "a":
        if (cursor >= 0) { submitAct(rows[cursor], "approve"); e.preventDefault(); }
        break;
      case "r":
        if (cursor >= 0) { submitAct(rows[cursor], "reject"); e.preventDefault(); }
        break;
      case "?":
        alert("vouch review shortcuts\n\n j / k  move down / up\n a      approve focused\n r      reject focused (then type a reason)\n ?      this help");
        e.preventDefault();
        break;
    }
  });
})();
