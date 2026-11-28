# Preserved source extraction and recovery

Fresh normal M1 crawls now use `preserve_source_assets: true` by default. The
normal extract hook preserves original text assets, invokes the same converters
as the recovery tool, and produces `.tex/.bib` inputs for the existing M2 parser.
Setting the option to false retains legacy `.tex/.bib/.bbl` extraction. The
low-level `extract_source` API keeps its original default whitelist.

Each normal version also writes `source_assets_audit.json`: archive member names,
suffixes, sizes, retained/filtered decisions, conversion records, warnings and
SHA-256 provenance. Filtered files with TeX/BibTeX-looking content in their first
64 KiB are flagged for review; this bounded heuristic cannot identify every
possible source format. All filtered filenames remain in the audit, even when
the heuristic finds no source syntax. Failed/partial archive scans are labeled
incomplete. Console and `events.jsonl` source_assets events summarize filtered
suffixes and resources needing attention.

The downloaded raw `tmp/<paper_key>/v<N>/source.archive` is retained. Include
that directory in saved/exported runs: retrying an unknown filtered format
requires the raw archive, not just the already filtered `source/` directory.
Original unpacked/retained byte counts are in the audit's extraction record;
the derived manifest's kept_source_bytes measures parser inputs, and its
unpacked_total_bytes is null because derivation can enlarge text.

Completed legacy caches are left intact and emit `SOURCE_POLICY_NOT_APPLIED`.
Use a fresh run ID for a full recrawl. Fresh member configs are
`m1/configs/team/preserve_<member_id>.yaml`; their timestamped run IDs prevent
accidentally resuming the earlier dataset.

```powershell
python -B -m m1.src.main m1/configs/team/preserve_24120027.yaml
python -B -m m1.src.tools.audit_sources --run-dir experiments/recrawl_20261128/<RUN_ID> --member-id 24120027 --output tmp/audit_<RUN_ID>
python -B -m m2.src.parsing.cli --run-dir experiments/recrawl_20261128/<RUN_ID> --ids-file m1/configs/team/ids_24120027.txt --output-dir experiments/m2_recrawl_20261128 --label <RUN_ID>
```

Replace `<RUN_ID>` with the actual ID printed by M1, and use the matching member
config and IDs for each other range. These commands do not launch a crawl merely
by creating the configs. The audit command creates `report.json` and
`retry_plan.json` in a new directory. The latter is compatible with the recovery
tool below: available archives are reused offline; use `--download` if they are
missing. Review candidates are not guaranteed repairable by another extraction.
Genuine PDF-only sources are reported as no-TeX, not sent into an endless retry.

Run M2 after its member's M1 snapshot is ready; keep all sibling versions.
The upgraded M2 code reads these sources without an additional overlay step.
Audit/policy metadata and diagnostics are optional manifest additions; official
M2 table schemas are unchanged. Structural ambiguities and unverified metadata
conflicts still produce partial/failed versions.

## Recover selected versions separately

Run from the repository root. This standalone recovery path does not overwrite
canonical M1 inputs. It uses the same bounded extractor and converters as fresh
normal crawls, with an additional text-asset whitelist.
The publication processing pipeline and official Parquet schemas are unchanged.

```powershell
python -B -m m1.src.tools.recover_sources --plan PLAN.json --output NEW_RUN
```

The plan is a JSON list. Each row has `arxiv_id`, integer `version`, optional
`member_id`, optional `archive` (an original arXiv source archive), and optional
`paper_dir` (existing metadata/references to copy). Duplicate versions are rejected.
The output directory must not exist. Metadata in this recovery run lists only
the versions actually supplied in the plan; it is a subset, not proof of full
version coverage. Missing reference metadata is never manufactured.

```json
[
  {
    "arxiv_id": "2507.01659",
    "version": 1,
    "member_id": "24120058",
    "archive": "D:/archives/2507.01659v1/source.archive",
    "paper_dir": "D:/old-run/papers/2507-01659"
  }
]
```

If archives are unavailable, `--download` fetches the versioned source endpoint
sequentially, with at least 3.1 seconds between request starts, a 90-second
timeout and a 256 MiB response limit. Failed downloads remain explicit in the
report; never interpret a failure as an empty bibliography. Transient network,
truncated-response and retryable HTTP failures get at most three paced attempts.
Successful archives are published only after size checks. Use a new output
directory for another attempt. No model, GPU, TeX execution or external metadata
lookup is involved. The network option needs network access and was not required
for the local 220-version pilot.

Each recovered version contains:

- `original_assets/`: retained original text assets, with original names/bytes.
- `source/`: derived parser inputs using only `.tex` and `.bib` suffixes.
- `manifest.json`: valid M1 extraction fields plus optional recovery diagnostics.
- `recovery_provenance.json`: archive location/hash, original/derived hashes,
  conversions, rewritten directives, extraction byte counts and validation.
- `recovery_compilation.json`, when available: compilation hints verified against
  original `00README.json`. The README stays in `original_assets`, never in TeX.

The original archive stays at its recorded location. A downloaded archive is
kept alongside the recovered version. Figure binaries never enter either source
directory. Derivation byte counts differ from original archive extraction counts;
the original unpacked total is in provenance, while the derived manifest leaves
`unpacked_total_bytes` null rather than reporting an invented extraction size.

## Conversions

| Input | Derived input | Limits |
| --- | --- | --- |
| `.tex`, `.bib` | Original name/bytes, except explicit dependency rewrites | Originals remain independently available |
| Classic BBL with `\bibitem` | `name.bbl.tex` | Lossless citation text; structured metadata remains best effort |
| BibLaTeX/Biber BBL | `name.bbl.bib` | Supported fields/names only; malformed/unsupported records diagnosed |
| AMSRefs BBL with `\bib{key}{type}{fields}` | `name.bbl.bib` | Repeated authors/editors preserved; unsupported nested metadata diagnosed |
| `.ltx`, `.latex`, `.sty`, `.cls`, `.def`, `.inc`, `.tikz`, `.pgf`, `.pdf_tex`, `.eps_tex` | Original name plus `.tex` | Text retention, not automatic interpretation of custom packages/macros |
| `.txt` containing valid BibTeX | Original name plus `.bib` | Parse errors prevent conversion |
| `.txt` identified as TeX | Original name plus `.tex` | Ordinary logs/download instructions are not source |
| Plain `.txt` reached by active literal `input/include/subfile` | Original name plus `.tex` | Requires a known compilation root and resolved conditional path |
| `.bst` | Original assets only | Style source is preserved, not mislabeled as bibliography or document text |

Binary/NUL assets and HTML responses are not converted. A support file alone does
not create a document record. Existing original paths win over colliding derived
names; collisions are diagnosed. Conversion never guesses a missing title/year,
downloads a bibliography mentioned in a text instruction, or converts PDF to TeX.

Static input/include paths are rewritten when a retained resource was renamed.
Root/jobname BBL fallback replaces an unavailable bibliography resource, or is
included beside an available bibliography to preserve complementary metadata.
Only a unique same-root BBL is selected. Unknown conditional branches are not
guessed. Rewritten sources are standardized parser inputs; this is not a claim
that arbitrary reconstructed submissions compile to byte-identical PDFs.

BBL conversion diagnostics are also carried in
`manifest.recovery_bibliography_warnings` and propagated into M2 partial status.
An unknown BBL cannot silently turn into a fully reliable `ok` document merely
because some other reference file was readable.

The extractor can retain the exact filename `00README.json` without enabling
arbitrary JSON input. M2 verifies the sidecar identity, bounded README content and
hash before using a unique declared top-level file. `usage=ignore` is not an
instruction to discard BBL/resources. Multiple actual top-level documents remain
ambiguous; no supplementary text is silently thrown away. Embedded `subfiles`
children contribute their document bodies and declared bibliography resources,
while their standalone document envelopes are excluded.

The bibliography reader supports bounded IEEE, natbib/ACL, REVTeX `bibinfo` and
AMSRefs cases. Repeated-author marks use only an explicit preceding author list
in the same bibliography context. Unknown macros, ambiguous dates and unsupported
metadata retain diagnostics instead of guessed values. Citation checks resolve
canonical/case aliases and exclude wildcard `nocite` and BibTeX control commands.

M2 `processing_report.json` includes `bibliography_reconciliation` records with
source paths, native/fallback canonical values, conflicting/equivalent fields,
filled fields and identity evidence. Native populated fields are preserved.
Compatible missing fields can be filled even when a different field remains in
conflict, but only with independent work identity. Note-only DOI mentions and
arXiv-looking substrings on unrelated hosts cannot establish that identity.
Actual conflicts keep partial status. Identifier URL alternatives are preserved
in reconciliation provenance. Official Parquet column schemas are unchanged.

## Validation and corpus rollout

Run the extraction, recovery, gathering and bibliography tests before processing
new cases. Compare the same version subset, using the same parser, once with
original `.tex/.bib` inputs and once with recovered inputs. Validate exported
tables and inspect regressions in missing citation keys and version status.
Keep archive failures, PDF-only/empty versions and ambiguous roots visible.

The local pilot inventory covers all four assigned member ranges, but only 220
versions/140 publications; it is not a full 20,000-publication rerun. Extending
recovery beyond local archives requires fetching additional original versions.
Keep M3 selection IDs fixed, then compare citation keys and row hashes before
adopting the recovered snapshot for labeling or candidate generation.

### Balanced expansion waves

`plan_recovery` produces a reproducible 64-version wave: 16 versions per member,
with four other-text dependencies, four explicitly missing BBLs, four missing
bibliographies, two root/structure cases and two clean controls. It selects one
version per publication and excludes publications from earlier validation plans.
It fails on quota shortages instead of silently reducing range coverage. Input
report hashes, seed and strata are recorded beside the plan.

```powershell
python -B -m m1.src.tools.plan_recovery --reports-root D:/intro2DS/m2-output --m1-roots roots.json --exclude-plan previous-plan.json --output tmp/next-wave
python -B -m m1.src.tools.recover_sources --plan tmp/next-wave/plan.json --output tmp/next-wave-recovered --download
```

`roots.json` maps each member ID to its existing M1 run directory. Repeated
`--exclude-plan` arguments exclude all previous validation waves. Downloads are
sequential; parsing independent publications may run in parallel. Retain and
reuse downloaded archives when improving conversions, rather than requesting
the same source again. A new offline plan can point `archive` to the retained
archive and omit `--download`.

### Preserve every sibling version before adopting recovery

The recovered sampling run contains only selected versions. Do not use it to
replace an entire publication or merge its single-version Parquet rows into the
full corpus. M2 deduplicates both elements and bibliography across versions.

Add `recovered_run` to each plan row, pointing to the recovery run that supplies
that version, then create a separate overlay:

```powershell
python -B -m m1.src.tools.overlay_recovery --plan overlay-plan.json --output tmp/full-publication-overlay
```

The overlay retains original metadata, references, all other version directories
and declarations of unavailable versions. Only selected version directories
come from recovery. It verifies manifests and byte hashes, writes
`overlay_manifest.json`, and refuses an existing destination. It never changes
the original M1 run. Different replacement versions can come from different
recovery runs, while all replacements for a publication must share the same
original paper directory.

Run the same frozen M2 parser on the original full publications and the overlay;
compare selected and unmodified version statuses separately. Export and validate
all publication parts together. An accepted replacement must replace that
publication's parts in every M2 table, rather than append duplicate rows. Retain
the previous snapshot and report for rollback.

Inspect clean-control regressions in raw BBL and field reconciliation. Extra
metadata can expose real conflicts, and an unsupported printed style can also
cause a parser error; neither should be hidden by dropping BBL or suppressing
warnings. Keep genuine multiple-root cases pending until compilation evidence
or a documented main/supplement policy resolves them. Scalar AMSRefs `type` and
`review` metadata are retained; unknown nested fields remain diagnostic. A
strictly recognized empty `thebibliography` is valid empty input, not evidence
that an unavailable bibliography was recovered.

For a full recovery rollout, track completed `(arxiv_id, version)` keys, not
only publication IDs: another version of a previously sampled paper may still
need recovery. The one-publication exclusion in `plan_recovery` is specifically
for diverse validation waves. A missing-resource candidate is a reason to
inspect/download source, not proof that the original archive contains that
resource. Increase batch size only after the newly observed syntax and control
regressions have been reviewed. No model or GPU is needed for this workflow.
