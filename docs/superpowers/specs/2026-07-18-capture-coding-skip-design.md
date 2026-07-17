# coding-content skip for passive answer-memory — design

## goal

give passive answer-memory a selectivity rule modelled on the claude.ai
memory system's "never store generic technical questions": when a session's
answer is *purely coding work* — a code dump, a diff, a generic "how do i
write X" — don't turn it into durable, recallable knowledge. keep saving the
answers that carry durable or decision-type knowledge, including decisions
*about* code.

opt-in, off by default. mechanical, no LLM. capture-side only.

## why this shape (the constraints that pin it down)

- **the memory analogue is `capture_answer`, not the activity log.** vouch has
  two capture paths. the observation buffer (`observe` / `summarize_tool` →
  session page) is by design a dev-activity log — "Edited foo.py", "Ran:
  pytest" — and a session page is *supposed* to be full of coding activity.
  the passive answer-memory path (`capture_answer`) is the one analogous to a
  recallable memory. so the coding-skip gates the answer path and leaves the
  activity log untouched. filtering the activity log would gut the feature,
  not refine it.
- **no LLM.** capture's load-bearing invariant is "passive harvest →
  mechanical rollup → one PENDING proposal, no LLM." the classifier must be a
  pure mechanical function, same discipline as `secrets.mask_secrets`.
- **reduce-only, never a new write path.** the rule can only make
  `capture_answer` skip. it never writes, never approves, never touches
  `proposals.approve`. the review gate is untouched — north-star safe.
- **dropping a real answer is the expensive error.** false-negatives (a code
  answer that slips through and gets captured) are cheap; a wrongly-dropped
  decision is not. the classifier biases toward keeping when uncertain, and
  the whole rule ships default-off.

## what already exists (do not rebuild)

- **`capture_answer`** (`src/vouch/capture.py`): the Stop-hook entry point that
  turns the last exchange into a source + receipt-backed claims. already has a
  clean skip ladder — `disabled-env`, `disabled`, `no-answer`,
  `answer-too-short`, `already-captured` — each returning via `_answer_skip`
  with shape `{"captured": false, "skipped": <reason>, ...}`.
- **`CaptureConfig` + `load_config`**: reads the `capture:` block of
  `config.yaml` into a frozen dataclass with defaults. adding a field is a
  two-line change (dataclass field + `raw.get`).
- **`secrets.mask_secrets`**: the precedent for a pure, conservative,
  mechanical content filter that runs inside capture. the classifier follows
  its house style — curated high-precision signals, not entropy/ML.

the only missing piece is the classifier and one guard call. that is this
feature.

## approach

add a pure classifier and gate `capture_answer` on it.

### 1. new module `src/vouch/capture_filters.py`

one public function, no I/O, no LLM:

```python
def is_coding_answer(question: str, answer: str) -> bool:
    """True when the answer is coding-dominant AND carries no durable signal.

    Conservative by construction: biases toward False (keep) whenever the
    signal is weak, because wrongly dropping a decision costs more than
    keeping a stray code answer.
    """
```

**coding signals** (push toward skip):

- fenced-code ratio: characters inside ``` fences ÷ total characters.
- code-line fraction: non-empty lines matching code-ish shape (leading
  indentation + symbols; `def `/`class `/`import `/`function `/`const `/
  trailing `{`/`;`/`</`).
- diff/patch markers: `@@ `, `+++ `/`--- `, runs of leading `+`/`-`.
- file-path / extension density: `\b[\w./-]+\.(py|ts|js|tsx|go|rs|rb|java|
  c|cpp|sh|yaml|toml|json)\b` hits per 100 words.
- shell markers: leading `$ `, `pip install`, `npm `, `git `, `sudo `.

**durable-signal override** (force keep even if code-heavy):

- rationale/decision lexicon: `decided`, `chose`, `instead of`, `because`,
  `the reason`, `trade-?off`, `we should`, `going with`, `gotcha`, `lesson`,
  `prefer`, `avoid`, `so that`.
- high prose-to-code ratio: substantial natural-language explanation
  surrounding any code (the answer is *about* code, not *just* code).

**decision rule:** `is_coding_answer` returns True iff the combined coding
score clears a deliberately high threshold **and** no durable signal fired.
thresholds are constants at module top, tuned to bias toward keep. the
function is written generically enough that a future `skip_topics` denylist
(see out-of-scope) reuses it rather than replacing it.

### 2. wire it into `capture.py`

- `CaptureConfig`: add `skip_coding: bool = False`.
- `load_config`: `skip_coding=bool(raw.get("skip_coding", False))`.
- `capture_answer`: immediately after the `answer-too-short` check, before the
  content is hashed and sourced:

  ```python
  if cfg.skip_coding and is_coding_answer(question, answer):
      return _answer_skip(session_id, "coding-content")
  ```

  new reason `"coding-content"` joins the existing ladder; no new return shape.

### 3. config surface

document `capture.skip_coding` (default `false`) wherever the `capture:` block
is described. no schema/pydantic model change — `CaptureConfig` is a plain
dataclass read by `load_config`.

## data flow

```
Stop hook → capture_answer
  ├─ VOUCH_CAPTURE_DISABLE?        → skipped: disabled-env
  ├─ cfg.enabled?                  → skipped: disabled
  ├─ last_exchange none?           → skipped: no-answer
  ├─ len(answer) < min?            → skipped: answer-too-short
  ├─ cfg.skip_coding & coding?     → skipped: coding-content   ← new
  ├─ already sourced?              → skipped: already-captured
  └─ put_source → extract claims → gated approve
```

## not in scope (YAGNI — deferred, not rejected)

- **general `skip_topics` denylist.** the classifier is written reusably so
  this is a later extension, not a rewrite. not built now — coding is the only
  topic asked for.
- **recall-side filtering.** the claude.ai analogue also refuses to *apply*
  memories to generic technical questions. this feature is capture-only; the
  read path is unchanged.
- **user-editable exclusion control** (the `memory_user_edits` analogue —
  "exclude coding topics" as a durable, user-owned rule). natural follow-on to
  `skip_topics`; out of scope here.

## testing

- **`tests/test_capture_filters.py`** (new) — unit tests for
  `is_coding_answer`:
  - pure fenced code block → True
  - unified diff dump → True
  - shell-session transcript → True
  - decision-about-code ("we chose X over Y because …", with a snippet) → False
  - code with substantial surrounding rationale → False
  - plain prose answer → False
  - empty / whitespace / very short → False
- **`tests/test_capture.py`** (or the passive-answer test module) — integration:
  - `skip_coding=True` + a coding answer → returns `skipped="coding-content"`
    and **no source is written** (`store.get_source` still raises).
  - `skip_coding=True` + a decision answer → captured as normal.
  - **regression guard:** default config (`skip_coding=False`) captures a
    coding answer exactly as today — the opt-in default must not change
    existing behaviour.

## rollout

single feature branch off `test`, one commit. no capabilities/method-surface
change (this is internal config + a filter, not a new `kb.*` method), so the
four-site registration dance and `test_capabilities` do not apply.
