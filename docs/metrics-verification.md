# Metrics implementation verification — 2026-10-28

This records the initial implementation and smoke. The subsequent
[pre-pilot review](metrics-pilot-readiness.md) adds pagination regressions/fix,
HTTP scope/timeout verification, B timeline analysis and current pilot blockers.
Initial test counts and raw smoke measurements below remain historical evidence.
The 2026-10-29 local commit organization and isolated validation are recorded in
[metrics-commit-review.md](metrics-commit-review.md).

Work branch: `feature/metrics-comparison`. No commits, merges or pushes had been
made at the initial verification stage. Existing untracked user files were left intact. Main/A/B business code
was not changed. The three detached worktrees remain under
`/tmp/intro2ds-metrics-checkouts/{b0,a,b}` for inspection.

## Work completed

Followed Analyze → Plan → Implement → Test → Review → Report. Analysis and
measurement semantics are in [metrics-comparison.md](metrics-comparison.md).
The implementation reuses existing metrics/validation, adds an output adapter
and isolated runner, exercises edge cases offline, checks real outputs, and
exports reports. Architecture details and complete CPU-only Colab cells are
in that document.

| File | Purpose |
|---|---|
| `src/metrics.py` | Pair timed events independently of serialization order; accept successful normalized-empty references; reject nonstandard NaN/Infinity JSONL |
| `src/tools/compare_runs.py` | Read canonical output; validate schema/content; summarize; gate speedups; export CSV/JSON/Markdown |
| `src/tools/benchmark_run.py` | Freeze common IDs/configuration; record clean Git revision, interpreter/dependencies, policy and cache provenance; measure subprocess resources |
| `src/tools/demo_comparison.py` | Generate explicitly synthetic output fixtures and example reports |
| `configs/metrics_smoke.yaml` | Shared real smoke policy: timeout 15 s, one retry, arXiv delay 3 s |
| `configs/metrics_smoke_ids.txt` | Two IDs taken from the common pilot |
| `tests/test_metrics.py` | Remove the old assertion that contradicted the crawler's normalized-empty contract |
| `tests/test_comparison.py` | Cross-run statistics, quality, content, malformed/missing records, concurrency, validity gates and CLI integration |
| `tests/test_benchmark_run.py` | Frozen configuration/IDs, algorithm controls, warm cache guards, secret exclusion and real local subprocess measurement |
| `docs/metrics-comparison.md` | Repository findings, measurement definitions, Colab instructions, export commands and coordination issues |
| `docs/metrics-verification.md` | Actual execution evidence and limitations |
| `docs/examples/metrics-synthetic/comparison.{json,csv,md}` | Small generated **synthetic** report artifacts; invented times are never presented as real |
| `docs/benchmark.md`, `docs/metrics-integration.md` | Link the implemented workflow and clarify reference semantics |
| `README.md` | Discoverability; correct Python requirement to >=3.11 for current pinned dependencies |

## Test execution

The existing `.venv/bin/python` symlink targets an absent interpreter.
System Python is 3.10.12 without pytest/pip, and the pinned `kagglehub==1.0.3`
requires Python >=3.11. A separate Python 3.12.14 interpreter and virtualenv
were installed under `/tmp`; the existing `.venv` was preserved. Requirements
were installed unchanged. Pytest version: 9.1.1; psutil: 7.2.2.

Actual final commands, from the repository root:

```bash
/tmp/intro2ds-metrics-py312/bin/python -m pytest \
  tests/test_metrics.py tests/test_metrics_integration.py \
  tests/test_comparison.py tests/test_benchmark_run.py -q --tb=short
# 333 passed in 1.20s

/tmp/intro2ds-metrics-py312/bin/python -m pytest -q --tb=short
# 714 passed, 121 subtests passed

git diff --check
# Exit 0
```

An earlier fixture test run failed with 274 passes and 26 setup errors: the
new synthetic generator omitted the required `error_message` callback argument.
The fixture was corrected; the final tests above pass. Initial attempts to use
the broken `.venv` and system pytest could not execute tests and are not counted
as successful validation.

Covered cases include completed/interrupted/crashed runs, explicit paper
failures, unknown metrics/versions, failed S2 404 versus valid empty references,
pending/running/skipped outcomes, multiple 429 attempts/retries, differing IDs
or order, differing environments/policies/cold-warm modes, dirty revisions,
invalid JSON/JSONL and provenance shapes, start-only/unfinished operations,
unordered start/finish records, duplicate attempts, stale schema validation,
missing source files, reference ordering and multiplicity, unassigned output
folders, CSV/JSON/Markdown exports, CLI exit 2 for invalid benchmark conditions,
warm seed provenance and rejection of implicit runs larger than 40 papers.

## Synthetic integration example

Executed:

```bash
/tmp/intro2ds-metrics-py312/bin/python -m src.tools.demo_comparison \
  --output experiments/metrics-comparison-synthetic-v1
```

Small reviewable exports are retained in
[Markdown](examples/metrics-synthetic/comparison.md),
[CSV](examples/metrics-synthetic/comparison.csv) and
[JSON](examples/metrics-synthetic/comparison.json).
The invented times 12/8/6 seconds yield 1.5×/2× arithmetic test speedups.
These artifacts contain no real paper content or performance measurements.

## Real API smoke integration

After successful remote fetch, clean target worktrees were created from the
three revisions documented in `metrics-comparison.md`. An arXiv API probe
returned HTTP 200 outside the network-restricted sandbox. Each pipeline then
ran sequentially with the same interpreter, dependencies, unauthenticated S2
mode, IDs `2505.16300` / `2505.16447`, request policy, sampling interval 0.2 s
and a new cold run ID. Commands actually used:

```bash
/tmp/intro2ds-metrics-py312/bin/python -m src.tools.benchmark_run \
  --repo /tmp/intro2ds-metrics-checkouts/b0 --variant B0 \
  --ids configs/metrics_smoke_ids.txt --template configs/metrics_smoke.yaml \
  --output-root experiments/metrics-smoke

# Same command for --repo /tmp/intro2ds-metrics-checkouts/a --variant A
# Same command for --repo /tmp/intro2ds-metrics-checkouts/b --variant B
```

All three commands exited 0. Run IDs:

- `B0_cold_20261028T160725Z_28c437285b`
- `A_cold_20261028T160812Z_b7bab4aeef`
- `B_cold_20261028T160902Z_5c9237f8f5`

Real measured results, rounded for presentation:

| Metric | B0 | A | B |
|---|---:|---:|---:|
| External wall time, seconds | 44.988 | 48.080 | 108.457 |
| Mean paper operation, seconds | 22.410 | 23.295 | 73.752 |
| Peak RSS, bytes | 45,240,320 | 44,466,176 | 48,050,176 |
| Time-weighted mean RSS, bytes | 42,933,655 | 43,997,901 | 44,956,806 |
| Sampled peak disk, bytes | 3,699,496 | 3,151,176 | 3,163,291 |
| Final papers output, bytes | 59,826 | 59,826 | 59,826 |
| Complete / failed / unresolved papers | 2 / 0 / 0 | 2 / 0 / 0 | 2 / 0 / 0 |
| Expected / processed versions | 2 / 2 | 2 / 2 | 2 / 2 |
| Version coverage | 100% | 100% | 100% |
| Reference retrieval success | 100% | 100% | 100% |
| Valid normalized references | 31 | 31 | 31 |
| Observed callback 429 / retries | 0 / 0 | 0 / 0 | 0 / 0 |
| Schema status | ok | ok | ok |
| B0 wall / variant wall | — | 0.9357 | 0.4148 |
| Paper workers / discovery batch size | 1 / 1 | 1 / 20 | 2 / 1 |

Metadata, expected version lists, source content and normalized reference
multisets all matched for both papers across all variants. The comparison
reported `benchmark_valid=true` for the recorded conditions. Real evidence
(including run/event records, source files and console output) remains under
the Git-ignored `experiments/metrics-smoke/` directory.

Actual comparison command:

```bash
/tmp/intro2ds-metrics-py312/bin/python -m src.tools.compare_runs \
  --baseline experiments/metrics-smoke/B0_cold_20261028T160725Z_28c437285b \
  --a experiments/metrics-smoke/A_cold_20261028T160812Z_b7bab4aeef \
  --b experiments/metrics-smoke/B_cold_20261028T160902Z_5c9237f8f5 \
  --output experiments/metrics-smoke/report
# Exit 0, comparison.json / comparison.csv / comparison.md written
```

## Limits and next execution

This was one local CPU smoke triplet on WSL2, not a Colab benchmark or repeated
statistical comparison. Wall time is sensitive to API/download variability.
These two-paper numbers do not establish a general speed ranking. In
particular, B's summed paper time exceeds total wall time because worker
operations overlap; it is not a substitute for pipeline wall time.

Zero callback 429/retries does not establish zero total service retries:
`arxiv.Client` internal attempts are not instrumented. Multi-page references
also need a distinct request hook per page upstream. These limitations are
reported explicitly; no missing totals were invented.

No real warm run, 40-paper pilot or 1,000-paper benchmark was executed. The
final performance target is Colab CPU-only; repeat the smoke and verify the
environment there before the 40-paper pilot. Independent preparation manifests
and complete per-attempt HTTP instrumentation still require coordination with
the A/B owners. Use the Colab cells in `metrics-comparison.md` for that handoff.
