import { ref, reactive, onMounted } from "/static/vendor/vue.esm-browser.prod.js";

// Minimal unified-diff parser: groups lines per file with +/-/context classes.
function parseDiff(diff) {
  const files = [];
  let cur = null;
  for (const line of (diff || "").split("\n")) {
    if (line.startsWith("diff --git")) {
      const m = line.match(/ b\/(.+)$/);
      cur = { path: m ? m[1] : line, lines: [] };
      files.push(cur);
    } else if (!cur) {
      continue;
    } else if (line.startsWith("+++") || line.startsWith("---") ||
               line.startsWith("index ")) {
      continue;
    } else if (line.startsWith("@@")) {
      cur.lines.push({ cls: "hunk", text: line });
    } else if (line.startsWith("+")) {
      cur.lines.push({ cls: "add", text: line });
    } else if (line.startsWith("-")) {
      cur.lines.push({ cls: "del", text: line });
    } else {
      cur.lines.push({ cls: "ctx", text: line });
    }
  }
  return files;
}

export default {
  setup() {
    const issueUrl = ref("");
    const claudeEffort = ref("high");
    const codexEffort = ref("high");
    const reason = ref("");
    const job = reactive({
      id: null, status: "idle", progress: [], candidates: [],
      issue: null, error: null, kept_branch: null, proposed_ids: [],
    });

    function applyState(s) {
      Object.assign(job, s, {
        candidates: (s.candidates || []).map(
          (c) => ({ ...c, files: parseDiff(c.diff) })),
      });
    }
    async function refresh() {
      if (!job.id) return;
      const r = await fetch(`/dual-solve/job/${job.id}`);
      if (r.ok) applyState(await r.json());
    }
    function connectWs() {
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(`${proto}//${location.host}/ws`);
      ws.onmessage = (ev) => {
        let f;
        try { f = JSON.parse(ev.data); } catch { return; }
        if (f.type !== "dual_solve" || f.job_id !== job.id) return;
        if (f.event === "progress") job.progress.push(f.message);
        else refresh();   // ready/done/error -> pull the full state
      };
    }
    async function run() {
      job.progress = []; job.error = null; job.candidates = [];
      job.kept_branch = null; job.proposed_ids = [];
      const r = await fetch("/dual-solve/run", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          issue_url: issueUrl.value,
          claude_effort: claudeEffort.value,
          codex_effort: codexEffort.value,
        }),
      });
      if (!r.ok) { job.error = `run failed (${r.status})`; return; }
      job.id = (await r.json()).job_id;
      job.status = "running";
      await refresh();
    }
    async function choose(winner) {
      job.status = "finalizing";
      const r = await fetch("/dual-solve/choose", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: job.id, winner, reason: reason.value }),
      });
      if (!r.ok) { job.error = `choose failed (${r.status})`; return; }
      await refresh();
    }

    onMounted(connectWs);
    return { issueUrl, claudeEffort, codexEffort, reason, job, run, choose };
  },
  template: `
<section class="ds">
  <h1>dual-solve</h1>
  <form class="ds-run" @submit.prevent="run">
    <input v-model="issueUrl" placeholder="github issue url or owner/name#42"
           :disabled="job.status==='running'||job.status==='finalizing'" />
    <select v-model="claudeEffort"><option>low</option><option>medium</option><option selected>high</option><option>max</option></select>
    <select v-model="codexEffort"><option>low</option><option>medium</option><option selected>high</option><option>max</option></select>
    <button :disabled="!issueUrl||job.status==='running'||job.status==='finalizing'">run</button>
  </form>

  <p v-if="job.issue" class="ds-issue">#{{job.issue.number}} {{job.issue.title}}</p>
  <pre v-if="job.progress.length" class="ds-progress">{{ job.progress.join('\\n') }}</pre>
  <p v-if="job.error" class="ds-error">{{ job.error }}</p>

  <div v-if="job.status==='ready'||job.status==='done'" class="ds-panes">
    <div v-for="c in job.candidates" :key="c.engine" class="ds-pane">
      <h2>{{c.engine}} <small>{{c.branch}}</small></h2>
      <p v-if="!c.ok" class="ds-error">failed: {{c.error}}</p>
      <div v-for="f in c.files" :key="f.path" class="ds-file">
        <div class="ds-file-head">{{f.path}}</div>
        <pre><code><span v-for="(l,i) in f.lines" :key="i" :class="'ln-'+l.cls">{{l.text}}\\n</span></code></pre>
      </div>
    </div>
  </div>

  <div v-if="job.status==='ready'" class="ds-choose">
    <input v-model="reason" placeholder="one line: why this solution" />
    <button @click="choose('claude')">choose claude</button>
    <button @click="choose('codex')">choose codex</button>
    <button @click="choose(null)">keep neither</button>
  </div>

  <div v-if="job.status==='done'" class="ds-result">
    <p v-if="job.kept_branch">kept <code>{{job.kept_branch}}</code></p>
    <p v-for="pid in job.proposed_ids" :key="pid">
      proposed <a :href="'/'">{{pid}}</a> — review in the queue
    </p>
  </div>
</section>`,
};
