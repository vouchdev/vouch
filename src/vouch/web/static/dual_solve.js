import { ref, reactive, onMounted } from "/static/vendor/vue.esm-browser.prod.js";
import { parseDiff, buildFileTree, flattenTree } from "/static/diff_view.js";

export default {
  setup() {
    const issueUrl = ref("");
    const claudeEffort = ref("high");
    const codexEffort = ref("high");
    const reason = ref("");
    const job = reactive({
      id: null, status: "idle", progress: [], candidates: [],
      issue: null, error: null, kept_branch: null, proposed_ids: [],
      changed_files: [], recommendation: null,
    });

    // engine -> selected file path in that candidate's file-changes view.
    // selection is per-candidate by design: picking a file in the claude pane
    // never moves the codex pane. lives outside applyState so polling doesn't
    // reset it.
    const selected = reactive({});

    function applyState(s) {
      Object.assign(job, s, {
        candidates: (s.candidates || []).map((c) => {
          const files = parseDiff(c.diff);
          return {
            ...c,
            files,
            rows: flattenTree(buildFileTree(files.map((f) => f.path))),
          };
        }),
      });
    }
    // selected file object for a candidate; falls back to the first changed
    // file when nothing is selected yet or the diff no longer has the path.
    function activeFile(c) {
      const want = selected[c.engine];
      return c.files.find((f) => f.path === want) || c.files[0] || null;
    }
    function selectFile(engine, path) {
      selected[engine] = path;
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
      job.kept_branch = null; job.proposed_ids = []; job.changed_files = [];
      job.recommendation = null;
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
      Object.assign(job, await r.json());
      await refresh();
    }

    onMounted(connectWs);
    return {
      issueUrl, claudeEffort, codexEffort, reason, job, run, choose,
      activeFile, selectFile,
    };
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
  <p v-if="job.recommendation && job.recommendation.reason" class="ds-recommendation">
    <strong>recommendation:</strong>
    <span v-if="job.recommendation.engine">{{job.recommendation.engine}}</span>
    <span v-else>no automatic pick</span>
    <span> -- {{job.recommendation.reason}}</span>
  </p>

  <div v-if="job.status==='ready'||job.status==='done'" class="ds-panes">
    <div v-for="c in job.candidates" :key="c.engine" class="ds-pane">
      <h2>{{c.engine}} <small>{{c.branch}}</small></h2>
      <p v-if="!c.ok" class="ds-error">failed: {{c.error}}</p>
      <details v-if="c.log" class="ds-log">
        <summary>{{c.engine}} log</summary>
        <pre>{{c.log}}</pre>
      </details>
      <div v-if="c.ok && c.files.length" class="fc">
        <div class="fc-rail">
          <template v-for="r in c.rows" :key="r.path">
            <div v-if="r.type==='tree'" class="fc-dir"
                 :style="{paddingLeft: (r.depth*10)+'px'}" :title="r.path">{{r.name}}/</div>
            <button v-else type="button" class="fc-file"
                    :class="{sel: activeFile(c) && activeFile(c).path===r.path}"
                    :style="{paddingLeft: (r.depth*10+4)+'px'}" :title="r.path"
                    @click="selectFile(c.engine, r.path)">{{r.name}}</button>
          </template>
        </div>
        <div class="fc-pane">
          <div v-if="activeFile(c)" class="ds-file">
            <div class="ds-file-head">{{activeFile(c).path}}</div>
            <pre><code><span v-for="(l,i) in activeFile(c).lines" :key="i" :class="'ln-'+l.cls">{{l.text + '\\n'}}</span></code></pre>
          </div>
        </div>
      </div>
      <p v-else-if="c.ok" class="fc-empty">(no file changes)</p>
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
    <div v-if="job.changed_files && job.changed_files.length">
      <p>changed files</p>
      <ul class="ds-changed-files">
        <li v-for="f in job.changed_files" :key="f">{{f}}</li>
      </ul>
    </div>
    <p v-for="pid in job.proposed_ids" :key="pid">
      proposed <a :href="'/'">{{pid}}</a> — review in the queue
    </p>
  </div>
</section>`,
};
