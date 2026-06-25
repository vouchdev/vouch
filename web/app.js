// ============================================================
// app.js — the interactive layer for the Gittensor fascicle.
//
// Progressive enhancement, the same posture as the vouch review
// console: the page is a complete, readable monograph with this file
// absent. When it runs it adds an `html.js` flag and five behaviours:
//
//   1. scroll-reveal   — gate the SVG draw-ins to viewport entry
//   2. plate-nav rail  — a fixed index that tracks the active plate
//   3. live terminal   — a replayable `vouch init --template gittensor`
//   4. review-gate     — click propose→review→approve→recall; self-
//                        approval is refused, supersede keeps history
//   5. claim catalogue — search/filter the seven seeded SN74 claims
//
// No framework, no build step, no network. Every widget is driven by
// the real seeded content embedded in index.html.
// ============================================================
(function () {
  "use strict";

  var root = document.documentElement;
  root.classList.add("js");

  var REDUCE =
    window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function $(sel, ctx) { return (ctx || document).querySelector(sel); }
  function $all(sel, ctx) {
    return Array.prototype.slice.call((ctx || document).querySelectorAll(sel));
  }
  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }

  // ----------------------------------------------------------
  // 1 · SCROLL-REVEAL
  // ----------------------------------------------------------
  function initReveal() {
    var sections = $all(".plate, .masthead");
    if (REDUCE || !("IntersectionObserver" in window)) {
      sections.forEach(function (s) { s.classList.add("revealed"); });
      return;
    }
    var io = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (e) {
          if (e.isIntersecting) {
            e.target.classList.add("revealed");
            io.unobserve(e.target);
          }
        });
      },
      { rootMargin: "0px 0px -12% 0px", threshold: 0.12 }
    );
    sections.forEach(function (s) { io.observe(s); });
  }

  // ----------------------------------------------------------
  // 2 · PLATE-NAV RAIL  +  READING PROGRESS
  // ----------------------------------------------------------
  function initRail() {
    var plates = $all(".plate[id]");
    if (!plates.length) return;

    var rail = el("nav", "plate-rail");
    rail.setAttribute("aria-label", "Plate index");
    plates.forEach(function (p) {
      var label = $(".plate-label span", p);
      var name = label ? label.textContent.trim() : p.id;
      var a = el(
        "a",
        null,
        '<span class="tick"></span><span class="rail-label">' + name + "</span>"
      );
      a.href = "#" + p.id;
      a.dataset.target = p.id;
      rail.appendChild(a);
    });
    document.body.appendChild(rail);

    var progress = el("div", "read-progress");
    document.body.appendChild(progress);

    var links = $all("a", rail);
    function setActive(id) {
      links.forEach(function (l) {
        l.classList.toggle("active", l.dataset.target === id);
      });
    }

    if ("IntersectionObserver" in window) {
      var spy = new IntersectionObserver(
        function (entries) {
          entries.forEach(function (e) {
            if (e.isIntersecting) setActive(e.target.id);
          });
        },
        { rootMargin: "-45% 0px -45% 0px", threshold: 0 }
      );
      plates.forEach(function (p) { spy.observe(p); });
    }

    var ticking = false;
    function onScroll() {
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(function () {
        var h = document.documentElement;
        var max = h.scrollHeight - h.clientHeight;
        var pct = max > 0 ? (h.scrollTop / max) * 100 : 0;
        progress.style.width = pct.toFixed(1) + "%";
        ticking = false;
      });
    }
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  }

  // ----------------------------------------------------------
  // 3 · LIVE TERMINAL  — vouch init --template gittensor
  // ----------------------------------------------------------
  function initTerminal() {
    var body = $("#term-body");
    var replay = $("#term-replay");
    if (!body) return;

    var claims = readClaims();
    var seedLines = claims.map(function (c) {
      return { k: "out", c: "ok", t: "  ▸ claim   " + pad(c.id, 34) + "✓ approved" };
    });

    var script = [
      { k: "cmd", t: "vouch init --template gittensor" },
      { k: "out", c: "dim", t: "  ▸ creating .vouch/" + pad("", 27) + "ok" },
      { k: "out", c: "ok", t: "  ▸ source  " + pad("Gittensor SN74", 34) + "ok" },
      { k: "out", c: "ok", t: "  ▸ entity  " + pad("gittensor-sn74", 34) + "ok" }
    ]
      .concat(seedLines)
      .concat([
        { k: "out", c: "accent", t: "  seeded: 1 source · 1 entity · 7 claims (cited, approved)" },
        { k: "gap" },
        { k: "cmd", t: "vouch status" },
        { k: "out", c: "dim", t: "  durable: 7 claims · 1 source · 1 entity · 0 pending" },
        { k: "gap" },
        { k: "cmd", t: 'vouch search "emission split"' },
        {
          k: "out",
          c: "dim",
          t: "  claim/gittensor-emission-split  …split between OSS rewards\n  and an issue-treasury share; a validator policy decision."
        }
      ]);

    var timers = [];
    function clearTimers() { timers.forEach(clearTimeout); timers = []; }
    function after(ms, fn) { timers.push(setTimeout(fn, ms)); }

    function appendLine(line) {
      if (line.k === "gap") { body.appendChild(el("div", null, "&nbsp;")); return null; }
      if (line.k === "cmd") {
        var c = el("div", "cmd");
        c.innerHTML = '<span class="p">$</span> ';
        body.appendChild(c);
        return c;
      }
      var o = el("div", line.c || "");
      o.textContent = line.t;
      body.appendChild(o);
      return o;
    }

    function typeCmd(node, text, done) {
      if (REDUCE) {
        node.innerHTML = '<span class="p">$</span> ' + escapeHtml(text);
        done();
        return;
      }
      var i = 0;
      var cursor = el("span", "cursor");
      node.appendChild(cursor);
      (function step() {
        if (i >= text.length) { if (cursor.parentNode) cursor.remove(); done(); return; }
        cursor.insertAdjacentText("beforebegin", text.charAt(i));
        i++;
        after(28 + Math.round(Math.random() * 30), step);
      })();
    }

    function run() {
      clearTimers();
      body.innerHTML = "";
      body.scrollTop = 0;
      var idx = 0;
      (function next() {
        if (idx >= script.length) return;
        var line = script[idx++];
        var node = appendLine(line);
        body.scrollTop = body.scrollHeight;
        if (line.k === "cmd") {
          typeCmd(node, line.t, function () {
            after(REDUCE ? 0 : 260, next);
          });
        } else {
          after(REDUCE ? 0 : (line.k === "gap" ? 120 : 90), next);
        }
      })();
    }

    if (replay) replay.addEventListener("click", run);

    // Autostart the first time the terminal scrolls into view.
    if (!REDUCE && "IntersectionObserver" in window) {
      var started = false;
      var io = new IntersectionObserver(
        function (entries) {
          entries.forEach(function (e) {
            if (e.isIntersecting && !started) { started = true; run(); io.disconnect(); }
          });
        },
        { threshold: 0.35 }
      );
      io.observe(body);
    } else {
      run();
    }
  }

  function pad(s, n) {
    s = String(s);
    return s.length >= n ? s + " " : s + new Array(n - s.length + 1).join(" ");
  }

  // ----------------------------------------------------------
  // 4 · REVIEW-GATE SIMULATOR
  // ----------------------------------------------------------
  function initGate() {
    var gate = $("#gate");
    if (!gate) return;

    var chambers = $all(".chamber", gate);
    var readout = $(".gate-readout", gate);
    var bAdvance = $("[data-act='advance']", gate);
    var bSelf = $("[data-act='self']", gate);
    var bSuper = $("[data-act='supersede']", gate);
    var bReset = $("[data-act='reset']", gate);

    // step 0 = idle, 1 propose, 2 review, 3 durable claim, 4 recall
    var STEPS = [
      null,
      { msg: "drafted into <span class='mono-id'>.vouch/proposed/</span> — a proposal, not yet memory. nothing has touched the repo's history.", cls: "" },
      { msg: "a maintainer opens the queue and reads it. the proposer cannot sign their own work.", cls: "" },
      { msg: "approved by a <em>different</em> identity → durable at <span class='mono-id'>.vouch/claims/gittensor-emission-split.yaml</span>, cited and committed.", cls: "ok" },
      { msg: "recall: <span class='mono-id'>kb_context</span> now returns this claim, cited, to any agent in the repo.", cls: "ok" }
    ];
    var step = 0;

    function render() {
      chambers.forEach(function (c, i) {
        c.classList.toggle("lit", i === step - 1);
        c.classList.toggle("done", i < step - 1);
      });
      if (bAdvance) {
        bAdvance.disabled = step >= 4;
        bAdvance.textContent =
          step === 0 ? "propose a claim" :
          step === 1 ? "send to review" :
          step === 2 ? "approve (as another maintainer)" :
          step === 3 ? "recall it" : "complete";
      }
      if (bSuper) bSuper.disabled = step < 3;
    }

    function say(html, cls) {
      readout.className = "gate-readout" + (cls ? " " + cls : "");
      readout.innerHTML = html;
    }

    if (bAdvance) bAdvance.addEventListener("click", function () {
      if (step >= 4) return;
      step++;
      render();
      say(STEPS[step].msg, STEPS[step].cls);
    });

    if (bSelf) bSelf.addEventListener("click", function () {
      // light the review chamber to show where it's refused
      if (step < 2) { step = 2; render(); }
      say(
        "✗ <span class='mono-id'>forbidden_self_approval</span> — the actor that proposed a claim may not approve it. a different identity must sign. <em>this is the whole point of the gate.</em>",
        "err"
      );
    });

    if (bSuper) bSuper.addEventListener("click", function () {
      if (step < 3) return;
      say(
        "superseded: <span class='mono-id'>gittensor-emission-split</span> → <span class='mono-id'>…-emission-split-v2</span>. the old claim is kept, marked superseded — the history of <em>what changed</em> stays queryable.",
        "ok"
      );
    });

    if (bReset) bReset.addEventListener("click", function () {
      step = 0;
      render();
      say("press <em>propose a claim</em> to walk a decision through the gate.", "");
    });

    render();
    say("press <em>propose a claim</em> to walk a decision through the gate.", "");
  }

  // ----------------------------------------------------------
  // 5 · CLAIM CATALOGUE
  // ----------------------------------------------------------
  function initCatalogue() {
    var mount = $("#catalogue");
    if (!mount) return;
    var claims = readClaims();
    if (!claims.length) return;

    var listEl = $(".cat-list", mount);
    var input = $(".cat-search input", mount);
    var countEl = $(".cat-count", mount);
    var tagsEl = $(".cat-tags", mount);

    var allTags = [];
    claims.forEach(function (c) {
      (c.tags || []).forEach(function (t) {
        if (allTags.indexOf(t) === -1) allTags.push(t);
      });
    });
    allTags.sort();

    var active = {};
    allTags.forEach(function (t) {
      var chip = el("button", "cat-tag", t);
      chip.type = "button";
      chip.addEventListener("click", function () {
        active[t] = !active[t];
        chip.classList.toggle("on", !!active[t]);
        render();
      });
      tagsEl.appendChild(chip);
    });

    var cards = claims.map(function (c) {
      var card = el("article", "claim");
      card.innerHTML =
        '<div class="claim-top"><span class="cid">claim/' + escapeHtml(c.id) +
        '</span><span class="conf">conf ' + c.conf + " · " + escapeHtml(c.status) + "</span></div>" +
        '<p class="ctext">' + escapeHtml(c.text) + "</p>" +
        '<div class="cmeta"><span class="lbl">cites</span>' + escapeHtml(c.cite) +
        '<div class="ctags">' + (c.tags || []).map(function (t) {
          return "<span>" + escapeHtml(t) + "</span>";
        }).join("") + "</div></div>";
      card.addEventListener("click", function () { card.classList.toggle("open"); });
      card._claim = c;
      return card;
    });

    function render() {
      var q = (input.value || "").trim().toLowerCase();
      var onTags = allTags.filter(function (t) { return active[t]; });
      var shown = 0;
      cards.forEach(function (card) {
        var c = card._claim;
        var matchText = !q || (c.id + " " + c.text).toLowerCase().indexOf(q) !== -1;
        var matchTag =
          !onTags.length ||
          onTags.some(function (t) { return (c.tags || []).indexOf(t) !== -1; });
        var show = matchText && matchTag;
        card.style.display = show ? "" : "none";
        if (show) shown++;
      });
      countEl.textContent = shown + " of " + claims.length;
      var empty = $(".cat-empty", listEl);
      if (!shown && !empty) {
        listEl.appendChild(el("p", "cat-empty", "no claims match — clear the filter."));
      } else if (shown && empty) {
        empty.remove();
      }
    }

    cards.forEach(function (c) { listEl.appendChild(c); });
    input.addEventListener("input", render);
    render();
  }

  // ----------------------------------------------------------
  // shared helpers
  // ----------------------------------------------------------
  function readClaims() {
    var island = $("#gittensor-claims");
    if (!island) return [];
    try { return JSON.parse(island.textContent); } catch (e) { return []; }
  }
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // ----------------------------------------------------------
  function boot() {
    initReveal();
    initRail();
    initTerminal();
    initGate();
    initCatalogue();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
