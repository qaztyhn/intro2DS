# A bounded pipeline (experimental)

Branch: `feature/a-bounded-pipeline` (based on `feature/a-batch-lanes`).
Baseline `main`, A + Batch and B parallel-papers branches are untouched.

## Architecture

* Main thread is the only **arXiv lane**: discover metadata by batches, download source versions sequentially, and optionally fetch **one next metadata batch** early when a batch is almost exhausted. No separate discovery thread or concurrent arXiv calls.
* **Extract worker** and **References worker** consume paper jobs from independent bounded queues. References may execute while a different paper downloads or extracts.
* Storage/validation runs **once per paper**, only after both consumers have finished (including failures); the paper event records status and underlying root failure.
* The queues contain only small job descriptors (ID, counters, flags) — not downloaded archives or reference payloads. Original modules write large outputs to disk.
* `pipeline_max_inflight` semaphore bounds *all* unfinished papers, including a paper whose references finished but whose extraction is waiting. Queue sizes alone do not provide this bound.
* Prefetch is an **interleaved metadata lookup on the same arXiv lane**, not a concurrent HTTP call. It does not eliminate rate-limit retries; no promise of a particular speedup.

## Configuration

`configs/a_pipeline_pilot.yaml`:

| Key | Default | Meaning |
| --- | ---: | --- |
| `discovery_batch_size` | 20 | Metadata IDs in one discovery group |
| `pipeline_max_inflight` | 4 | Maximum unfinished papers |
| `pipeline_extract_queue` | 2 | Extract jobs waiting (excludes active worker) |
| `pipeline_references_queue` | 4 | Reference jobs waiting (excludes active worker) |
| `pipeline_prefetch_remaining` | 5 | Prefetch when this many or fewer IDs remain in the current group; 0 disables early prefetch, though the next group is still discovered when reached |

The main thread is the arXiv producer and there are two worker threads, plus the existing optional resource-monitor sampler. Processing of each individual source archive remains sequential, bounded, and performed on disk.

## Run and compare

```powershell
git fetch origin
git switch feature/a-bounded-pipeline
py -m pytest -q tests/test_a_pipeline.py tests/test_a_batch.py
py -m pytest -q
py -m src.main configs/a_pipeline_pilot.yaml
```

Compare `run.json.wall_seconds`, successful paper count, root errors, 429s, and resource-monitor peak RSS with the existing `A_compare_02` and `B_compare_02` runs. Use new cold run IDs, the exact same ID list and network settings. Do **not** claim a speed or RAM win without measurements; rate limits confound timing.

## Caveats

* `run_status=completed` means the orchestration finished, not that every paper succeeded. Review paper-level events and validation.
* The queues apply backpressure and can block arXiv producer downloads when the extract/reference consumers cannot keep up.
* `pipeline_prefetch_remaining` is a scheduling threshold, not background discovery. If the pipeline is saturated, next batch discovery occurs when the producer reaches it.
* A failed download can still reach Extract and Storage for validation, preserving the existing per-stage error model.
* No live external-API benchmark or full local test run has been confirmed as part of this remote branch update. Run tests and a small pilot before relying on benchmark results.
