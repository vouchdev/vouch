# Effectiveness schema

`vouch health effectiveness --format json` and `kb.effectiveness` share one
stable machine-readable schema.

## Top-level fields

- `schema_version` (int): schema revision for compatibility checks.
- `window` (object): `{spec, since, until, generated_at}`.
- `min_samples` (int): minimum surfaced samples required for confident verdicts.
- `sessions` (object): `{classified, good, bad, baseline_rate}`.
- `artifacts` (array): ranked per-artifact rows.

## Artifact row

- `artifact_kind` (string)
- `artifact_id` (string)
- `samples` (int): surfaced sessions with classified outcomes.
- `surfaced` (object): `{good, bad}` counts for surfaced sessions.
- `not_surfaced` (object): `{good, bad}` counts for classified sessions where
  this artifact was not surfaced.
- `rate` (float): surfaced good rate.
- `baseline_rate` (float): global good rate across classified sessions.
- `lift` (float): `rate - baseline_rate`.
- `wilson_95` (object): `{low, high}` confidence interval on surfaced good rate.
- `verdict` (string): one of `useful`, `harmful`, `unverified`, `insufficient`.
- `earned_value` (float): `lift * samples`, used for ranking.

## Verdict rules

- `insufficient`: `samples < min_samples`
- `useful`: interval lower bound is greater than baseline
- `harmful`: interval upper bound is lower than baseline
- `unverified`: otherwise
