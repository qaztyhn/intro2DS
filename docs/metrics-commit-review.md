# Local Metrics commit organization — 2026-10-29

The existing Metrics work is organized into four functional commits. The target
branch is `feature/metrics-comparison`; no push, merge or A/B algorithm change
is part of this operation. Initial implementation/smoke and pre-pilot test
counts in the other verification documents remain historical records.

## Commit boundaries

| Order | Commit subject | Files and responsibility |
|---|---|---|
| 1 | `fix(metrics): handle concurrent events and HTTP request tracking` | `src/metrics.py`, `src/utils/http.py`, `tests/test_metrics.py`, `tests/test_request_scope.py`, `tests/test_http_observations.py`: independent metric/HTTP fixes and their unit/transport regressions |
| 2 | `feat(metrics): implement cross-variant comparison and reporting` | `src/tools/benchmark_protocol.py`, `src/tools/compare_runs.py`, `src/tools/demo_comparison.py`, `tests/test_comparison.py`, `tests/test_pilot_readiness.py`: report contract, synthetic fixtures, comparisons and 40-paper mixed-outcome integration |
| 3 | `feat(benchmark): add reproducible runner and performance analysis` | `src/tools/benchmark_run.py`, `src/tools/analyze_timeline.py`, both `configs/metrics_smoke*` files, `tests/test_benchmark_run.py`, `tests/test_timeline.py`: execution/provenance/preflight guards, resource/timeline analysis and tests |
| 4 | `docs(metrics): add benchmark protocol and verification reports` | README, benchmark/integration instructions, verification/pre-pilot/commit-review documents, small synthetic reports and the reviewable common request-scope patch |

Each file belongs to one new commit. The shared constants were extracted from
the runner into `benchmark_protocol.py` because comparison fixtures otherwise
imported a future runner commit. Pure HTTP observation tests were moved out of
the mixed pilot test file into commit 1; the effective-timeout probe test moved
with runner tests into commit 3. Their assertions were preserved. A standalone
unordered-event regression and three nonfinite-JSON cases were added alongside
the core fixes so commit 1 independently tests those behaviors.

The remaining `test_pilot_readiness.py` exercises the comparison's 40-paper
mixed outcomes; runner-specific readiness tests live in
`test_benchmark_run.py`. No file is repeatedly staged across groups.

## Validation of committed contents

After each code commit, `git archive HEAD` was extracted into a fresh directory
under `/tmp/intro2ds-commit-validation/`. Tests ran from that snapshot using
Python 3.12.14, with no working-tree/untracked source on `PYTHONPATH`. This
checks that later uncommitted files do not mask missing dependencies.

| Commit | Related tests on its isolated snapshot | Actual result |
|---|---|---|
| `5bc895c` | metrics, metrics integration, HTTP, references, download/discovery metrics, request scope, HTTP observations | 350 passed, 121 subtests passed |
| `1bfb10f` | comparison, 40-paper comparison integration, existing benchmark helpers | 146 passed |
| `f16809f` | runner, timeline, 40-paper comparison integration, comparison | 70 passed |

The documentation commit is followed by the complete pytest suite on both its
committed snapshot and the original working tree, plus documentation/example
checks. The working-tree suite also includes the user's pre-existing untracked
`tests/test_m1_integration.py`; the committed snapshot intentionally excludes it.
Final command outputs and the four-commit log are reported in the handoff.

To reproduce the complete suite in an installed environment:

```bash
python -m pytest -q --tb=short
```

## Scope and data retention

Only explicitly named Metrics paths are staged. The existing non-Metrics files
`apply_d1_patch.py`, `docs/Intro2DS_Project.docx`,
`docs/Intro2DS_Project.docx:Zone.Identifier`, `docs/person2-m1.md`, and
`tests/test_m1_integration.py` remain untracked. Their SHA-256 hashes are checked
against their values before organization.

No `experiments/` artifacts, virtual environments, caches or credentials are
included. The small reports under `docs/examples/metrics-synthetic/` are
explicitly invented fixtures. Real smoke measurements are described in the
verification prose; raw corpus/log evidence stays Git-ignored locally.

Pilot readiness is unchanged: actual target revisions still need the common
request-scope fix and verified arXiv timeout policy, followed by a fresh Colab
CPU-only smoke. Commit organization does not execute any live crawl.
