# Introduction to Data Science Project

## Overview

This project implements a data processing pipeline for scientific papers from arXiv.

The project consists of three milestones:

1. **Data Crawling:** Collect paper metadata, LaTeX source files, and reference metadata.
2. **Parsing:** Parse LaTeX documents, extract BibTeX entries, and remove duplicates.
3. **Matching:** Match BibTeX entries with reference metadata using machine learning.

## Milestone 1 — Data Crawling

The crawler supports:

- Discovering papers and their versions
- Downloading LaTeX sources
- Extracting `.tex` and `.bib` files
- Collecting references from Semantic Scholar
- Recording errors and performance metrics

## Project Structure

## Project Structure

- `src/main.py`: Pipeline orchestration (`python -m src.main`)
- `src/config.py`: Load and validate the run config
- `src/schema.py`: Manifest fields and path/ID validation
- `src/metrics.py`: Performance monitoring (`metrics_events.jsonl`, `ResourceMonitor`)
- `src/discovery/`: Paper metadata and version discovery
  - `arxiv_api.py`: Baseline discovery through the arXiv API
  - `oai_pmh.py`: D1 discovery through arXiv OAI-PMH
  - `kaggle.py`: Discovery from the Kaggle arXiv metadata snapshot
- `src/downloader/`: Source downloading
  - `http.py`: Download LaTeX sources, one request per version
- `src/stages/`: Pipeline stages after download
  - `extract.py`: Source extraction
  - `references.py`: Reference metadata collection
  - `storage.py`: Data storage
  - `validation.py`: Data validation (used by storage)
- `src/utils/`: Shared helpers
  - `common.py`: Status constants, paper IDs, JSON I/O, run event log, stopwatch
  - `http.py`: HTTP helpers (throttling, retries)
  - `errors.py`: Error codes
- `src/tools/`: Standalone tools including `benchmark.py`, `benchmark_protocol.py`,
  `benchmark_run.py`, `analyze_timeline.py`, `compare_runs.py`, `demo_comparison.py`,
  `demo_m1.py`, and `prepare_pilot_references.py`
- `configs/`: Run configs (`*.yaml`) and ID lists (`*.txt`)
- `tests/`: Offline unit tests (`python -m pytest -q`)
- `docs/`: Design notes (benchmark, D1 OAI-PMH, metrics integration, handover)

## Installation

Requires Python 3.11+ with the current pinned dependencies.

For B0/A/B measurements, reports and CPU-only Colab cells, see
[M1 metrics comparison](docs/metrics-comparison.md).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

All settings of a run are read from `configs/baseline.yaml`.

- `user_agent`: put a real contact email in it (arXiv asks for a descriptive User-Agent).
- `ids_file`: text file with one base arXiv id per line (no `vN` suffix; blank lines and `#` lines are ignored).
  - `configs/ids.txt`: small test set (first 20 ids of the assigned range).
  - `configs/ids_full.txt`: the full assigned range (2507.01659 to 2507.06658).
- `run_id`: `B_{timestamp}` starts a new run each time. Set a fixed value such as `B_run01` to resume an interrupted run: versions already `ok` are skipped and failed ones are retried.
- `arxiv_delay_seconds`: pause between arXiv requests (keep at 3 or more).
- `s2_max_retries`, `s2_backoff_base_seconds` (optional): retry budget for Semantic Scholar only. If unset, the shared `max_retries` and `backoff_base_seconds` apply.
- `S2_API_KEY` (environment variable, not in the config): gives a steady 1 request/second instead of the shared pool. Never commit it.
- `configs/baseline_pilot.yaml`: baseline B on the stratified 40-ID pilot (`configs/pilot_ids.txt`).

## Usage

```bash
python -m src.main                      # uses configs/baseline.yaml
python -m src.main configs/other.yaml   # uses another config
```

## Pipeline and module contract

`src.main` processes one paper at a time: discovery -> download (every version) -> extract -> references -> storage.
Each paper ends with one `paper` event (status, total seconds). One failing paper never stops the run.

| Module | Function | Called as |
|---|---|---|
| `src/stages/extract.py` | `extract_paper` | called from `make_extract_hook` in `src/main.py` as `extract_paper(cfg, arxiv_id, logger, metrics_logger=None)`; one measured `extract` operation per version |
| `src/stages/references.py` | `crawl_references` | called from `make_references_hook` in `src/main.py`; writes `<run_dir>/papers/<paper_key>/references.json` |
| `src/stages/storage.py` | `store_paper` | called from `make_storage_hook` in `src/main.py` as `store_paper(cfg, arxiv_id, logger)`; validates the paper and writes `validation.json` |
| `src/metrics.py` | `ResourceMonitor` | used directly in `src.main` to sample RAM and disk; its logs go to `metrics_events.jsonl` |

Rules for hooks: idempotent (skip work already done on resume), log with `logger.log(stage, status, arxiv_id=..., ...)`,
raise on unexpected errors (the driver logs them and continues), read inputs from `<run_dir>/papers/<paper_key>/`.
All three stages (`extract`, `references`, `storage`) are wired in `src/main.py` and timed with `metrics.measure_stage`; their timings go to `metrics_events.jsonl`.

`run.json` records `started_at`, `finished_at`, `run_status` (`running`, `completed`, `interrupted`, `crashed`),
`cache_mode` (`cold` for a new run, `warm` when resuming), `input_ids` and the environment.
