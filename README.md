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
- Preserving original text assets and deriving `.tex`/`.bib` inputs, with filtering/conversion audits
- Collecting references from Semantic Scholar
- Recording errors and performance metrics

## Project Structure

- `m1/src/main.py`: Pipeline orchestration (`python -m m1.src.main` from the repository root)
- `m1/src/config.py`: Load and validate the run config
- `m1/src/schema.py`: Manifest fields and path/ID validation
- `m1/src/metrics.py`: Performance monitoring (`metrics_events.jsonl`, `ResourceMonitor`)
- `m1/src/alternative_methods/`: Paper metadata and version discovery
  - `arxiv_api.py`: Baseline discovery through the arXiv API
  - `oai_pmh.py`: D1 discovery through arXiv OAI-PMH
  - `kaggle.py`: Discovery from the Kaggle arXiv metadata snapshot
- `m1/src/downloader/`: Source downloading
  - `http.py`: Download LaTeX sources, one request per version
- `m1/src/stages/`: Pipeline stages after download
  - `extract.py`: Source extraction
  - `references.py`: Reference metadata collection
  - `storage.py`: Data storage
  - `validation.py`: Data validation (used by storage)
- `m1/src/utils/`: Shared helpers
  - `common.py`: Status constants, paper IDs, JSON I/O, run event log, stopwatch
  - `http.py`: HTTP helpers (throttling, retries)
  - `errors.py`: Error codes
- `m1/src/tools/`: Standalone scripts for benchmark, recovery and source audits
- `m1/configs/`: Run configs (`*.yaml`) and ID lists (`*.txt`)
- `m1/tests/`, `m2/tests/`: Offline unit tests
- `m1/docs/`, `m2/docs/`: Design and execution notes

## Repository layout

Code is split by milestone: `m1/src` (crawler) and `m2/src` (parser), each with its own `tests/`
and `docs/`. A single `requirements.txt` at the repository root covers both. All commands are run
from the repository root, e.g. `python -m m1.src.main` and `python -B -m m2.src.parsing.cli`.

## Installation

Tested with Python 3.12; source preservation uses Python 3.11+ APIs.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

All settings of a run are read from `m1/configs/baseline.yaml` by default.
Config-internal relative paths resolve against `m1/`.

- `user_agent`: put a real contact email in it (arXiv asks for a descriptive User-Agent).
- `ids_file`: text file with one base arXiv id per line (no `vN` suffix; blank lines and `#` lines are ignored).
  - `m1/configs/ids.txt`: small test set (first 20 ids of the assigned range).
  - `m1/configs/team/ids_<member_id>.txt`: the full assigned range for that member.
- `run_id`: `B_{timestamp}` starts a new run each time. Set a fixed value such as `B_run01` to resume an interrupted run: versions already `ok` are skipped and failed ones are retried.
- `arxiv_delay_seconds`: pause between arXiv requests (keep at 3 or more).
- `s2_max_retries`, `s2_backoff_base_seconds` (optional): retry budget for Semantic Scholar only. If unset, the shared `max_retries` and `backoff_base_seconds` apply.
- `S2_API_KEY` (environment variable, not in the config): gives a steady 1 request/second instead of the shared pool. Never commit it.
- `preserve_source_assets`: true by default; retain original text assets, derive parser inputs and audit filtering. Completed legacy caches require a new run or explicit recovery to receive this policy.
- `m1/configs/baseline_pilot.yaml`: baseline B on the stratified 40-ID pilot.

## Usage

```bash
python -m m1.src.main
python -m m1.src.main m1/configs/team/preserve_24120027.yaml
```

## Pipeline and module contract

`m1.src.main` baseline processes one paper at a time: discovery -> download (every version) -> extract/preserve/convert -> references -> storage.
Each paper ends with one `paper` event (status, total seconds). One failing paper never stops the run.

| Module | Function | Called as |
|---|---|---|
| `m1/src/stages/extract.py` | `extract_paper` | called from `make_extract_hook` in `m1/src/main.py`; one measured `extract` operation per version |
| `m1/src/stages/references.py` | `crawl_references` | called from `make_references_hook`; writes `<run_dir>/papers/<paper_key>/references.json` |
| `m1/src/stages/storage.py` | `store_paper` | called from `make_storage_hook`; validates the paper and writes `validation.json` |
| `m1/src/metrics.py` | `ResourceMonitor` | used by the driver to sample RAM and disk; its logs go to `metrics_events.jsonl` |

Rules for hooks: idempotent (skip work already done on resume), log with `logger.log(stage, status, arxiv_id=..., ...)`,
raise on unexpected errors (the driver logs them and continues), read inputs from `<run_dir>/papers/<paper_key>/`.
All three stages (`extract`, `references`, `storage`) are wired in `m1/src/main.py` and timed with `metrics.measure_stage`; their timings go to `metrics_events.jsonl`.

`run.json` records `started_at`, `finished_at`, `run_status` (`running`, `completed`, `interrupted`, `crashed`),
`cache_mode` (`cold` for a new run, `warm` when resuming), `input_ids` and the environment.

## Milestone 2 — Integrated parser

M2 modules live in `m2/src/parsing`; run commands from the repository root.
See [M2 integration and execution](m2/docs/INTEGRATION.md) for setup, CLI arguments,
output schemas, QA, tests and current limitations. No crawling or S2 requests occur in M2.

```powershell
python -B -m pytest m2/tests -q -p no:cacheprovider
python -B -m m2.src.parsing.cli --help
```

Fresh M1 crawls preserve original text assets and derive `.bib`/`.tex` inputs
for M2, with a per-version filtering/conversion audit. See
[source preservation and retry](m1/docs/SOURCE_RECOVERY.md) for fresh member
configs and selective offline recovery. Existing corpora are not overwritten.
