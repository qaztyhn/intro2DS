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

- `src/main.py`: Pipeline orchestration
- `src/config.py`: Load and validate the run config
- `src/common.py`: Shared helpers (status constants, paper IDs, JSON I/O, run event log)
- `src/schema.py`: Manifest fields and path/ID validation
- `src/metrics.py`: Performance monitoring
- `src/discovery/`: Paper metadata and version discovery
- `src/downloader/`: Source downloading
- `src/stages/`: Pipeline stages after download
  - `extract.py`: Source extraction
  - `references.py`: Reference metadata collection
  - `storage.py`: Data storage
  - `validation.py`: Data validation (used by storage)
- `src/utils/`: HTTP helpers (throttling, retries) and error codes
- `src/tools/`: Standalone scripts that `src.main` does not run (`benchmark.py`, `demo_m1.py`, `prepare_pilot_references.py`)

## Installation

Requires Python 3.10+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r src/requirements.txt
```

## Configuration

All settings of a run are read from `configs/baseline.yaml`.

- `user_agent`: put a real contact email in it (arXiv asks for a descriptive User-Agent).
- `ids_file`: text file with one base arXiv id per line (no `vN` suffix; blank lines and `#` lines are ignored).
  - `configs/ids.txt`: small test set (first 20 ids of the assigned range).
  - `configs/ids_full.txt`: the full assigned range (2507.01659 to 2507.06658).
- `run_id`: `B_{timestamp}` starts a new run each time. Set a fixed value such as `B_run01` to resume an interrupted run: versions already `ok` are skipped and failed ones are retried.
- `arxiv_delay_seconds`: pause between arXiv requests (keep at 3 or more).

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