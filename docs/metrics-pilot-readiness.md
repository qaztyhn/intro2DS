# Pre-pilot review: references, HTTP coverage and B timeline

Review date: 2026-10-28. Branch: `feature/metrics-comparison`.
Test counts below describe that review. The later local commit organization and
validation are recorded in [metrics-commit-review.md](metrics-commit-review.md).
No A/B/main checkout was edited, committed, pushed or merged. No live pilot or
new live smoke was run. The 40-ID executions below were **preparation only**;
all new request/pagination integration tests use a mocked transport.

**Decision: not ready to launch the real 40-paper pilot yet.** Metrics handles
40-paper mixed outcomes offline, but the actual target revisions still need
the shared request-identity fix, a verified arXiv timeout policy, and a fresh
smoke in the intended Colab CPU-only runtime.

## Confirmed pagination defect and exact shared fix

The driver creates one metrics hook for each citing paper. References forwards
that same hook to every `request_json` call. Each page invokes a new
`request_with_retry` loop starting at attempt 1. Before this fix, two successful
pages therefore emitted the same `(request_id, 1)`. A retry on page two made
the sequence `1, 1, 2` under one ID; the strict summarizer correctly rejected
the duplicate. The same defect occurs if any caller reuses a hook for separate
downloads, and concurrent reuse cannot be repaired by watching attempt order.

The new regression file first ran with **4 failures**, confirming the problem
through real References and HTTP helper code with mocked GET responses.

The fix changes only the common hook/request layer on the Metrics branch:

- `make_request_hook` exposes an explicit `for_request()` factory. The root
  callback retains its supplied ID for direct callers.
- `request_with_retry` and `download_file` bind a child callback **once before
  entering the retry loop**. Each page/request gets a new UUID; all its retries
  retain that UUID. `request_group_id` retains the parent identity for grouping.
- Ordinary callbacks, including existing mock callbacks, retain the previous
  argument contract. A factory/callback failure does not trigger an HTTP retry.
- No change to References pagination, normalization, rate limiting, retries,
  service locks or the A/B algorithms is required for this identity fix.

The reviewable upstream patch is
[http-request-scope.patch](patches/http-request-scope.patch). It contains only
the hook factory and shared HTTP binding changes. It does **not** contain the
comparison tool, degraded-log aggregation, timeout policy or B algorithm changes.
`git apply --check` succeeded on all three actual target checkouts:

| Checkout | Commit | Patch check | Working tree |
|---|---|---|---|
| B0 | `4030b5b9cbe043deef559a6f3b9381927a360153` | exit 0 | clean |
| A | `0c7ffe0b5a40073d8c5f5d89d113adbf0f4f64b3` | exit 0 | clean |
| B | `dc8239237cad2d086631bfa2b024df05178f8d59` | exit 0 | clean |

Only the check was performed; the patch was not applied there. References and
A/B owners should review and integrate the same common change, run pagination
regressions, then supply the new clean revisions for the benchmark.

## Old/ambiguous logs and unknown HTTP totals

The strict `summarize_request_attempts` still rejects duplicate identities.
`summarize_http_observations` adds a reporting adapter around that validator:

- It never invents a page boundary, renumbers attempts, removes duplicate
  records, or interprets a repeated attempt 1 as a retry.
- Ambiguous groups make whole-stream observed request/attempt/retry/429
  aggregates `null`. A clearly labeled `validated_subset` preserves valid
  groups separately; `logged_attempt_record_count` counts log rows, not requests.
- Comparison retains paper/version/reference/schema outcomes and stage timing,
  emits an ambiguity warning and suppresses official speedups. It can still
  export all three reports for a partially successful pilot.
- Missing observations are `null`, not zero. Valid callbacks reporting no
  retries/429 can produce **observed** zero, while **total** counters remain
  `null`. CSV/Markdown expose both scopes explicitly.

HTTP instrumentation audit, based on local source and installed arxiv 4.0.2:

| Path | What is observed | What is missing |
|---|---|---|
| `/src/<id>vN` via `download_file` | One callback per helper attempt, including streaming/file replacement; retries keep one request ID | Redirect hops; DNS/connect/TLS/TTFB/stream/disk split |
| S2 reference pages via `request_json` | Per-attempt callback; per-page identity after common fix | Old duplicate page IDs; JSON parse/normalization time is outside attempt duration; redirect hops |
| arXiv abstract-page fallback via `request_text` | Shared per-attempt callbacks when the supplied hook is present | Redirect hops |
| arXiv initial metadata and intermediate revision queries through `client.results` | Discovery operation time and final business outcome | `arxiv.Client._session.get` calls, library retries and intermediate HTTP statuses |
| A batch metadata/revision queries | `discovery_batch` operation time | The same library-internal GETs/retries |
| B optional batch prefetch | Prefetch/business events where logged | The same library-internal GETs/retries; B prefetch does not supply metric operation/request hooks |

`arxiv.Client._parse_feed` retries HTTP errors, connection errors and unexpected
empty non-first pages inside the library. Its HTTP 200 response can therefore
still lead to a retry after feed validation. None of these attempts invokes the
project callback. A regression drives its actual retry code: the mocked
transport receives two GETs, while Metrics receives no request events and leaves
observed/total counts unknown. HTTP redirect hops also cannot be counted from
one final shared-helper callback.

There is another confirmed policy gap: `make_clients` passes delay and retry
count to `arxiv.Client`, but not the configured timeout. The installed client
calls `Session.get(url, headers=...)` without `timeout`; Requests' effective
timeout is `None`. A no-network capability probe intercepts `Session.send` to
verify the effective argument. The 15-second smoke / 30-second pilot settings
apply to shared helpers, not these library calls. Also, Requests' timeout is a
connect/read timeout, not a whole-download deadline: a successful 65-second
stream is not proof that its 15-second read timeout was ignored.

A/B/common discovery owners must agree an explicit connect/read timeout policy
for the arXiv transport and test it on all variants. A possible shared solution
is a Requests Session wrapper that forwards `cfg['timeout']` when the library
does not supply a timeout, installed in common `make_clients`; this depends on
the library's private session API and needs version-pinned regression tests.
It has not been installed into production code here. The preflight probe has
a positive offline test using such a wrapper in a **disposable synthetic source
copy**, proving that it can detect the configured timeout without network I/O.

Complete HTTP-attempt instrumentation remains separate upstream work. Until it
exists, `total_http_request_count`, `total_http_attempt_count`,
`total_http_retry_count` and `total_http_429_count` remain unknown in every run,
even when all recorded callbacks are valid. Do not reconstruct totals from
paper counts, discovery calls or business error events.

## Why B's smoke was slow: measured evidence and limits

The source records remain unchanged under `experiments/metrics-smoke/`.
The new analyzer generated `timeline-review/{B0,A,B}.json`, and refreshed
comparison exports are in `reviewed-report/`; the original `report/` is retained.

| Monotonic duration, seconds | B0 | A | B |
|---|---:|---:|---:|
| External pipeline wall | 44.987964 | 48.079564 | 108.456558 |
| Discovery operation sum, excluding A batch | 1.616598 | 0.000024 | 8.993073 |
| A batch discovery operation | — | 1.351947 | — |
| Download operation sum | 42.079377 | 46.548927 | 98.835025 |
| Observed source GET-attempt duration sum | 36.076877 | 43.544627 | 92.829385 |
| References operation sum | 1.071038 | 1.578379 | 0.897674 |
| Extraction operation sum | 0.032945 | 0.031340 | 0.029774 |

Both B source downloads are one observed successful attempt (HTTP 200):

| Paper | B0 source attempt | A source attempt | B source attempt |
|---|---:|---:|---:|
| `2505.16300` | 8.427308 s | 31.930185 s | 27.757477 s |
| `2505.16447` | 27.649568 s | 11.614442 s | 65.071908 s |

The observed source-attempt duration increases by **56.752508 seconds** from B0
to B. The full wall increase is **63.468594 seconds**. B source attempts alone
occupy 85.59% of B wall time. Discovery operation sum is also 7.376474 seconds
longer than B0, but no library-attempt breakdown exists for that difference.
The records provide no DNS/TLS/server-wait/body-throughput/disk split, so they
cannot explain *why* those transfers or discovery calls took longer.

B's checked-in `process_paper` holds the shared arXiv lock around **both
discovery and download**. The event ordering confirms these activities are
serialized: paper two's discovery starts immediately after paper one's download
finishes; its download starts after its own discovery. More paper workers do
not make these two downloads overlap under that algorithm. This is code/runtime
evidence of the concurrency boundary, not a change proposal for B.

Observed UTC stage chronology for B (same date, UTC):

| Paper | Stage | Start | End | Measured duration |
|---|---|---|---|---:|
| `2505.16300` | discovery | 16:09:02.961114 | 16:09:10.931654 | 7.970291 s |
| `2505.16300` | download | 16:09:10.932719 | 16:09:43.316229 | 30.760837 s |
| `2505.16447` | discovery | 16:09:43.316776 | 16:09:44.339885 | 1.022781 s |
| `2505.16300` | extract → references → storage | 16:09:43.318490 | 16:09:43.775472 | 0.455985 s (stage sum) |
| `2505.16447` | download | 16:09:44.341440 | 16:10:54.110460 | 68.074188 s |
| `2505.16447` | extract → references → storage | 16:10:54.111673 | 16:10:54.589659 | 0.476672 s (stage sum) |

The union of measured stages on different papers overlaps by approximately
**0.456173 seconds in UTC**. Paper two's recorded paper duration is 108.311 s,
but its measured stage durations sum to 69.573641 s, leaving **38.737359 s
unallocated**. The first paper's measured discovery+download sum is 38.731128 s.
Together with the lock's source-code boundary, this is consistent with a worker
waiting for the arXiv lane. It is not a direct lock-wait measurement. There are
no worker-start, lock-requested/acquired/released events; `lock_wait_s` is `null`.
Do not add that residual again to pipeline wall time or report it as exact wait.

The logs also expose a clock/boundary limitation: UTC start/end spans differ
materially from recorded monotonic durations. For example, B's second download
spans 69.769020 s by UTC timestamps but measures 68.074188 s; the first spans
32.383510 s but measures 30.760837 s. Similar discrepancies occur in B0/A.
The existing records cannot separate clock adjustments, logging/scheduling
overhead or measurement-boundary effects. Therefore the overlap above is only
a UTC diagnostic; precise lock/queue waits must not be inferred from timestamps.
Future instrumentation should record monotonic worker/lock/attempt boundaries.

The measured long source operations while the arXiv lane is held explain the
dominant part of this particular run's critical path. There is no recorded
evidence that references, extraction, CPU work or an observed HTTP 429 caused
the slowdown. Unknown internal retries are not evidence that retries occurred.
One two-paper triplet still cannot establish a general performance ranking.

Reproduce the evidence without API calls:

```bash
python -m src.tools.analyze_timeline \
  experiments/metrics-smoke/B_cold_20261028T160902Z_5c9237f8f5 \
  --output experiments/metrics-smoke/timeline-review/B.json
```

## Pilot readiness and preparation evidence

The three actual targets were prepared with the common 40 unique IDs from
`configs/pilot_ids.txt`, `configs/baseline_pilot.yaml`, one interpreter and
dependency set, fresh cold IDs and separate output directories. A uses batch
20 and one paper worker; B uses two workers and batch 1; B0 uses 1/1.
Config timeout/retry/arXiv/S2 policy values are common across the generated
configs. This generated configurations and provenance only, not paper outputs.
Evidence: `experiments/metrics-pilot-preflight-reviewed/preflight.json`.

| Check | Result |
|---|---|
| Same 40 IDs and input order | pass |
| Target Git commits match inspected branches; trees clean | pass |
| Offline comparison with 40 papers, pagination, HTTP 404 and a later-page failure | pass: 38 complete / 2 failed, all 40 retained |
| Export CSV/JSON/Markdown for mixed outcomes | pass |
| Log ambiguity/missing HTTP observations | explicit `null`, warning and validated subset; no invented totals |
| Actual B0/A/B per-request identity capability | fail on all three old target revisions |
| Configured arXiv library timeout | absent on all three targets |
| Colab CPU-only runtime and fresh smoke | not verified here (local WSL2 evidence only) |

The runner rechecks target revision/cleanliness, interpreter/dependencies,
credential presence, frozen YAML/IDs and instrumentation immediately before
launch. Drift raises an error before starting the pipeline. Runs of 40 or more
papers are blocked if request scoping, timeout policy or clean provenance is
unverified. `--prepare-only` can still produce reviewable configuration and the
full blocker list. A zero helper timeout is now rejected at preparation time.

The timeout capability probe is functional, not a declaration copied from
config: it invokes the target's client with a synthetic URL while replacing
Requests' `Session.send` with a sentinel that stops before network I/O. It
records the effective timeout passed to that boundary. It can recognize a
correctly configured transport once the common upstream fix exists.

Pending coordination:

1. References/A/B owners: integrate the provided shared identity patch on all
   three code lines and validate per-page retries / later-page 404 behavior.
2. Common discovery/A/B owners: establish and test the arXiv timeout policy;
   add complete per-attempt library instrumentation before claiming total HTTP
   or retry statistics. Keep internal callbacks/redirects' scope explicit.
3. B owner: if precise synchronization costs are needed, add monotonic worker
   and lock lifecycle events. No B algorithm tuning was performed here.
4. Benchmark operator: use the new clean SHAs, prepare again, then repeat the
   smoke on Colab with accelerator None before the 40-paper pilot. The controller
   changes are still uncommitted/unpublished; distribute them as reviewed source.

Known S2 HTTP 404 papers remain failures in the denominator. A later-page
failure discards partial reference content while retaining HTTP observations.
Partial success does not make the report unusable, and it is not a valid fully
complete warm-cache seed. No benchmark of 1,000 papers was executed.

## Files and validation for this review

| File | Change |
|---|---|
| `src/metrics.py` | Request-scope factory/group ID; robust HTTP observation summary with explicit unknown totals |
| `src/utils/http.py` | Bind opt-in hook once per GET/download invocation, before retries |
| `src/tools/compare_runs.py` | Retain quality/timing when request IDs are ambiguous; expose observed/total HTTP scope and effective transport provenance |
| `src/tools/benchmark_run.py` | No-network capability/timeout probe; pilot blockers; launch drift checks; positive timeout validation |
| `src/tools/analyze_timeline.py` | Reproducible operation/request/residual/overlap evidence with unknown lock waits |
| `tests/test_request_scope.py` | Pagination/retries/404, reused download hooks, concurrency, callback compatibility/failure |
| `tests/test_pilot_readiness.py` | Forty-paper mixed-outcome integration; arxiv internal retry/timeout and redirect coverage; ambiguous subset; effective timeout probe |
| `tests/test_timeline.py` | Union-based overlap and clock/duration/wait distinction |
| `tests/test_benchmark_run.py`, `tests/test_comparison.py` | Pilot refusal, launch drift, zero timeout and degraded-log reporting |
| `docs/patches/http-request-scope.patch` | Minimal reviewed patch for owners; checked against B0/A/B, not applied |
| `docs/metrics-comparison.md`, `docs/metrics-integration.md`, `docs/metrics-verification.md` | Updated protocol and links while retaining historical smoke evidence |

Actual targeted test command:

```bash
/tmp/intro2ds-metrics-py312/bin/python -m pytest \
  tests/test_request_scope.py tests/test_pilot_readiness.py tests/test_timeline.py \
  tests/test_benchmark_run.py tests/test_comparison.py tests/test_metrics.py \
  tests/test_metrics_integration.py tests/test_references.py tests/test_http.py \
  tests/test_discovery_metrics.py tests/test_download_metrics.py -q --tb=short
# 416 passed, 121 subtests passed in 2.39s
```

Final full-suite command and actual result:

```bash
/tmp/intro2ds-metrics-py312/bin/python -m pytest -q --tb=short
# 736 passed, 121 subtests passed in 2.68s

git diff --check
# Exit 0
```

The effective-timeout probe and clean target revisions were rechecked for all
three targets after implementation. Results match the prepared provenance;
evidence is in
`experiments/metrics-pilot-preflight-reviewed/effective-timeout-check.json`.
