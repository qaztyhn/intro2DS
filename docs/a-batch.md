# A + Batch (experimental branch)

Branch: `feature/a-batch-lanes`. Baseline `main` and the existing B / parallel-papers branch are unchanged.

## Architecture

The A experiment combines two improvements, and **does not process multiple papers concurrently**:

1. `discover_batches()` resolves metadata from the arXiv API using `Search(id_list=...)` in configurable batches (default 20 IDs). It maps responses by base arXiv ID, not response order. It batches intermediate revision-date lookups across papers as a second phase, while preserving the existing single-paper and abstract-page fallbacks.
2. After metadata discovery has completed, the pipeline processes **one paper at a time**. For each paper, two lanes overlap: source download + extract on the main thread, and Semantic Scholar reference collection on one worker thread. Storage/validation starts **after both lanes join**.

```
IDs -> arXiv batch discovery (base metadata + revision dates)
        -> one paper:
           |-- main lane: download all versions -> extract
           |-- worker:    Semantic Scholar references (one publication)
           -> join -> storage/validation
        -> next paper
```

The source versions are still downloaded individually; `references.json` is still shared by all versions of a publication. The Semantic Scholar API is not made version-aware. This variant does not change the stage logic or Semantic Scholar request throttle.

## Files

- `src/discovery/batch.py`: batch lookup, version dates, mapping and fallbacks.
- `src/main.py`: invokes batch discovery first and overlaps the two per-paper lanes only for `config_name: A_BATCH`.
- `src/utils/common.py`: protects the shared event logger with a thread lock; `metrics.EventLogger` already uses a lock.
- `configs/a_batch_pilot.yaml`: isolated pilot config with 40 IDs.
- `tests/test_a_batch.py`: mock/offline test coverage of batch mapping, revision batching, resume, fallback and lane ordering.

Other configurations B / D1 keep their earlier sequential `process_paper` flow.

## Usage

From the repository root, install project dependencies and run:

```bash
python -m pytest -q tests/test_a_batch.py tests/test_main_hooks.py
python -m pytest -q
python -m src.main configs/a_batch_pilot.yaml
```

The config points to `configs/d1_pilot_ids.txt`, the existing shared 40-paper pilot selection. The default `run_id` contains `{timestamp}`, so each invocation creates a **new cold run**. To resume, change it to a fixed string such as `A_BATCH_pilot01`, and rerun with that string.

Output remains under `experiments/<run_id>/papers/<paper_key>/`. Each paper has `metadata.json`, `references.json`, and its per-version manifests. Batch events use stage `discovery_batch` and count **one request/batch operation**, not one HTTP request per paper. Do not add batch duration to each paper's duration; per-paper `paper` events begin after prefetched discovery in this variant, so use `run.json.wall_seconds` for total end-to-end runtime.

## Benchmark

Compare with baseline on **identical IDs, fresh run IDs**, same environment, same 3-second arXiv pacing and same Semantic Scholar credentials/rate. Collect end-to-end wall time, completed/failed papers, actual HTTP 429 errors, number of metadata and revision API queries, and completeness of `metadata.json`, sources and `references.json`. For A, also test batch size 20 and 50 after the default pilot.

The multi-paper B branch is a separate experiment and is **not merged into A**.

## Limitations and verification

- arXiv `id_list` can reorder/omit results: a missing result is queried individually; failed batch requests fall back to individual discovery. Missing/incomplete revision dates use baseline revision-date lookup and abstract-page fallback.
- Some arXiv version data may be incomplete; `revised_dates=None` remains allowed by the baseline contract. Never fabricate dates or version numbers.
- Batch discovery runs before the first paper download, increasing time to first completed paper. The expected gain is fewer requests / lower overall wall time, not latency for the first paper.
- No live arXiv/Semantic Scholar crawl or benchmark has been performed by this change. Run all offline tests, then the 40-paper pilot, before claiming measured acceleration.
