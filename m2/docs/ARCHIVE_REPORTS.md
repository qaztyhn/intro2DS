# Incremental M1 statistics and M2 edge-case intake

Use this when members supply consolidated archives at different times and local
space is insufficient to retain every raw download. It is a read-only input scan,
not full M2 parsing or corpus consolidation. No notebook or network access is used.
The source profiler is shared with `profiling.py` through `profile_version_data`.

From the repository root, ingest one reviewed source per member into the same
persistent output directory:

```powershell
python -B -m m2.src.parsing.archive_reports --archive "<MEMBER_ARCHIVE.zip_or_tar.gz>" --member 24120046 --ids-file m1/configs/team/ids_24120046.txt --output "D:\intro2DS\m1-m2_report\accumulated"
```

For another member, change archive/member/IDs arguments, retaining `--output`.
ZIP is indexed without full extraction; TAR is streamed publication by publication.
TAR publications must be contiguous. Unsafe paths, links, duplicate publication
artifacts or non-contiguous duplicates stop registration instead of silently
choosing a winner. Archives must already embody the intended retry selection;
this tool does not infer precedence among conflicting retries.

For an outer **ZIP_STORED** bundle containing current batch ZIPs and separate
backup directories, select the reviewed directory and filename pattern explicitly:

```powershell
python -B -m m2.src.parsing.archive_reports --archive "<OUTER.zip>" --member 24120027 --ids-file m1/configs/team/ids_24120027.txt --output "D:\intro2DS\m1-m2_report\accumulated" --nested-zip-dir Intro2DS_Data --nested-zip-pattern "A_REAL_[0-9][0-9][0-9].zip"
```

Only direct matching ZIP children of that directory are used. Backup ZIPs in
other directories are not additional publications. A bounded, seekable view reads
the inner central directory and required members directly from the stored outer
payload; it does not extract whole ZIPs or load them into RAM. Compressed/encrypted
outer batch members are refused rather than using an unbounded fallback.
Nested batch paths and counts are retained in snapshot `inventory.json`. Available
`FINAL_AUDIT` reports and `update_report.json` are retained separately in
`source_reports.zip`. Source paths include the outer batch context. Duplicate
publication keys across selected batches stop intake for a precedence review.
Temporary `tmp/<paper_key>/source.archive` records are not publications.
When a retained `M1_FINAL_SUMMARY.json` is available, its claimed counts are
compared with observed artifact states. Disagreements never override stored
validation. Explicit no-TeX manifests paired with stored missing-source validation
errors are flagged separately. Empty directory entries may be absent from ZIPs,
so filesystem validation and logical no-TeX coverage must not be conflated.

## Persistent outputs

- `M1_REPORT.json`: per-member expected/observed/missing IDs, stored validation
  states, download/extraction/reference errors, reference/date counts and source
  provenance. Missing stored validation is unknown, not automatically successful.
- `M2_PROFILING.json`: per-member and combined preliminary source statistics.
- `M2_EDGE_CASES.json`: tagged version records, include/bibliography findings,
  source member locations and SHA256 hashes, plus representative sample inventory.
- `M2_EDGE_CASES_SUMMARY.json`: small human-readable tag/sample counts; tags overlap
  and count candidate versions, not unique publications or confirmed parser errors.
- `registry.json`: immutable completed snapshots and checksums, keyed by member
  and full archive SHA256, with the assigned ID lists and scanner/profiler hashes.
- `progress.json`: running/completed/failed state and progress every 100 papers.
- `snapshots/<member>_<hash>/`: publication/version JSONL ledgers, source inventory,
  per-source profile, bounded `edge_samples.zip`, and compressed audit records.

Re-ingesting identical bytes does not add counts. A different source for an
already registered member is refused until a deliberate retry/consolidation
decision is made. New member snapshots are appended; older sources are not needed
to rebuild the accumulated JSON. All retained artifacts are checked against hashes.
An exclusive lock prevents concurrent writers to the same output.

After verifying completion, reports can be regenerated without raw archives:

```powershell
python -B -m m2.src.parsing.archive_reports --rebuild --output "D:\intro2DS\m1-m2_report\accumulated"
```

Cross-member publication overlaps are exposed explicitly; combined statistics
that would silently double-count are withheld. Missing expected IDs and IDs outside
the assigned list remain visible. Raw audit records are retained compressed;
serialized duplicates are counted separately. Error-record counts are not assumed
to be network request attempts, and absent logs are not interpreted as zero errors.
Wall time and resource aggregation are deliberately outside this intake.

## Bounded profiling and retention

Input profiling uses regex and an approximate include graph, not the production
gatherer/parser. Findings such as multiple roots, missing includes, cycles, custom
if macros and raw BibTeX brace-count mismatches are candidates for later inspection,
not confirmed parser failures. Root counts and file presence are based on retained
manifest-listed source. Analysis reads up to 8 MB per source file, within a 32 MiB
prefix budget per publication; truncation is recorded and not called a root failure.
JSON metadata has a separate 16 MiB/file cap. Invalid/oversized JSON stays unknown.

Representative source retention is limited to two findings per tag and 4 MiB per
selected version. Oversized/truncated representatives are explicitly incomplete,
not claimed to be runnable golden fixtures. Samples have a `papers/` layout but
may contain only selected versions, whereas metadata still lists every version.

**Keep the accumulated directory.** Raw input downloads may be removed only after
successful intake/read-back verification and confirmation that the originals remain
available elsewhere (for example on Drive). Reports and bounded samples do not
replace the full sources or S2 references needed to actually run M2/M3. Preserve the
original corpus remotely or reload each member's source when running those stages.
No automatic deletion or cleanup of user input is performed.
