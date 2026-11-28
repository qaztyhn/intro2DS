# Pilot 2 — 200 publications

Run from the repository root with its Python environment active. The sample is fixed at 50 IDs per owner. In assignment order R1–R4, divide each 5,000-line team list into 50 consecutive blocks of 100. For zero-based global block index `j`, call the existing `select_sample(block, 1, 20261128 + j)`. The seed is also recorded in `m1/configs/pilot200_ids.txt`.

## Inventory and prepare offline

```sh
python -m m1.src.tools.pilot_report dry-run --cache-root experiments
```

Repeat `--cache-root` for other **unpacked M1 run directories or their parents**. The inventory checks every observed version against an OK download manifest, the canonical raw archive and its byte count. It checks SHA-256 when the cached source audit supplies a digest. Repeated copies count once. An extracted source directory alone cannot seed a download. Cached metadata describes a snapshot in time.

- `missing_archives` lists confirmed ID/version pairs lacking a reusable archive.
- `unknown_version_lists` lists IDs needing discovery. `provisional_v1_missing` lists the v1 placeholders for those IDs when no v1 is already accounted for. Discovery must confirm they exist and may reveal additional versions.
- `rejected_cache` records failed checks. `s2_publications_to_fetch` counts publications without a reusable successful references snapshot.

For a fresh checkout, seed the new run once with the same cache roots:

```sh
python -m m1.src.tools.pilot_report prepare --cache-root experiments
```

The command refuses an existing destination. If this workspace already has the prepared pilot run, continue with that run. Preparation copies verified raw downloads, reusable metadata and references into `experiments/pilot200_preserve/M1_preserve_pilot200_64cd331/`. Download manifests are seeded as downloaded with extraction pending, so fresh extraction generates the preserve audit. Existing corpora are read-only inputs.

## Network step — requires explicit approval

Run the following block **only after approval**. Supply `S2_API_KEY` in the shell environment through the team's secret mechanism. The check reveals no value.

```sh
: "${S2_API_KEY:?Set S2_API_KEY in the environment}"
python -m m1.src.main m1/configs/pilot200_preserve.yaml
```

M1 discovers missing publication metadata, fetches only versions without an OK seeded download, and requests missing Semantic Scholar references. The fixed run ID supports resume. An interrupted attempt can be resumed with the same config after checking its log. A separate experiment requires a new run ID.

For each ID needing discovery, budget one arXiv discovery request, plus optional version-history requests and one archive request per missing version. Semantic Scholar requires at least one page per uncached publication reaching references (1,000 references/page). Retries, pagination and fallbacks add requests. The arXiv pacing estimate is 3 seconds multiplied by estimated requests; transfer, parsing, S2 and retry time are additional. Record actual requests and timings from the completed run.

## Parse and report offline

```sh
run=experiments/pilot200_preserve/M1_preserve_pilot200_64cd331
out=experiments/pilot200_analysis_64cd331
python -m m2.src.parsing.cli --run-dir "$run" --ids-file m1/configs/pilot200_ids.txt --output-dir "$out" --label preserve
python -m m1.src.tools.pilot_report report --run "$run" --m2 "$out/preserve" --output "$out/report"
```

Use a new label and report directory for each attempt. M2 can return nonzero for a failed version while still writing the QA and processing reports; the report command can then describe the failure. Review `report.json` or `report.md`:

- Multi-root incidence counts distinct manifest-listed files containing active document declarations after T4 provenance filtering and before README/graph selection. Compare candidate names for resolved and unresolved versions. An observed no-TeX version counts as zero roots. An unavailable scan is excluded and has a null list.
- Diagnostic counts are numbers of versions carrying each code. A version can contribute to multiple codes.
- For a BBL control, remove original BBL files in a temporary copy, repeat M1 conversion, apply the same root choice and parse both bibliographies with the same parser. Compare per-version keys, normalized fields and entry type before cross-version deduplication. A changed or missing previously populated field is reported with both values; newly populated fields are enrichment. Versions without BBL have an identical control. Unavailable inputs or a different/unresolved root have a null comparison. Zero regressions applies only to the stated denominator.
- Filtered references are literal path matches from included original TeX assets for input/include, listing and PGF table commands; graphics are excluded. Hits include suffix, path, source file and command. This is static evidence; dynamic macro targets and unresolved branches can escape detection.
- Per-version seconds are measured M1 version-tagged stage sums from `metrics_events.jsonl`. Publication-wide work is not allocated to siblings. M2 provides end-to-end batch wall time, reported as an average over its recorded versions. Missing measurements are null. Disk figures are logical bytes for the raw archive, original assets and derived source plus their sum.
- Check table validation, publication issues, comparison coverage and every concrete key/field regression before deciding whether to expand. If selective source recovery is later needed, retain sibling versions as described in [SOURCE_RECOVERY.md](SOURCE_RECOVERY.md).
