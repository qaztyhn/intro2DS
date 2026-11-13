# M2 TV1 — M1 adapter and multifile gathering

This branch implements only the shared team dataclasses, M1 version adapter and source gathering. TV2 hierarchy/normalization, TV3 bibliography standardization, TV4 element deduplication/export and full-corpus orchestration are deliberately deferred until the owners integrate. No crawler stages, M1 files, or notebook are modified.

## Public interfaces

```python
from pathlib import Path
from src.parsing.m1_adapter import load_versions
from src.parsing.gathering import gather_source

# Definitions, not a command to run the corpus:
# load_versions(run_dir: Path, arxiv_id: str) -> list[SourceVersion]
# gather_source(source: SourceVersion) -> GatheredDocument
```

`src/parsing/contracts.py` follows the supplied team contract exactly: `SourceVersion`, `GatheredDocument`, `ParsedNode`, `ParsedDocument`, `BibEntry`, and `BibliographyResult`. SourceVersion is frozen. No additional public fields, final table columns, or element IDs are introduced. Share this module with TV2–TV4 before integration.

## M1 adapter

- `run_dir` must be the extracted canonical corpus root containing `papers/`, not a ZIP, retry parent, archive path or source directory. Use the merged 24120046 corpus once tests/integration are complete. There are no owner-specific ID ranges in the implementation.
- Reuses M1 `load_metadata(cfg, arxiv_id)` and `load_manifest(cfg, arxiv_id, version)` and manifest schema validation. The M1 signature `load_paper(cfg, arxiv_id)` eagerly reads `references.json`; using its narrow helpers avoids making corrupt/absent S2 data a dependency of M2.
- Returns the sorted union of metadata versions and observed canonical `versions/vN` directories, not just the latest version. Missing/malformed manifests produce an explicit failed SourceVersion rather than dropping a version. Invalid publication identity, invalid metadata version lists and ambiguous/noncanonical version directory names raise contextual `M1AdapterError`.
- Missing metadata can still expose observed valid manifests, with a logger warning; an unrecorded version is never invented. With no known versions, returns an empty list and logs `NO_RECORDED_VERSIONS`. Future orchestration must report that publication explicitly.
- Metadata discovery failure or M1 paper validation failure alone does not discard otherwise valid TeX. Semantic Scholar references are not consulted.
- Paths and manifest identities are checked. Symlinks are rejected. If Drive presents the run via a shortcut, supply its verified resolved path first.
- SourceVersion has no source_format field in the agreed contract. The adapter validates the manifest's format without extending that dataclass.

## Gathering behavior

1. A successful M1 download with `extract_status=empty` / `NO_TEX_SOURCE` maps to `no_tex`, with no invented text. Pending/failed input becomes `failed`; explicitly skipped input stays `skipped` with a reason.
2. Reads only manifest-listed `.tex` and `.bib`, never raw archives, images, S2 references, or arbitrary discovered files. Root discovery inspects listed TeX files; only reachable sources enter the gathered text.
3. Finds exactly one active literal `\documentclass` declaration. Root names are not hardcoded. Comments, verbatim-like bodies, common macro definitions, and inactive literal `\iffalse` branches do not introduce roots. Multiple roots or missing/unreadable root candidates fail explicitly instead of guessing `main.tex`.
4. Expands literal braced/bare `\input`, braced `\include` and literal import/subimport/inputfrom/includefrom variants recursively. Repeated include occurrences expand repeatedly; this is not content deduplication. An active-stack check detects cycles.
5. Removes ordinary TeX comments, preserving escaped percent and literal bodies. A `%` comment consumes its line ending. Leaves ordinary document structure, math, captions, citations and figure commands for TV2/TV3.
6. Resolves paths against the including file's directory, root document directory and source root. A single distinct declared match is required. Multiple distinct matches become `AMBIGUOUS_PATH`, not a silent choice. Parent-relative paths are permitted only when normalized inside source root. Case-sensitive manifest matching is portable across Windows/Linux.
7. Literal `\includeonly` is supported. Known `\iftrue` / `\iffalse` choose a branch; unknown conditions remain unexpanded with partial status. No macro/package execution occurs.
8. Resolves `\bibliography{...}` and `\addbibresource[...]{...}` reached through active TeX. Only those `.bib` contents enter `bib_files`. Unused `.bib` files are not indiscriminately included. Inline `thebibliography` / `\bibitem` stays in `latex` for TV3 extraction; TV2 must exclude bibliography from the final main hierarchy.

### Result conventions

- `included_files`: unique first-visit **TeX** paths, including the root. Repeated occurrence text is still expanded multiple times. Bibliography paths are the keys of `bib_files`.
- `status='ok'`: supported gathering completed without warnings. This does not certify that LaTeX compiles or that TV2–TV4 have parsed it correctly.
- `partial`: recoverable missing/cyclic/ambiguous/dynamic dependency, uncertain decoding, or unsupported conditional/macro behavior. Unresolved commands are retained and contextual warnings include ID, version, stage, origin and code. Do not silently treat partial as ok or discard it without QA during later orchestration.
- `failed`: no unambiguous root, invalid/unready input, unsafe source/root scan, or resource limit. Text output is empty; source data is never changed.
- `no_tex`: explicit M1 empty/no-TeX record, distinct from download/extraction failure.

## Supported subset and limits

This is bounded literal gathering, not a complete TeX interpreter. Common macro definitions are preserved but not executed; definitions containing gathering directives emit `DEFERRED_MACRO_DIRECTIVE` when used in the compilation path. Unknown conditional blocks and dynamic paths are preserved and warned. `subfiles` package semantics are not implemented; `\subfile` is flagged. Legacy `\documentstyle` does not satisfy the official documentclass root rule. Ambiguous cases require a deliberate future policy rather than a heuristic filename preference.

`\endinput` stops the remainder of the file; same-line trailing tokens are kept unexpanded with a warning. Complex catcode changes, aliases and arbitrary macro/package behavior are outside this implementation's support. No attempt is made to compile, execute shell escapes or download missing `.sty`, `.cls`, `.bbl`, images or other resources.

UTF-8/BOM is preferred. UTF-16 BOM and byte-preserving Latin-1 fallback are explicitly warned and produce partial status; fallback is not a claim that the original encoding was Latin-1. NUL-containing or unreadable sources fail closed. Default bounds: 64 MiB/file, 512 MiB of bytes read per version, 128 Mi characters expanded output, 128 include/conditional nesting depth. Exceeding a limit is reported, not silently truncated.

## Tests only — no corpus processing

```powershell
.venv\Scripts\python.exe -B -m pytest tests/parsing -q -p no:cacheprovider
```

Fixtures under `tests/parsing/fixtures/` are synthetic, including single-file, multifile and nested include layouts. The existing synthetic M1 fixture supplies a multiversion TeX/PDF-only adapter integration case. Tests also cover comments/literals, repeated includes, missing files/cycles, relative/unsafe/ambiguous paths, bibliography selection, conditional/macro limits, encoding, no-TeX, schema failures, and immutable inputs.

Tests do not call APIs, compile LaTeX, crawl, or process the real 24120046/other members' corpora. No corpus runner or production TV2–TV4 integration is shipped in this step. Later TV1 orchestration should process a whole publication's versions together, catch publication-level adapter errors, preserve each version's status and warnings, and integrate bibliography mapping before hierarchy normalization as specified by the team contract.
