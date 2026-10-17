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

```bash
pip install -r requirements.txt
```

## Usage

Usage instructions will be added after the baseline crawler is implemented.

