# Reviewed roots, conservative bibliography recovery and QA

These behaviors supplement the shared M2 interfaces without changing the six
dataclasses, `gather_source(source)` or `process_publication(run_dir, arxiv_id)`.
No M1 source is edited and no network lookup or download is performed.

## Multiple independent roots

The default gatherer still requires exactly one active, manifest-listed root.
It does not choose the longest file, a preferred filename or the first candidate.
If inspection establishes which document is the publication's main article,
provide a version-specific root choice to the CLI:

```json
{
  "2507.12345": {
    "3": {
      "root_file": "article.tex",
      "reason": "Reviewed title and article body; the other root is an independent video description"
    }
  }
}
```

The IDs/file above are examples, not a supplied corpus configuration. Store your
reviewed choices outside the source corpus and use a fresh output label:

```powershell
python -B -m m2.src.parsing.cli --run-dir "<M1_RUN>" --ids-file "<IDS.txt>" --output-dir "<OUTPUT>" --label "<NEW_LABEL>" --root-choices "<CHOICES.json>"
```

The choice must be relative, declared by the manifest, and contain one active
documentclass. A non-root, missing file, absolute/escaping path or unknown version
does not bypass validation. A reason is mandatory. The configuration is preserved
in the benchmark and per-version processing report, and gathering records
`ROOT_SELECTED_BY_CONFIG` with all candidates. The version remains partial for
review. Unconfigured ambiguous roots still fail explicitly. This is reviewed
selection, not automatic inference that all multiple-root corpora are resolved.

## Conditional commands

Commands declared by `newcommand`/`def` and related forms are tracked as ordinary
macros during gathering, including declarations in reached input files. A custom
math macro named `ifthen` is not presumed to require a matching primitive `fi`.
Known package conditional commands use their braced argument arity; their unknown
branches remain unexpanded, with a partial warning. Explicit `newif` declarations
take precedence over a package-style command name. Primitive unknown conditions
are not guessed, and arbitrary macro/package execution remains unsupported.

## Bibliography recovery

Normal BibTeX parsing runs first. On failure, recovery uses line-anchored field
assignments within the damaged entry and independently validates bounded values.
A malformed field cannot consume later field assignments. Recovery requires a
valid title plus an author/year/DOI; no title, citation key or field is invented.
Malformed fields are quarantined with warnings, while recoverable fields and the
next valid entry remain available. This is conservative best effort on damaged
syntax, not proof of author intent; ambiguous multi-line fields may be dropped.

`extract_bibliography_detailed(document)` returns entries and contextual diagnostics.
The original `extract_bibliography(document)` still returns only entries. The
pipeline uses the detailed helper so parser warnings are not merely lost in logs.

After canonical key mapping and hierarchy normalization, the pipeline records
unresolved citation keys per version. A version with bibliography parse warnings
or unresolved citations becomes partial, even if its main hierarchy parsed.
The auxiliary processing/QA rows include `bibliography_parse_warnings`,
`missing_citation_keys`, `missing_bibliography_resources` and `root_selection`.
These are audit fields, not columns added to the three final tables. Missing `.bib`
resources use `MISSING_BIB_RESOURCE`, separately from failed TeX includes, so
bibliography absence does not incorrectly inflate the include-resolution metric.

Missing keys are never matched by guessed aliases (for example an author surname
is not automatically treated as another bibitem key). Missing retained resources
remain missing; this code does not re-extract archives or reconstruct absent `.bbl`.
Use a new output label when re-testing; existing artifacts are never overwritten.
