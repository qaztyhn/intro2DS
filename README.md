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

- `src/discovery/`: Paper metadata and version discovery
- `src/downloader/`: Source downloading
- `src/extract.py`: Source extraction
- `src/references.py`: Reference metadata collection
- `src/storage.py`: Data storage
- `src/metrics.py`: Performance monitoring
- `src/main.py`: Pipeline orchestration

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

Results are written to `experiments/<run_id>/` (not tracked by git):

```
experiments/<run_id>/
  run.json                      # config of the run
  events.jsonl                  # one JSON line per event (status, error_code, elapsed_seconds)
  papers/<paper_key>/
    metadata.json               # title, authors, dates, version list
    versions/v<N>/manifest.json # download status and source format of each version
  tmp/                          # downloaded archives (temporary)
```

`paper_key` is the arXiv id with `.` replaced by `-` and `/` by `__` (e.g. `2507-01659`).