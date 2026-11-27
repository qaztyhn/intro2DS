# BBL retention and BibTeX conversion

New M1 source archives retain regular .tex, .bib, and .bbl files, including their original bytes and paths. The M1 manifest records all retained files and kept_source_bytes includes BBL content. M1 validation and cached extraction use the same accepted suffixes. A source with BBL but no TeX remains NO_TEX_SOURCE.

M2 chooses the compilation root using its existing algorithm. It loads a manifest-declared BBL matching the root basename (root directory or source top level), or an explicitly included BBL file. It ignores unrelated BBL files. If two root BBL paths compete, it emits AMBIGUOUS_BBL_RESOURCE and does not guess.

The existing parse_bibitems(), normalize_entry() and unify_bibliography() handle BBL citation entries and cross-version deduplication. Biblatex BBL files with no parseable bibitem cause BBL_NO_BIBITEM; these are not silently claimed as converted. The M2 Parquet schemas in export.py remain unchanged.

Existing M1 ZIPs are not modified, and discarded BBL files cannot be reconstructed from them. Re-download original source archives if needed, and re-extract to a NEW M1 run rather than mutating completed runs. Preserve old ZIPs.

Tests: python -m pytest m1/tests/test_extract.py m1/tests/test_validation.py m2/tests/parsing/test_gathering.py m2/tests/parsing/test_bibliography.py m2/tests/parsing/test_pipeline.py

Only the branch feat/m1-m2-preserve-convert-bbl is modified; main is unchanged.
