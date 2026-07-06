# benchmarks/

Performance fixtures for vouch. The intent is to keep this minimal and
honest — vouch is not a search engine and we are not chasing milliseconds
against tools that are. But there are a few numbers that *do* matter:

- **Search latency** as the KB grows. FTS5 is fast on small KBs and
  fine on large ones; we want a published curve.
- **Proposal write latency.** This sits in the agent's hot loop. If it
  ever climbs past ~50ms on a warm SSD, something regressed.
- **Bundle import time.** Imports gate cross-team KB sharing; a 10k-claim
  bundle should land in seconds, not minutes.
- **Index rebuild time** at fixed KB sizes (1k / 10k / 100k claims).

## Status

Implemented. All four `bench_*.py` files run under `pytest-benchmark`.
See [ROADMAP.md](../ROADMAP.md) (0.3) for the surrounding milestone. The
100k fixture in `conftest.py` exists but no bench file exercises it yet.

## Baseline

First recorded run. This is a single developer-machine snapshot, **not**
a published environment per the methodology below — treat it as
order-of-magnitude, not gospel. Medians, warm:

| Benchmark | 1k claims | 10k claims |
|---|---|---|
| `search_fts5` (FTS5 query) | 0.46 ms | 1.65 ms |
| `search_substring` (fallback scan) | 241 ms | 2.42 s |
| `propose_claim` (hot-loop write) | 2.40 ms | — |
| `index_rebuild` | 311 ms | 7.25 s |
| `bundle_export` | 113 ms | — |
| `bundle_export_check` | 48.6 ms | — |
| `bundle_import` | 1.05 s | 12.4 s |

What the numbers say:

- **FTS5 search stays fast and scales sub-linearly** (~3.6x for 10x the
  claims). The substring fallback reads every claim file, so it's ~500x
  slower — that's the whole reason it's only a fallback.
- **`propose_claim` medians 2.4 ms**, comfortably under the ~50ms hot-loop
  budget noted above.
- **A 10k-claim bundle imports in 12.4 s** — seconds, not minutes, which
  is the bar this benchmark was written to guard.

Environment: 13th Gen Intel Core i9-13900K (16 threads), ~22 GB RAM,
Python 3.14, vouch 1.0.0. Full per-run detail (min/max/stddev, machine
info) lands in `bench.json`.

## Layout

```
benchmarks/
├── README.md                  (you are here)
├── conftest.py                pytest-benchmark configuration + seeded KB fixtures
├── fixtures/
│   └── gen_kb.py              synth a KB of N claims with realistic distributions
├── bench_search.py            kb.search latency at varying KB sizes
├── bench_propose.py           kb.propose_* write latency
├── bench_bundle.py            export + import + verify round-trips
└── bench_index_rebuild.py     kb.index_rebuild at varying sizes
```

Benchmarks live outside `tests/` so a regular `pytest` run doesn't
pull them in. `pytest-benchmark` isn't in the `[dev]` extras, and the
`bench_*.py` filenames don't match pytest's default `python_files`
glob — so the invocation needs both an install and a collection
override:

```bash
pip install pytest-benchmark
pytest benchmarks/ --benchmark-only \
    -o python_files='bench_*.py test_*.py' \
    --benchmark-json=bench.json
```

`make bench` is not wired in the Makefile yet; when it is, it should
fold in the `python_files` override so this isn't a footgun.

## Methodology principles

- **Real disks.** No tmpfs benchmarks. The file-based design makes
  tmpfs misleadingly fast.
- **Cold and warm.** Report both; FTS5's first query after open is
  meaningfully slower than the second.
- **Reproducible fixtures.** `gen_kb.py` is seeded; the same seed
  produces the same KB.
- **Published environment.** Every benchmark run records CPU, RAM,
  disk model, and vouch version in the result JSON.

## What we explicitly are *not* benchmarking

- Semantic quality. That's a *correctness* concern, not a performance
  one; it belongs in [docs/](../docs/) and in the conformance suite
  (see ROADMAP 0.2).
- Comparison against other KB tools. We're not racing mem0. Speak for
  yourself, mem0.

## Contributing benchmarks

If you have a workload that stresses vouch in a way these don't
capture, please file a VEP describing the scenario rather than just
adding a `bench_*` file — we want the benchmark suite to be small and
intentional.
