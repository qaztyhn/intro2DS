# M1 Shared Benchmark

The implemented B0/A/B runner, comparison CLI, measurement definitions and
CPU-only Colab cells are in [metrics-comparison.md](metrics-comparison.md).

## Purpose

Compare crawler configurations using the same selected paper IDs,
expected versions and measurement protocol.

Preparation requests and source-inspection time are not crawler
benchmark measurements.

## Fixed selection

- Benchmark: 1,000 unique paper IDs, 250 per assigned range.
- Pilot: 40 papers from the benchmark, 10 per range.
- Seed: 42; range-specific seeds are derived using SHA-256.
- Both range endpoints are included.

| Range | Start ID | End ID |
|---|---|---|
| R1 | 2505.15666 | 2505.20665 |
| R2 | 2507.11659 | 2507.16658 |
| R3 | 2507.01659 | 2507.06658 |
| R4 | 2507.21659 | 2508.02871 |

The July boundary 2507.23787 is inferred from the exact assigned
count of 5,000 IDs in R4 and its inclusive endpoints.
It was not established by a complete monthly API listing.

Metadata was subsequently obtained for all 1,000 sampled IDs.

## Prepared artifacts

Artifacts are stored outside Git under:

```text
experiments/benchmark-preparation/
  selection-v1/
    config.json
    selection.json
    benchmark_ids.txt
    pilot_ids.txt
    metadata-manifest-v1.json
    pilot-source-manifest-v1.json
    benchmark-manifest-v1.json
  discovery/
    batch-*/
      request.json
      response.xml
      parsed.json
  source-inspection/
    raw/
    records/
  reference-inspection/
```

- selection.json preserves the original selection.
- metadata-manifest-v1.json contains expected versions and metadata
  provenance for all selected papers.
- pilot-source-manifest-v1.json adds source expectations for the pilot.
- Raw API responses and source archives are retained as evidence.
- benchmark-manifest-v1.json is the handoff manifest. It combines the
  fixed selection, metadata, pilot source expectations and reference
  snapshot provenance.
- Paths inside the manifest are relative to
  experiments/benchmark-preparation/.

## Verification completed

- Metadata: 1,000 papers.
- Expected versions: 1,625, inferred from latest-version suffixes.
- Pilot: 40 papers and 60 expected versions.
- Source inspection: all 60 pilot versions are readable tar archives
  and contain TeX.
- Inspection records contain paths, sizes and SHA-256 hashes of
  regular .tex and .bib members.

Source inspection does not establish successful LaTeX compilation,
correct M2 parsing, or completeness of references.

Absence of .bib is not a crawl failure.

## References status

All 40 pilot papers have a recorded lookup outcome:

- 38 papers have complete reference-page snapshots.
- 2 papers returned HTTP 404:
  - 2507.15396
  - 2507.16398
- Completed snapshots contain 1,739 raw reference entries.
- All 1,739 entries have a resolved citedPaper object.
- 860 entries carry an arXiv ID; this is an entry count, not a count
  of distinct cited papers.
- Page hashes, pagination chains and recorded counts passed integrity
  checks before the handoff manifest was created.

HTTP 404 is not evidence of an empty reference list. These two papers
have no verified reference-content expectation.

Snapshots describe Semantic Scholar data at collection time.
They do not establish that Semantic Scholar identified every citation
in the original publication.

## Execution rules

1. Run the fixed pilot first to check integration.
2. Run the full benchmark after pilot issues are resolved.
3. Use Colab CPU-only for the final performance measurements.
4. Record environment, code revision, configuration and run ID.
5. Keep cold and warm runs separate.
6. Do not give one configuration prepared metadata while requiring
   another to retrieve it, unless reporting separate warm/cold results.
7. Retain failed and unresolved input IDs in the report.
8. Do not replace difficult papers without a documented selection change.
9. Use expected source paths, sizes and hashes only for versions
   actually inspected.
10. Never commit API keys, raw datasets or preparation artifacts.

## Comparison rules and remaining integration

- Use benchmark-manifest-v1.json for handoff.
- Test the 40-paper pilot before running the 1,000-paper benchmark.
- Source-content expectations are available only for the 60 inspected
  pilot versions.
- Reference-content expectations are available only for the 38 pilot
  papers with complete snapshots.
- Keep all raw references locally, including entries without arXiv IDs.
- Match resolved reference entries by ref_s2_id where available,
  preserving occurrence counts; do not compare list positions.
- Compare normalized fields against the stored citedPaper data using
  the group's documented normalization rules.
- Do not equate publicationDate with arXiv submission_date.
- The two not_found cases must not be used as expected empty outputs.
- A fresh API response may differ from the stored snapshot. Investigate
  differences before treating them as crawler errors.
- Validate real crawler integrations before comparing configurations.
- Additional edge-case fixtures, such as multi-page responses and
  unresolved citedPaper entries, remain separate from the random sample
  unless those cases are observed in the stored snapshots.
