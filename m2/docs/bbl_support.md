# BBL retention and BibTeX conversion

Fresh normal M1 crawls preserve original text assets in original_assets/ and retain the downloaded archive for retry. Supported BBL and other text formats are converted into .tex/.bib parser inputs in source/. The manifest lists these derived inputs; source_assets_audit.json records original sizes, all filtering decisions and conversion diagnostics. Legacy .tex/.bib/.bbl extraction remains available with preserve_source_assets=false. A support bibliography alone does not create a document. See [source preservation and retry](../../m1/docs/SOURCE_RECOVERY.md).

M2 chooses the compilation root using its existing algorithm. It loads a manifest-declared BBL matching the root basename (root directory or source top level), or an explicitly included BBL file. It ignores unrelated BBL files. If two root BBL paths compete, it emits AMBIGUOUS_BBL_RESOURCE and does not guess.

The existing parse_bibitems(), normalize_entry() and unify_bibliography() handle BBL citation entries and cross-version deduplication. Biber/BibLaTeX BBL files containing structured `\\entry` blocks are now processed by `m2/src/parsing/biblatex_bbl.py`: supported metadata includes `\\field`, `\\verb`, `\\name{author}` and `\\list`; all records still pass through `normalize_entry()` and cross-version deduplication. Malformed/truncated records emit BBL_* diagnostics. This is a conservative subset, not full coverage of all Biber versions or custom macros. The M2 Parquet schemas in export.py remain unchanged.

Existing M1 ZIPs are not modified, and discarded BBL files cannot be reconstructed from them. Re-download original source archives if needed, and re-extract to a NEW M1 run rather than mutating completed runs. Preserve old ZIPs.

Tests: python -m pytest m1/tests/test_extract.py m1/tests/test_validation.py m2/tests/parsing/test_gathering.py m2/tests/parsing/test_bibliography.py m2/tests/parsing/test_pipeline.py

Only the branch feat/m1-m2-preserve-convert-bbl is modified; main is unchanged.


Biber syntax test coverage: multiple entries, nested author names, preserved percent-escaped URL strings in verbatim data, DOI/title/year/journal normalization, duplicate citation keys with .bib precedence, and truncated records. Follow up with sampled real Biber .bbl files for coverage measurements before claiming all references are recovered.


## Regression hardening (post-review)

- A structured Biber `\\entry` missing `\\endentry` is reported as `BBL_TRUNCATED_ENTRY`; its metadata is not merged with the next entry. The parser resumes at the next record, and subsequent valid records can still be recovered.
- BBL dialect detection happens only after stripping LaTeX comments. A commented `\\entry` cannot hide genuine `\\bibitem` references.
- A `.bib` followed by a compiled `.bbl` with the same citation key is normal. Native populated values are kept; compatible missing fields can be enriched. Comparison uses canonical fields and conservative identity checks, with per-source decisions in bibliography_reconciliation. Identical or complementary BBL metadata does not demote an otherwise `ok` paper to `partial`; conflicting fields are still reported. New integration tests exercise end-to-end QA status.

Note: This branch keeps the existing M2 Parquet schemas and does not modify input M1 ZIPs. Run pytest before merging; test additions alone do not constitute a passing CI run.
