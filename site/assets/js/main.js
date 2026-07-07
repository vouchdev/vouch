/* vouch landing — review-gate demo, knowledge graphs, timeline, animations.
   plain js, no dependencies. all data below is a mock demonstration KB.
   note on innerHTML: every string rendered comes from the static constants
   in this file — no user, URL, or network input ever reaches these
   templates. keep it that way if you edit. */

(function () {
  'use strict';

  var REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------- copy buttons ---------- */

  ['copy-btn', 'copy-btn-2'].forEach(function (id) {
    var btn = document.getElementById(id);
    if (!btn) return;
    btn.addEventListener('click', function () {
      navigator.clipboard && navigator.clipboard.writeText('pipx install vouch-kb').then(function () {
        btn.textContent = 'copied';
        setTimeout(function () { btn.textContent = 'copy'; }, 1600);
      });
    });
  });

  /* ---------- scroll reveals ---------- */

  if ('IntersectionObserver' in window && !REDUCED) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
      });
    }, { rootMargin: '0px 0px -8% 0px' });
    document.querySelectorAll('.reveal:not(.in)').forEach(function (el) { io.observe(el); });
  } else {
    document.querySelectorAll('.reveal').forEach(function (el) { el.classList.add('in'); });
  }

  /* ---------- theme colors (read once from tokens) ---------- */

  var css = getComputedStyle(document.documentElement);
  var C = {
    claim: css.getPropertyValue('--k-claim').trim() || '#5dcaa5',
    source: css.getPropertyValue('--k-source').trim() || '#9d8ed0',
    entity: css.getPropertyValue('--k-entity').trim() || '#7ea8ff',
    page: css.getPropertyValue('--k-page').trim() || '#8b7ff0',
    person: css.getPropertyValue('--k-person').trim() || '#88a8d9',
    concept: css.getPropertyValue('--k-concept').trim() || '#c9a2e0',
    tool: css.getPropertyValue('--k-tool').trim() || '#7eb9c4',
    ink: '#ebeef5',
    dim: 'rgba(157,161,173,0.9)',
    faint: 'rgba(90,93,104,0.95)',
    edge: 'rgba(235,238,245,0.10)',
    edgeFresh: 'rgba(93,202,165,0.55)'
  };

  /* =========================================================
     PART 1 — hero review console (queue, mini graph, ledger)
     ========================================================= */

  var CLAIMS = [
    { id: 'claim/deploy-tagged-commit', text: 'Production deploys run `make release` from a tagged commit.', src: 'RUNBOOK.md', hash: '9f31c4a8…e2', links: [{ to: 'source/runbook', type: 'cites' }, { to: 'entity/deploy', type: 'mentions' }], auto: 'approve', why: 'matches runbook', by: 'alice-example' },
    { id: 'claim/skip-migration-tests', text: 'Skipping migration tests is safe on hotfix branches.', src: '(no source cited)', hash: null, links: [], auto: 'reject', why: 'contradicts RUNBOOK.md §4', by: 'alice-example' },
    { id: 'claim/use-pnpm', text: 'Use pnpm, not npm — the lockfile is pnpm-lock.yaml.', src: 'CONTRIBUTING.md', hash: 'c58a91d0…19', links: [{ to: 'source/contributing', type: 'cites' }, { to: 'entity/tooling', type: 'mentions' }] },
    { id: 'claim/staging-db-reset', text: 'The staging database resets nightly at 02:00 UTC.', src: 'infra/cron.yaml', hash: '41bc77aa…7d', links: [{ to: 'source/cron', type: 'cites' }, { to: 'entity/staging', type: 'mentions' }] },
    { id: 'claim/tokens-rotate-90d', text: 'API tokens rotate every 90 days.', src: 'SECURITY.md', hash: '73de10fc…4b', links: [{ to: 'source/security', type: 'cites' }, { to: 'page/deployment', type: 'supports' }] },
    { id: 'claim/payments-own-retries', text: 'The payments service retries webhooks itself — clients must not.', src: 'services/payments/README.md', hash: 'e814a2c9…33', links: [{ to: 'entity/payments', type: 'mentions' }, { to: 'source/payments-readme', type: 'cites' }] }
  ];

  var SEED_NODES = [
    { id: 'page/deployment', label: 'deployment', type: 'page' },
    { id: 'source/runbook', label: 'RUNBOOK.md', type: 'source' },
    { id: 'source/contributing', label: 'CONTRIBUTING.md', type: 'source' },
    { id: 'source/security', label: 'SECURITY.md', type: 'source' },
    { id: 'source/cron', label: 'cron.yaml', type: 'source' },
    { id: 'source/payments-readme', label: 'payments/README', type: 'source' },
    { id: 'entity/deploy', label: 'deploy', type: 'entity' },
    { id: 'entity/staging', label: 'staging', type: 'entity' },
    { id: 'entity/tooling', label: 'tooling', type: 'entity' },
    { id: 'entity/payments', label: 'payments', type: 'entity' },
    { id: 'claim/rollback-by-tag', label: 'rollback-by-tag', type: 'claim' }
  ];
  var SEED_EDGES = [
    { a: 'claim/rollback-by-tag', b: 'source/runbook', type: 'cites' },
    { a: 'claim/rollback-by-tag', b: 'entity/deploy', type: 'mentions' },
    { a: 'page/deployment', b: 'claim/rollback-by-tag', type: 'cites' },
    { a: 'entity/deploy', b: 'source/runbook', type: '' },
    { a: 'entity/staging', b: 'source/cron', type: '' }
  ];

  var canvas = document.getElementById('kgraph');
  var ctx = canvas ? canvas.getContext('2d') : null;
  var nodes = [], edges = [], tick = 0;

  function sizeCanvas(cv, c2d) {
    var r = cv.getBoundingClientRect();
    var dpr = window.devicePixelRatio || 1;
    cv.width = r.width * dpr;
    cv.height = r.height * dpr;
    c2d.setTransform(dpr, 0, 0, dpr, 0, 0);
    return r;
  }

  function seedGraph() {
    var r = canvas.getBoundingClientRect();
    var cx = r.width / 2, cy = r.height / 2;
    nodes = SEED_NODES.map(function (n, i) {
      var angle = (i / SEED_NODES.length) * Math.PI * 2 + 0.6;
      var rad = n.type === 'page' ? 0 : (n.type === 'claim' ? Math.min(r.width, r.height) * 0.31 : Math.min(r.width, r.height) * 0.42);
      return {
        id: n.id, label: n.label, type: n.type,
        x: cx + Math.cos(angle) * rad, y: cy + Math.sin(angle) * rad * 0.8,
        tx: cx + Math.cos(angle) * rad, ty: cy + Math.sin(angle) * rad * 0.8,
        born: -100, phase: Math.random() * Math.PI * 2, labelUp: i % 2 === 0
      };
    });
    edges = SEED_EDGES.map(function (e) { return { a: e.a, b: e.b, type: e.type, born: -100 }; });
  }

  function nodeById(id) {
    for (var i = 0; i < nodes.length; i++) if (nodes[i].id === id) return nodes[i];
    return null;
  }

  function addClaimNode(claim) {
    var r = canvas.getBoundingClientRect();
    var cx = r.width / 2, cy = r.height / 2;
    var claimCount = nodes.filter(function (n) { return n.type === 'claim'; }).length;
    var angle = claimCount * 2.4 + 1.2;
    var rad = Math.min(r.width, r.height) * 0.31;
    nodes.push({
      id: claim.id, label: claim.id.replace('claim/', ''), type: 'claim',
      x: 12, y: cy,
      tx: cx + Math.cos(angle) * rad, ty: cy + Math.sin(angle) * rad,
      born: tick, phase: Math.random() * Math.PI * 2, labelUp: claimCount % 2 === 1
    });
    claim.links.forEach(function (l) {
      if (nodeById(l.to)) edges.push({ a: claim.id, b: l.to, type: l.type, born: tick });
    });
  }

  function drawShape(c2d, n, x, y, rr) {
    c2d.fillStyle = C[n.type] || C.entity;
    if (n.type === 'claim' || n.type === 'person' || n.type === 'concept' || n.type === 'tool') {
      c2d.beginPath(); c2d.arc(x, y, rr, 0, Math.PI * 2); c2d.fill();
    } else if (n.type === 'source') {
      c2d.fillRect(x - rr, y - rr, rr * 2, rr * 2);
    } else if (n.type === 'entity') {
      c2d.save(); c2d.translate(x, y); c2d.rotate(Math.PI / 4);
      c2d.fillRect(-rr, -rr, rr * 2, rr * 2); c2d.restore();
    } else { /* page */
      c2d.beginPath();
      if (c2d.roundRect) c2d.roundRect(x - rr * 1.3, y - rr, rr * 2.6, rr * 2, 3); else c2d.rect(x - rr * 1.3, y - rr, rr * 2.6, rr * 2);
      c2d.fill();
    }
  }

  /* layered-sine float: visible organic drift around a node's anchor.
     amp scales by node type (hubs anchor the eye, satellites wander). */
  function floatPos(n, t) {
    if (REDUCED) return { x: n.x, y: n.y };
    var amp = n.type === 'page' ? 2 : 6;
    var s1 = n.s1 || (n.s1 = 0.008 + ((n.phase * 997) % 1) * 0.010);
    var s2 = n.s2 || (n.s2 = 0.013 + ((n.phase * 613) % 1) * 0.012);
    return {
      x: n.x + Math.sin(t * s1 + n.phase) * amp + Math.sin(t * s2 + n.phase * 1.7) * amp * 0.45,
      y: n.y + Math.cos(t * s1 * 0.85 + n.phase) * amp * 0.8 + Math.cos(t * s2 * 1.1 + n.phase * 2.3) * amp * 0.4
    };
  }

  function render() {
    if (!ctx) return;
    var r = canvas.getBoundingClientRect();
    ctx.clearRect(0, 0, r.width, r.height);
    tick++;

    nodes.forEach(function (n) {
      n.x += (n.tx - n.x) * 0.06;
      n.y += (n.ty - n.y) * 0.06;
      var p = floatPos(n, tick);
      n.px = p.x; n.py = p.y;
    });

    edges.forEach(function (e, idx) {
      var a = nodeById(e.a), b = nodeById(e.b);
      if (!a || !b) return;
      var fresh = tick - e.born < 70;
      ctx.strokeStyle = fresh ? C.edgeFresh : C.edge;
      ctx.lineWidth = fresh ? 1.6 : 1;
      ctx.beginPath(); ctx.moveTo(a.px, a.py); ctx.lineTo(b.px, b.py); ctx.stroke();
      if (e.type && (fresh || idx < 3)) {
        ctx.font = '9px "Geist Mono", monospace';
        ctx.fillStyle = C.faint;
        ctx.textAlign = 'center';
        ctx.fillText(e.type, (a.px + b.px) / 2, (a.py + b.py) / 2 - 4);
      }
    });

    nodes.forEach(function (n) {
      var x = n.px;
      var y = n.py;
      var appear = Math.min(1, (tick - n.born) / 30);
      ctx.globalAlpha = appear;
      drawShape(ctx, n, x, y, n.type === 'page' ? 7 : 6);
      if (n.type === 'claim' && tick - n.born < 60 && tick - n.born > -50) {
        ctx.strokeStyle = 'rgba(93,202,165,' + (0.6 * (1 - (tick - n.born) / 60)) + ')';
        ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.arc(x, y, 6 + (tick - n.born) / 4, 0, Math.PI * 2); ctx.stroke();
      }
      ctx.font = '10px "Geist Mono", monospace';
      ctx.fillStyle = C.dim;
      ctx.textAlign = 'center';
      var label = n.label.length > 16 ? n.label.slice(0, 15) + '…' : n.label;
      ctx.fillText(label, x, n.type === 'page' ? y + 24 : (n.labelUp ? y - 13 : y + 20));
      ctx.globalAlpha = 1;
    });

    if (!REDUCED || tick < 120) requestAnimationFrame(render);
  }

  /* ledger */
  var ledgerEl = document.getElementById('ledger-lines');
  var clockMin = 41, clockSec = 12;
  function ledgerTime() {
    clockSec += 7 + Math.floor(Math.random() * 40);
    if (clockSec >= 60) { clockMin += 1; clockSec -= 60; }
    return '10:' + String(clockMin).padStart(2, '0') + ':' + String(clockSec).padStart(2, '0');
  }
  function ledgerLine(ev, id, why, by, fresh) {
    var li = document.createElement('li');
    if (fresh) li.className = 'fresh';
    li.innerHTML =
      '<span class="t">' + ledgerTime() + 'Z</span>' +
      '<span class="ev-' + ev + '">' + ev + '</span> ' +
      '<span class="id">' + id + '</span>' +
      (by ? ' <span class="why">— ' + by + (why ? ': “' + why + '”' : '') + '</span>' : '');
    ledgerEl.appendChild(li);
    while (ledgerEl.children.length > 5) ledgerEl.removeChild(ledgerEl.firstChild);
  }

  /* queue */
  var queueEl = document.getElementById('queue-cards');
  var countEl = document.getElementById('queue-count');
  var yourTurnEl = document.getElementById('your-turn');
  var qIndex = 0, pendingLeft = CLAIMS.length, VISIBLE = 2;
  var autoplayTimer = null, userActed = false;

  function shortSrc(c) {
    return c.hash
      ? c.src + ' <span class="hash">sha256:' + c.hash + '</span>'
      : '<span class="nosrc">' + c.src + '</span>';
  }

  function makeCard(claim) {
    var card = document.createElement('div');
    card.className = 'claim-card';
    card.dataset.id = claim.id;
    card.innerHTML =
      '<div class="cid"><span>' + claim.id + '</span><span class="status">PENDING</span></div>' +
      '<div class="ctext">' + claim.text + '</div>' +
      '<div class="csrc">source: ' + shortSrc(claim) + '</div>' +
      '<div class="actions">' +
        '<button class="stamp-btn stamp-approve" type="button" aria-label="approve ' + claim.id + '">✓ APPROVE</button>' +
        '<button class="stamp-btn stamp-reject" type="button" aria-label="reject ' + claim.id + '">✕ REJECT</button>' +
      '</div>' +
      '<div class="stamp-mark" aria-hidden="true"></div>';
    card.querySelector('.stamp-approve').addEventListener('click', function () { userDecide(card, claim, true); });
    card.querySelector('.stamp-reject').addEventListener('click', function () { userDecide(card, claim, false); });
    return card;
  }

  function fillQueue() {
    while (queueEl.children.length < VISIBLE && qIndex < CLAIMS.length) {
      queueEl.appendChild(makeCard(CLAIMS[qIndex]));
      qIndex++;
    }
    var first = queueEl.querySelector('.claim-card');
    queueEl.querySelectorAll('.claim-card').forEach(function (c) { c.classList.remove('is-next'); });
    if (first) first.classList.add('is-next');
    countEl.textContent = pendingLeft + ' pending';
    if (pendingLeft === 0) {
      countEl.textContent = 'queue clear';
      yourTurnEl.textContent = 'queue clear — every write reviewed ✓';
      yourTurnEl.classList.add('show');
    }
  }

  function decide(card, claim, approved, by, why) {
    if (card.dataset.done) return;
    card.dataset.done = '1';
    pendingLeft--;
    var mark = card.querySelector('.stamp-mark');
    mark.textContent = approved ? 'APPROVED' : 'REJECTED';
    mark.className = 'stamp-mark show ' + (approved ? 'ok' : 'no');
    card.style.borderLeftColor = approved ? C.claim : '#e07a8b';
    var delay = REDUCED ? 60 : 620;
    setTimeout(function () {
      card.classList.add(approved ? 'leaving-approved' : 'leaving-rejected');
      if (approved) addClaimNode(claim);
      ledgerLine(approved ? 'approve' : 'reject', claim.id, why || claim.why, by || claim.by || 'you', true);
      setTimeout(function () { card.remove(); fillQueue(); }, REDUCED ? 30 : 380);
    }, delay);
  }

  function userDecide(card, claim, approved) {
    userActed = true;
    if (autoplayTimer) { clearTimeout(autoplayTimer); autoplayTimer = null; }
    yourTurnEl.classList.remove('show');
    decide(card, claim, approved, 'you', 'stamped from vouchai.dev');
  }

  function autoplay(step) {
    if (userActed) return;
    var card = queueEl.querySelector('.claim-card');
    if (!card) return;
    var claim = CLAIMS.find(function (c) { return c.id === card.dataset.id; });
    if (step < 2 && claim && claim.auto) {
      decide(card, claim, claim.auto === 'approve');
      autoplayTimer = setTimeout(function () { autoplay(step + 1); }, REDUCED ? 500 : 2600);
    } else {
      yourTurnEl.classList.add('show');
    }
  }

  /* =========================================================
     PART 2 — the big map (force-laid mock KB, hover to trace)
     ========================================================= */

  var big = document.getElementById('bigmap');
  var bctx = big ? big.getContext('2d') : null;
  var M = { nodes: [], edges: [], hover: -1, t: 0 };

  function buildMapData() {
    /* deterministic mock KB: pages ← claims ← sources, entities/people/concepts orbit */
    var rng = (function (s) { return function () { s = (s * 1103515245 + 12345) % 2147483648; return s / 2147483648; }; })(42);
    var pages = ['deployment', 'auth', 'payments', 'testing', 'infra', 'onboarding'];
    var sources = ['RUNBOOK.md', 'SECURITY.md', 'CONTRIBUTING.md', 'cron.yaml', 'payments/README', 'ARCHITECTURE.md', 'ADR-0007', 'ADR-0012', 'api/openapi.yaml', 'Makefile'];
    var entities = ['deploy', 'staging', 'tokens', 'webhooks', 'pnpm', 'migrations', 'canary', 'rollback', 'sso', 'rate-limits', 'ci', 'sandbox'];
    var people = ['alice-example', 'bob-example'];
    var concepts = ['idempotency', 'least-privilege', 'blue-green', 'backfill'];
    var claimsPerPage = [5, 4, 5, 3, 4, 3];

    var ns = [], es = [];
    pages.forEach(function (p, i) { ns.push({ id: 'page/' + p, label: p, type: 'page', deg: 0 }); });
    sources.forEach(function (s) { ns.push({ id: 'source/' + s, label: s, type: 'source', deg: 0 }); });
    entities.forEach(function (e) { ns.push({ id: 'entity/' + e, label: e, type: 'entity', deg: 0 }); });
    people.forEach(function (p) { ns.push({ id: 'person/' + p, label: p, type: 'person', deg: 0 }); });
    concepts.forEach(function (c) { ns.push({ id: 'concept/' + c, label: c, type: 'concept', deg: 0 }); });

    var claimN = 0;
    pages.forEach(function (p, pi) {
      for (var k = 0; k < claimsPerPage[pi]; k++) {
        var cid = 'claim/' + p + '-' + (k + 1);
        ns.push({ id: cid, label: p + '-' + (k + 1), type: 'claim', deg: 0 });
        claimN++;
        es.push({ a: 'page/' + p, b: cid, type: 'cites' });
        es.push({ a: cid, b: 'source/' + sources[Math.floor(rng() * sources.length)], type: 'cites' });
        es.push({ a: cid, b: 'entity/' + entities[Math.floor(rng() * entities.length)], type: 'mentions' });
        if (rng() > 0.72) es.push({ a: cid, b: 'concept/' + concepts[Math.floor(rng() * concepts.length)], type: 'mentions' });
        if (rng() > 0.65) es.push({ a: cid, b: 'person/' + people[Math.floor(rng() * people.length)], type: 'approved-by' });
      }
    });
    /* a few page↔entity links */
    es.push({ a: 'page/deployment', b: 'entity/deploy', type: 'mentions' });
    es.push({ a: 'page/auth', b: 'entity/sso', type: 'mentions' });
    es.push({ a: 'page/payments', b: 'entity/webhooks', type: 'mentions' });

    var idx = {};
    ns.forEach(function (n, i) { idx[n.id] = i; });
    es.forEach(function (e) {
      e.ai = idx[e.a]; e.bi = idx[e.b];
      ns[e.ai].deg++; ns[e.bi].deg++;
    });

    document.getElementById('ms-claims').textContent = claimN;
    document.getElementById('ms-sources').textContent = sources.length;
    document.getElementById('ms-pages').textContent = pages.length;

    return { nodes: ns, edges: es, rng: rng };
  }

  function layoutMap(data, w, h) {
    var ns = data.nodes, es = data.edges, rng = data.rng;
    ns.forEach(function (n) {
      n.x = w / 2 + (rng() - 0.5) * w * 0.7;
      n.y = h / 2 + (rng() - 0.5) * h * 0.7;
      n.vx = 0; n.vy = 0;
      n.r = n.type === 'page' ? 9 : n.type === 'claim' ? 4.5 : n.type === 'source' ? 5.5 : 4.5;
      n.phase = rng() * Math.PI * 2;
    });
    /* frozen force sim: repulsion + springs + centering */
    for (var it = 0; it < 260; it++) {
      var alpha = 1 - it / 260;
      for (var i = 0; i < ns.length; i++) {
        for (var j = i + 1; j < ns.length; j++) {
          var dx = ns[j].x - ns[i].x, dy = ns[j].y - ns[i].y;
          var d2 = dx * dx + dy * dy + 0.01, d = Math.sqrt(d2);
          if (d < 140) {
            var f = 900 / d2 * alpha;
            var fx = dx / d * f, fy = dy / d * f;
            ns[i].vx -= fx; ns[i].vy -= fy;
            ns[j].vx += fx; ns[j].vy += fy;
          }
        }
      }
      es.forEach(function (e) {
        var a = ns[e.ai], b = ns[e.bi];
        var dx = b.x - a.x, dy = b.y - a.y;
        var d = Math.sqrt(dx * dx + dy * dy) + 0.01;
        var want = 62;
        var f = (d - want) * 0.02 * alpha;
        var fx = dx / d * f, fy = dy / d * f;
        a.vx += fx; a.vy += fy;
        b.vx -= fx; b.vy -= fy;
      });
      ns.forEach(function (n) {
        n.vx += (w / 2 - n.x) * 0.004 * alpha;
        n.vy += (h / 2 - n.y) * 0.004 * alpha;
        n.x += n.vx; n.y += n.vy;
        n.vx *= 0.6; n.vy *= 0.6;
        n.x = Math.max(30, Math.min(w - 30, n.x));
        n.y = Math.max(26, Math.min(h - 30, n.y));
      });
    }
  }

  function renderMap() {
    if (!bctx) return;
    var r = big.getBoundingClientRect();
    bctx.clearRect(0, 0, r.width, r.height);
    M.t++;
    var ns = M.nodes, es = M.edges;
    var hov = M.hover;

    ns.forEach(function (n) {
      var p = floatPos(n, M.t);
      n.px = p.x; n.py = p.y;
    });

    var connected = null;
    if (hov >= 0) {
      connected = new Set([hov]);
      es.forEach(function (e) {
        if (e.ai === hov) connected.add(e.bi);
        if (e.bi === hov) connected.add(e.ai);
      });
    }

    es.forEach(function (e) {
      var a = ns[e.ai], b = ns[e.bi];
      var lit = hov >= 0 && (e.ai === hov || e.bi === hov);
      bctx.strokeStyle = lit ? 'rgba(139,127,240,0.6)' : (hov >= 0 ? 'rgba(235,238,245,0.04)' : C.edge);
      bctx.lineWidth = lit ? 1.5 : 1;
      bctx.beginPath();
      bctx.moveTo(a.px, a.py);
      bctx.lineTo(b.px, b.py);
      bctx.stroke();
      if (lit && e.type) {
        bctx.font = '9px "Geist Mono", monospace';
        bctx.fillStyle = 'rgba(157,161,173,0.9)';
        bctx.textAlign = 'center';
        bctx.fillText(e.type, (a.px + b.px) / 2, (a.py + b.py) / 2 - 4);
      }
    });

    ns.forEach(function (n, i) {
      var x = n.px;
      var y = n.py;
      var dimmed = connected && !connected.has(i);
      bctx.globalAlpha = dimmed ? 0.18 : 1;
      drawShape(bctx, n, x, y, n.r);
      if (i === hov || (!connected && n.type === 'page') || (connected && connected.has(i) && (n.type === 'page' || i === hov))) {
        bctx.font = (n.type === 'page' ? '11px' : '10px') + ' "Geist Mono", monospace';
        bctx.fillStyle = i === hov ? C.ink : C.dim;
        bctx.textAlign = 'center';
        bctx.fillText(n.label, x, y - n.r - 7);
      }
      bctx.globalAlpha = 1;
    });

    if (!REDUCED || M.t < 120) requestAnimationFrame(renderMap);
  }

  function initMap() {
    if (!big) return;
    var r = sizeCanvas(big, bctx);
    var data = buildMapData();
    layoutMap(data, r.width, r.height);
    M.nodes = data.nodes;
    M.edges = data.edges;
    renderMap();

    var hint = document.getElementById('map-hint');
    big.addEventListener('mousemove', function (ev) {
      var rect = big.getBoundingClientRect();
      var mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
      var best = -1, bd = 22 * 22;
      M.nodes.forEach(function (n, i) {
        var dx = (n.px != null ? n.px : n.x) - mx, dy = (n.py != null ? n.py : n.y) - my, d = dx * dx + dy * dy;
        if (d < bd) { bd = d; best = i; }
      });
      M.hover = best;
      if (hint) hint.textContent = best >= 0 ? M.nodes[best].id : 'demo data — hover a node';
      if (REDUCED) { M.t = 0; renderMap(); }
    });
    big.addEventListener('mouseleave', function () {
      M.hover = -1;
      if (hint) hint.textContent = 'demo data — hover a node';
      if (REDUCED) { M.t = 0; renderMap(); }
    });
    window.addEventListener('resize', function () {
      var rr = sizeCanvas(big, bctx);
      layoutMap({ nodes: M.nodes, edges: M.edges, rng: (function (s) { return function () { s = (s * 1103515245 + 12345) % 2147483648; return s / 2147483648; }; })(7) }, rr.width, rr.height);
      if (REDUCED) { M.t = 0; renderMap(); }
    });
  }

  /* =========================================================
     PART 3 — pipeline flow stage cycling
     ========================================================= */

  function initFlow() {
    var track = document.getElementById('flow-track');
    if (!track) return;
    var stages = [].slice.call(track.querySelectorAll('.flow-stage'));
    var i = 0;
    function step() {
      stages.forEach(function (s, j) { s.classList.toggle('active', j === i); });
      i = (i + 1) % stages.length;
    }
    step();
    if (!REDUCED) setInterval(step, 1300);
    else stages.forEach(function (s) { s.classList.add('active'); });
  }

  /* =========================================================
     PART 4 — timeline stream
     ========================================================= */

  var TL_EVENTS = [
    { ev: 'propose', c: '#e1b340', body: '<b>claim/canary-first</b> — deploys hit canary before fleet <i>· agent:claude-code</i>' },
    { ev: 'approve', c: '#5dcaa5', body: '<b>claim/canary-first</b> <i>· alice-example: "matches ADR-0012"</i>' },
    { ev: 'compile', c: '#8b7ff0', body: '<b>page/deployment</b> recompiled — 5 claims, 0 dangling citations' },
    { ev: 'propose', c: '#e1b340', body: '<b>claim/redis-eviction-lru</b> — cache evicts LRU at 80% <i>· agent:cursor</i>' },
    { ev: 'reject', c: '#e07a8b', body: '<b>claim/redis-eviction-lru</b> <i>· bob-example: "config says allkeys-lfu, recheck"</i>' },
    { ev: 'supersede', c: '#7ea8ff', body: '<b>claim/tokens-rotate-30d → tokens-rotate-90d</b> <i>· policy change, provenance kept</i>' },
    { ev: 'propose', c: '#e1b340', body: '<b>claim/webhook-idempotency-keys</b> — retries carry idempotency keys <i>· agent:claude-code</i>' },
    { ev: 'approve', c: '#5dcaa5', body: '<b>claim/webhook-idempotency-keys</b> <i>· alice-example: "verified in payments/README"</i>' },
    { ev: 'approve', c: '#5dcaa5', body: '<b>session f555c022 crystallized</b> — 6 proposals approved in one review' },
    { ev: 'compile', c: '#8b7ff0', body: '<b>page/payments</b> recompiled — new claim cited, wikilinks verified' }
  ];
  var TL_COLORS = { propose: '#e1b340', approve: '#5dcaa5', reject: '#e07a8b', supersede: '#7ea8ff', compile: '#8b7ff0' };

  function initTimeline() {
    var feed = document.getElementById('tl-feed');
    if (!feed) return;
    var i = 0, h = 14, m = 6, s = 2;
    function stamp() {
      s += 11 + Math.floor(Math.random() * 37);
      if (s >= 60) { m++; s -= 60; }
      if (m >= 60) { h++; m -= 60; }
      return String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0') + 'Z';
    }
    function addRow() {
      var e = TL_EVENTS[i % TL_EVENTS.length];
      i++;
      var li = document.createElement('li');
      li.className = 'tl-row';
      li.innerHTML =
        '<span class="tt">' + stamp() + '</span>' +
        '<span class="lane" style="background:' + TL_COLORS[e.ev] + ';color:' + TL_COLORS[e.ev] + '"></span>' +
        '<span class="ev" style="color:' + TL_COLORS[e.ev] + '">' + e.ev + '</span>' +
        '<span class="body">' + e.body + '</span>';
      feed.insertBefore(li, feed.firstChild);
      while (feed.children.length > 12) feed.removeChild(feed.lastChild);
    }
    for (var k = 0; k < 8; k++) addRow();
    if (!REDUCED) {
      var timer = null;
      var io2 = new IntersectionObserver(function (entries) {
        entries.forEach(function (en) {
          if (en.isIntersecting && !timer) timer = setInterval(addRow, 2100);
          else if (!en.isIntersecting && timer) { clearInterval(timer); timer = null; }
        });
      }, { threshold: 0.2 });
      io2.observe(feed);
    }
  }

  /* =========================================================
     PART 5 — count-up proof numbers
     ========================================================= */

  function initCountups() {
    var els = [].slice.call(document.querySelectorAll('[data-count]'));
    if (!els.length) return;
    function run(el) {
      var target = parseInt(el.dataset.count, 10);
      if (REDUCED) { el.textContent = '−' + target + '%'; return; }
      var start = null;
      function frame(ts) {
        if (!start) start = ts;
        var p = Math.min(1, (ts - start) / 1100);
        var eased = 1 - Math.pow(1 - p, 3);
        el.textContent = '−' + Math.round(target * eased) + '%';
        if (p < 1) requestAnimationFrame(frame);
      }
      requestAnimationFrame(frame);
    }
    var io3 = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) { run(en.target); io3.unobserve(en.target); }
      });
    }, { threshold: 0.6 });
    els.forEach(function (el) { io3.observe(el); });
  }

  /* ---------- boot ---------- */

  function boot() {
    if (canvas) {
      sizeCanvas(canvas, ctx);
      seedGraph();
      render();
      ledgerLine('propose', 'claim/deploy-tagged-commit', null, 'agent:claude-code');
      ledgerLine('propose', 'claim/skip-migration-tests', null, 'agent:claude-code');
      ledgerLine('approve', 'claim/rollback-by-tag', 'verified against RUNBOOK.md', 'alice-example');
      fillQueue();

      var started = false;
      function start() {
        if (started) return;
        started = true;
        autoplayTimer = setTimeout(function () { autoplay(0); }, REDUCED ? 400 : 1400);
      }
      if ('IntersectionObserver' in window) {
        var cio = new IntersectionObserver(function (entries) {
          entries.forEach(function (e) { if (e.isIntersecting) { start(); cio.disconnect(); } });
        }, { threshold: 0.35 });
        cio.observe(document.getElementById('console'));
      } else { start(); }

      window.addEventListener('resize', function () {
        sizeCanvas(canvas, ctx);
        var r = canvas.getBoundingClientRect();
        var cx = r.width / 2, cy = r.height / 2;
        nodes.forEach(function (n, i) {
          if (n.type === 'page') { n.tx = cx; n.ty = cy; return; }
          var angle = (i / nodes.length) * Math.PI * 2 + 0.6;
          var rad = n.type === 'claim' ? Math.min(r.width, r.height) * 0.31 : Math.min(r.width, r.height) * 0.42;
          n.tx = cx + Math.cos(angle) * rad;
          n.ty = cy + Math.sin(angle) * rad * 0.8;
        });
        if (REDUCED) { tick = 0; render(); }
      });
    }
    initMap();
    initFlow();
    initTimeline();
    initCountups();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
