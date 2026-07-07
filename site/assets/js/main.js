/* vouch landing — review-gate demo, knowledge graph, ledger.
   plain js, no dependencies. all data below is a mock demonstration KB.
   note on innerHTML: every string rendered comes from the static CLAIMS /
   ledger constants in this file — no user, URL, or network input ever
   reaches these templates. keep it that way if you edit. */

(function () {
  'use strict';

  var REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------- copy button ---------- */

  var copyBtn = document.getElementById('copy-btn');
  if (copyBtn) {
    copyBtn.addEventListener('click', function () {
      var cmd = document.getElementById('install-cmd').textContent;
      navigator.clipboard && navigator.clipboard.writeText(cmd).then(function () {
        copyBtn.textContent = 'copied';
        setTimeout(function () { copyBtn.textContent = 'copy'; }, 1600);
      });
    });
  }

  /* ---------- scroll reveals ---------- */

  if ('IntersectionObserver' in window && !REDUCED) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
      });
    }, { rootMargin: '0px 0px -8% 0px' });
    document.querySelectorAll('.reveal').forEach(function (el) { io.observe(el); });
  } else {
    document.querySelectorAll('.reveal').forEach(function (el) { el.classList.add('in'); });
  }

  /* ---------- demo data (mock) ---------- */

  var CLAIMS = [
    { id: 'claim/deploy-tagged-commit', text: 'Production deploys run `make release` from a tagged commit.', src: 'RUNBOOK.md', hash: '9f31c4a8…e2', links: [{ to: 'source/runbook', type: 'cites' }, { to: 'entity/deploy', type: 'mentions' }], auto: 'approve', why: 'matches runbook', by: 'alice-example' },
    { id: 'claim/skip-migration-tests', text: 'Skipping migration tests is safe on hotfix branches.', src: '(no source cited)', hash: null, links: [], auto: 'reject', why: 'contradicts RUNBOOK.md §4', by: 'alice-example' },
    { id: 'claim/use-pnpm', text: 'Use pnpm, not npm — the lockfile is pnpm-lock.yaml.', src: 'CONTRIBUTING.md', hash: 'c58a91d0…19', links: [{ to: 'source/contributing', type: 'cites' }, { to: 'entity/tooling', type: 'mentions' }] },
    { id: 'claim/staging-db-reset', text: 'The staging database resets nightly at 02:00 UTC.', src: 'infra/cron.yaml', hash: '41bc77aa…7d', links: [{ to: 'source/cron', type: 'cites' }, { to: 'entity/staging', type: 'mentions' }] },
    { id: 'claim/tokens-rotate-90d', text: 'API tokens rotate every 90 days.', src: 'SECURITY.md', hash: '73de10fc…4b', links: [{ to: 'source/security', type: 'cites' }, { to: 'page/deployment', type: 'supports' }] },
    { id: 'claim/payments-own-retries', text: 'The payments service retries webhooks itself — clients must not.', src: 'services/payments/README.md', hash: 'e814a2c9…33', links: [{ to: 'entity/payments', type: 'mentions' }, { to: 'source/payments-readme', type: 'cites' }] }
  ];

  /* seed graph: sources (squares), entities (diamonds), a page (rect) */
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

  /* ---------- graph canvas ---------- */

  var canvas = document.getElementById('kgraph');
  var ctx = canvas ? canvas.getContext('2d') : null;
  var nodes = [], edges = [], tick = 0;

  var COLORS = {
    claim: '#43c463',
    source: '#a39d90',
    entity: '#7fb4e8',
    page: '#e0a32e',
    edge: 'rgba(237,232,220,0.16)',
    edgeFresh: 'rgba(67,196,99,0.55)',
    label: 'rgba(163,157,144,0.85)',
    type: 'rgba(110,106,96,0.9)'
  };

  function sizeCanvas() {
    if (!canvas) return;
    var r = canvas.getBoundingClientRect();
    var dpr = window.devicePixelRatio || 1;
    canvas.width = r.width * dpr;
    canvas.height = r.height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function seedGraph() {
    var r = canvas.getBoundingClientRect();
    var cx = r.width / 2, cy = r.height / 2;
    nodes = SEED_NODES.map(function (n, i) {
      var angle = (i / SEED_NODES.length) * Math.PI * 2 + 0.6;
      var rad = n.type === 'page' ? 0 : (n.type === 'claim' ? Math.min(r.width, r.height) * 0.31 : Math.min(r.width, r.height) * 0.42);
      return {
        id: n.id, label: n.label, type: n.type,
        x: cx + Math.cos(angle) * rad,
        y: cy + Math.sin(angle) * rad * 0.8,
        tx: cx + Math.cos(angle) * rad,
        ty: cy + Math.sin(angle) * rad * 0.8,
        born: -100, phase: Math.random() * Math.PI * 2,
        labelUp: i % 2 === 0
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
    /* target: ring between page and outer ring, angle from count */
    var claimCount = nodes.filter(function (n) { return n.type === 'claim'; }).length;
    var angle = claimCount * 2.4 + 1.2;
    var rad = Math.min(r.width, r.height) * 0.31;
    var node = {
      id: claim.id,
      label: claim.id.replace('claim/', ''),
      type: 'claim',
      x: 12, y: cy,             /* enters from the queue side */
      tx: cx + Math.cos(angle) * rad,
      ty: cy + Math.sin(angle) * rad,
      born: tick, phase: Math.random() * Math.PI * 2,
      labelUp: claimCount % 2 === 1
    };
    nodes.push(node);
    claim.links.forEach(function (l) {
      if (nodeById(l.to)) edges.push({ a: claim.id, b: l.to, type: l.type, born: tick });
    });
  }

  function drawNode(n, wob) {
    var x = n.x + Math.sin(tick / 90 + n.phase) * wob;
    var y = n.y + Math.cos(tick / 110 + n.phase) * wob;
    var appear = Math.min(1, (tick - n.born) / 30);
    ctx.globalAlpha = appear;
    ctx.fillStyle = COLORS[n.type];
    if (n.type === 'claim') {
      var rr = 6 + (1 - appear) * 6;
      ctx.beginPath(); ctx.arc(x, y, rr, 0, Math.PI * 2); ctx.fill();
      if (tick - n.born < 60 && tick - n.born > -50) {  /* fresh ring */
        ctx.strokeStyle = 'rgba(67,196,99,' + (0.6 * (1 - (tick - n.born) / 60)) + ')';
        ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.arc(x, y, 6 + (tick - n.born) / 4, 0, Math.PI * 2); ctx.stroke();
      }
    } else if (n.type === 'source') {
      ctx.fillRect(x - 5, y - 5, 10, 10);
    } else if (n.type === 'entity') {
      ctx.save(); ctx.translate(x, y); ctx.rotate(Math.PI / 4);
      ctx.fillRect(-5, -5, 10, 10); ctx.restore();
    } else { /* page */
      ctx.beginPath();
      if (ctx.roundRect) { ctx.roundRect(x - 8, y - 6, 16, 12, 3); } else { ctx.rect(x - 8, y - 6, 16, 12); }
      ctx.fill();
    }
    ctx.font = '10px "Plex Mono", monospace';
    ctx.fillStyle = COLORS.label;
    ctx.textAlign = 'center';
    var label = n.label.length > 16 ? n.label.slice(0, 15) + '…' : n.label;
    ctx.fillText(label, x, n.type === 'page' ? y + 24 : (n.labelUp ? y - 13 : y + 20));
    ctx.globalAlpha = 1;
    return { x: x, y: y };
  }

  function render() {
    if (!ctx) return;
    var r = canvas.getBoundingClientRect();
    ctx.clearRect(0, 0, r.width, r.height);
    tick++;
    var wob = REDUCED ? 0 : 2.2;

    /* ease nodes toward targets */
    nodes.forEach(function (n) {
      n.x += (n.tx - n.x) * 0.06;
      n.y += (n.ty - n.y) * 0.06;
    });

    /* edges first */
    edges.forEach(function (e) {
      var a = nodeById(e.a), b = nodeById(e.b);
      if (!a || !b) return;
      var fresh = tick - e.born < 70;
      ctx.strokeStyle = fresh ? COLORS.edgeFresh : COLORS.edge;
      ctx.lineWidth = fresh ? 1.6 : 1;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
      if (e.type && (fresh || edges.indexOf(e) < 3)) {
        ctx.font = '9px "Plex Mono", monospace';
        ctx.fillStyle = COLORS.type;
        ctx.textAlign = 'center';
        ctx.fillText(e.type, (a.x + b.x) / 2, (a.y + b.y) / 2 - 4);
      }
    });

    nodes.forEach(function (n) { drawNode(n, n.type === 'page' ? 0 : wob); });

    if (!REDUCED || tick < 120) requestAnimationFrame(render);
  }

  /* ---------- ledger ---------- */

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

  /* ---------- review queue ---------- */

  var queueEl = document.getElementById('queue-cards');
  var countEl = document.getElementById('queue-count');
  var yourTurnEl = document.getElementById('your-turn');
  var qIndex = 0;         /* next claim not yet rendered */
  var pendingLeft = CLAIMS.length;
  var VISIBLE = 2;
  var autoplayTimer = null;
  var userActed = false;

  function shortSrc(c) {
    return c.hash
      ? c.src + ' <span class="hash">sha256:' + c.hash + '</span>'
      : '<span style="color:var(--rejected)">' + c.src + '</span>';
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
    /* prevent double decisions */
    if (card.dataset.done) return;
    card.dataset.done = '1';
    pendingLeft--;

    var mark = card.querySelector('.stamp-mark');
    mark.textContent = approved ? 'APPROVED' : 'REJECTED';
    mark.className = 'stamp-mark show ' + (approved ? 'ok' : 'no');
    card.style.borderLeftColor = approved ? 'var(--approved)' : 'var(--rejected)';

    var delay = REDUCED ? 60 : 620;
    setTimeout(function () {
      card.classList.add(approved ? 'leaving-approved' : 'leaving-rejected');
      if (approved) addClaimNode(claim);
      ledgerLine(approved ? 'approve' : 'reject', claim.id, why || claim.why, by || claim.by || 'you', true);
      setTimeout(function () {
        card.remove();
        fillQueue();
      }, REDUCED ? 30 : 380);
    }, delay);
  }

  function userDecide(card, claim, approved) {
    userActed = true;
    if (autoplayTimer) { clearTimeout(autoplayTimer); autoplayTimer = null; }
    yourTurnEl.classList.remove('show');
    decide(card, claim, approved, 'you', approved ? 'stamped from vouchai.dev' : 'stamped from vouchai.dev');
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

  /* ---------- boot ---------- */

  function boot() {
    if (!canvas) return;
    sizeCanvas();
    seedGraph();
    render();

    /* seed the ledger with history */
    ledgerLine('propose', 'claim/deploy-tagged-commit', null, 'agent:claude-code');
    ledgerLine('propose', 'claim/skip-migration-tests', null, 'agent:claude-code');
    ledgerLine('approve', 'claim/rollback-by-tag', 'verified against RUNBOOK.md', 'alice-example');

    fillQueue();

    /* start autoplay when the console scrolls into view */
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
      sizeCanvas();
      /* retarget nodes relative to new size */
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

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
