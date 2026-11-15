# M2 integration

Integration is based on `origin/main` at `1705c36`, including its M1 restructuring.
Merged branches (original commits remain ancestors):

| Component | Branch | Revision |
| --- | --- | --- |
| Adapter / gathering | feature/m2-gathering | c8c8e0a |
| Hierarchy / normalization | feature/m2-hierarchy | c18469d |
| Bibliography / profiling | feature/m2-bibliography | f46f212 |
| Dedup / export / QA / benchmark | feature/m2-dedup-export | e6b885e |

Requirements checked against the team contract `Intro2DS_Project.md` and
[official M2 specification](https://hcmus-cs-ta.github.io/lab-book/intro-ds/statement/parsing/)
on 2026-11-15. Integration tests are synthetic; this is not a full-corpus validation.

## One package and one contract

Use `m2.src.parsing` from the repository root. M1 remains in `m1.src`.
The six shared dataclasses live only in `m2/src/parsing/contracts.py`, with their
agreed fields and public signatures unchanged. The duplicated provisional
contracts and root-level M2 modules were consolidated, not maintained as aliases.
Existing component tests were retained and imports updated for the new layout.

The `process_publication(run_dir, arxiv_id)` contract returns three lists:
elements, hierarchy rows (including internal position), and mapped bibliography
rows. `process_publication_detailed` additionally supplies version QA, warnings,
bibliography counts and stage durations without extending the shared dataclasses.

Order: adapter → gathering → bibliography extraction across versions → unified
citation mapping → hierarchy/normalization → deduplication → Parquet export → QA.
Partial versions retain usable text/bibliography and warnings; they are never
reported as fully successful. Each publication includes all its recorded versions.
Version errors are logged and isolated; publication errors are recorded by the CLI
and do not terminate the remaining ID list. Missing metadata/versions are recorded
as publication errors, without inventing a version number.

## Integration corrections

- **No-TeX:** the original exporter branch emitted a synthetic root for an empty
  version. The official requirement says no parsed elements. Integration emits no
  root/edges for `no_tex` and keeps the version in QA. Related fixture expectations
  were corrected, with explicit coverage assertions retained.
- **Windows:** `resource` is optional. On Windows the lifetime peak is the process
  peak working set from psutil; Linux retains `getrusage`. The measurement source is
  labeled, not silently substituted. Sampling still measures the process tree.
- **Arrow:** integer bounds are checked before conversion so out-of-range int32
  values consistently raise a contextual ValueError, including on Windows.
- **UTF-8:** the profiling test reads generated UTF-8 Markdown explicitly instead
  of depending on Windows' default encoding.
- No M1 crawler files or data were changed. Missing `.bbl` cannot be reconstructed
  from absent input; integration does not download archives or alter extraction.

## Run later, after corpus preparation

Python 3.12 is used for local tests. Install repository `requirements.txt`.
From the repository root:

```powershell
python -B -m m2.src.parsing.cli --run-dir "<M1_CORPUS_ROOT>" --ids-file "<IDS.txt>" --output-dir "<NEW_M2_OUTPUT_ROOT>" --label "<UNIQUE_LABEL>"
```

`run-dir` contains `papers/` with extracted canonical M1 manifests/sources. It is
not a ZIP or the parent of retry runs. Resolve verified Drive shortcuts first.
Consolidate retries outside this parser. ID lists may be non-contiguous and use
M1's existing load_ids rules; no member range is hardcoded.

The output must be outside the M1 run. Existing labels are refused, not resumed or
overwritten. There is no resume/checkpoint guarantee in this initial M2 CLI.
For a later retry, use a separate output label and an explicit ID list.

Outputs:

- `benchmark_<label>.json`: measured processing/export/merge wall time, RAM,
  counts, sizes, checksums and separate validation/checksum timings.
- `<label>/tables/{elements,hierarchy,bibtex}.parquet`: the three final schemas.
- `<label>/tables/hierarchy_internal.parquet`: auxiliary sibling/preorder data;
  position is not added to the official four-column hierarchy.
- `<label>/tables/qa_report.json`: per-version coverage and table validation.
- `<label>/processing_report.json`: publication failures, version statuses,
  warnings and successful-publication stage-duration sums.

Capture stderr for detailed component logging. Benchmark `publications_ok` means
the publication call/export completed, not that every version parsed successfully;
use QA status counts for parse success. Versions that failed before loading have
unknown coverage and appear in publication errors, not the known-version denominator.
CLI exits nonzero for publication exceptions or table-validation failure; partial
or failed versions remain explicit in QA and must still be reviewed.

## Validation and remaining gates

```powershell
python -B -m pytest m2/tests -q -p no:cacheprovider
```

The integration fixture exercises nested source inclusion, unused files, multiple
versions, key renaming, shared sentences, missing manifests, no-TeX, immutable M1
inputs, partial gathering, version/publication failures, export and validation.
It does not replace a real publication pilot or a CPU-only Colab benchmark.
No corpus processing or notebook modification was performed during integration.

Before a full run, review representative real papers from each member and confirm
the proposed structural dedup policy in `decisions/dedup_export.md`: contextual
heading/list identity is retained to avoid mixing children, whereas prose/math/
figures use exact normalized content. This can retain equal headings under changed
parents and needs an explicit group decision against the full-text requirement.
That handoff document contains historical proposals; this integration document
overrides its obsolete no-TeX-root behavior. Gathering remains a bounded literal
TeX interpreter, with unsupported constructs explicitly partial/failed.
Missing bibliography sources, unsupported TeX and real-data coverage remain QA
limitations; passing unit tests does not establish completion of milestone 2.
