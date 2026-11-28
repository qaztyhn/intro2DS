"""Offline integration of all four M2 branches, using synthetic M1 artifacts."""
import json

import pyarrow.parquet as pq
import pytest
from m1.src.downloader.http import make_manifest

from m2.src.parsing import pipeline
from m2.src.parsing.cli import run
from m2.src.parsing.cli import main
from m2.src.parsing.export import export_tables
from m2.src.parsing.validation import validate_tables

ID = '2507.01659'


def test_cli_all_failed_versions_are_not_success(tmp_path):
    directory = tmp_path / 'input'
    paper = directory / 'papers' / ID.replace('.', '-')
    paper.mkdir(parents=True)
    (paper / 'metadata.json').write_text(json.dumps({'arxiv_id': ID, 'versions': [1]}))
    ids = tmp_path / 'ids.txt'
    ids.write_text(ID + '\n')
    output = tmp_path / 'output'
    code = main(['--run-dir', str(directory), '--ids-file', str(ids),
                 '--output-dir', str(output), '--label', 'failed'])
    assert code == 1
    benchmark = json.loads((output / 'benchmark_failed.json').read_text())
    assert benchmark['counts']['publications_ok'] == 0
    assert benchmark['counts']['publications_failed'] == 1
    report = json.loads((output / 'failed/processing_report.json').read_text())
    assert report['publications'][0]['version_status'][0]['status'] == 'failed'


def test_cli_mixed_failed_versions_returns_failure_but_keeps_output(multiversion, tmp_path):
    ids = tmp_path / 'ids.txt'
    ids.write_text(ID + '\n')
    output = tmp_path / 'output'
    assert main(['--run-dir', str(multiversion), '--ids-file', str(ids),
                 '--output-dir', str(output), '--label', 'mixed']) == 1
    assert pq.read_table(output / 'mixed/tables/bibtex.parquet').num_rows == 1


def test_cli_no_tex_is_not_a_processing_failure(tmp_path):
    directory = tmp_path / 'input'
    version = directory / 'papers' / ID.replace('.', '-') / 'versions/v1'
    version.mkdir(parents=True)
    (version.parent.parent / 'metadata.json').write_text(json.dumps({'arxiv_id': ID, 'versions': [1]}))
    manifest = make_manifest(ID, 1)
    manifest.update(download_status='ok', extract_status='empty', source_format='pdf',
                    source_files=[], error_code='NO_TEX_SOURCE')
    (version / 'manifest.json').write_text(json.dumps(manifest))
    ids = tmp_path / 'ids.txt'
    ids.write_text(ID + '\n')
    assert main(['--run-dir', str(directory), '--ids-file', str(ids),
                 '--output-dir', str(tmp_path / 'output'), '--label', 'empty']) == 0


@pytest.fixture
def multiversion(tmp_path):
    directory = tmp_path / 'm1_run'
    paper = directory / 'papers' / ID.replace('.', '-')
    paper.mkdir(parents=True)
    (paper / 'metadata.json').write_text(json.dumps({'arxiv_id': ID, 'versions': [1, 2, 3, 4]}))
    # Malformed S2 must not affect source parsing or bibliography extraction.
    (paper / 'references.json').write_text('not JSON')
    for version, key in [(1, 'first'), (2, 'renamed')]:
        source = paper / f'versions/v{version}/source'
        source.mkdir(parents=True)
        files = {
            'article.tex': r'\documentclass{article}\begin{document}\section{Intro}'
                           r'\input{body}\bibliography{refs}\end{document}',
            'body.tex': r'Identical sentence. A citation \cite{' + key + '}.',
            'unused.tex': 'UNUSED SENTINEL',
            'refs.bib': '@article{' + key + ',title={Shared reference},author={Doe, Jane},year={2020}}',
        }
        for name, text in files.items():
            (source / name).write_text(text, encoding='utf-8')
        manifest = make_manifest(ID, version)
        manifest.update(
                        download_status='ok', extract_status='ok', source_format='tex',
                        source_files=list(files), error_code=None)
        (source.parent / 'manifest.json').write_text(json.dumps(manifest))
    empty = paper / 'versions/v3'
    empty.mkdir()
    manifest = make_manifest(ID, 3)
    manifest.update(download_status='ok', extract_status='empty', source_format='pdf',
                    source_files=[], error_code='NO_TEX_SOURCE')
    (empty / 'manifest.json').write_text(json.dumps(manifest))
    # v4 is recorded but has no manifest: must stay visible as a failed version.
    return directory


def test_real_interfaces_citation_mapping_dedup_no_tex_and_immutable_input(multiversion, tmp_path):
    before = {p: p.read_bytes() for p in multiversion.rglob('*') if p.is_file()}
    result = pipeline.process_publication_detailed(multiversion, ID)
    assert [row['status'] for row in result.version_status] == ['ok', 'ok', 'no_tex', 'failed']
    assert len(result.bibtex) == 1 and result.bib_entries_before == 2
    assert result.bibtex[0]['citation_key'] == 'first'
    assert all('renamed' not in row['content'] and 'UNUSED' not in row['content']
               for row in result.elements)
    assert any(r'\cite{first}' in row['content'] for row in result.elements)
    sentence = [row for row in result.elements if row['kind'] == 'sentence'
                and row['content'] == 'Identical sentence.']
    assert len(sentence) == 1
    assert {row['version'] for row in result.hierarchy if row['element_id'] == sentence[0]['element_id']} == {1, 2}
    assert not any(row['element_id'].endswith(('#root3', '#root4')) for row in result.elements)
    export_tables(result.elements, result.hierarchy, result.bibtex, tmp_path / 'tables')
    assert validate_tables(tmp_path / 'tables')['passed']
    assert all(p.read_bytes() == value for p, value in before.items())
    assert set(before) == {p for p in multiversion.rglob('*') if p.is_file()}


def test_cli_continues_after_publication_failure_and_refuses_reuse(multiversion, tmp_path):
    ids = tmp_path / 'ids.txt'
    ids.write_text('2507.09999\n' + ID + '\n')
    output = tmp_path / 'output'
    benchmark, qa = run(multiversion, ids, output, 'trial')
    assert benchmark['counts']['publications_attempted'] == 2
    assert benchmark['counts']['publications_failed'] == 1
    assert qa['validation']['passed']
    assert qa['versions']['counts']['versions'] == 4
    assert qa['versions']['counts']['by_status']['no_tex'] == 1
    assert pq.read_table(output / 'trial/tables/bibtex.parquet').num_rows == 1
    report = json.loads((output / 'trial/processing_report.json').read_text(encoding='utf-8'))
    assert report['publications'][0]['arxiv_id'] == '2507.09999'
    assert 'error' in report['publications'][0]
    with pytest.raises(FileExistsError):
        run(multiversion, ids, output, 'trial')
    with pytest.raises(ValueError, match='outside'):
        run(multiversion, ids, multiversion / 'm2_output', 'other')


def test_version_failure_does_not_drop_sibling_versions(multiversion, monkeypatch):
    original = pipeline.parse_hierarchy

    def parse(document, mapping):
        if document.version == 1:
            raise ValueError('synthetic parser failure')
        return original(document, mapping)

    monkeypatch.setattr(pipeline, 'parse_hierarchy', parse)
    result = pipeline.process_publication_detailed(multiversion, ID)
    assert [row['status'] for row in result.version_status] == ['failed', 'ok', 'no_tex', 'failed']
    assert {row['version'] for row in result.hierarchy} == {2}
    assert result.version_status[0]['stage'] == 'hierarchy'


def test_partial_gathering_keeps_recoverable_bibliography_and_reports_warning(multiversion):
    body = multiversion / 'papers' / ID.replace('.', '-') / 'versions/v1/source/body.tex'
    body.write_text(body.read_text() + r'\input{missing}')
    result = pipeline.process_publication_detailed(multiversion, ID)
    assert result.version_status[0]['status'] == 'partial'
    assert result.version_status[0]['include_problems'] == 1
    assert result.bib_entries_before == 2 and len(result.bibtex) == 1


def test_unexpected_gather_exception_is_logged_and_isolated(multiversion, monkeypatch, caplog):
    original = pipeline.gather_source_for_review

    def gather(source, reviews):
        if source.version == 1:
            raise RuntimeError('synthetic gather failure')
        return original(source, reviews)

    monkeypatch.setattr(pipeline, 'gather_source_for_review', gather)
    result = pipeline.process_publication_detailed(multiversion, ID)
    assert [row['status'] for row in result.version_status] == ['failed', 'ok', 'no_tex', 'failed']
    assert 'synthetic gather failure' in caplog.text
    assert {row['version'] for row in result.hierarchy} == {2}


def test_m1_manifest_bbl_to_m2_bibtex_schema(tmp_path):
    from m1.src.utils.common import paper_key
    from m2.src.parsing.export import BIBTEX_SCHEMA

    base = tmp_path / "run"
    paper = base / "papers" / paper_key(ID)
    source = paper / "versions/v1/source"
    source.mkdir(parents=True)
    (paper / "metadata.json").write_text(json.dumps({"arxiv_id": ID, "versions": [1]}))
    (source / "main.tex").write_text(
        r"\documentclass{article}\begin{document}\section{Intro}"
        r"A citation \cite{x}.\end{document}")
    (source / "main.bbl").write_text(
        r"\begin{thebibliography}{1}" "\n"
        r"\bibitem{x} A. Smith." "\n"
        r"\newblock A Study in Parsing." "\n"
        r"\newblock \emph{Nature}, 2020." "\n"
        r"\end{thebibliography}")
    manifest = make_manifest(ID, 1)
    manifest.update(download_status="ok", extract_status="ok", source_format="tar",
                    source_files=["main.tex", "main.bbl"], error_code=None)
    (source.parent / "manifest.json").write_text(json.dumps(manifest))
    result = pipeline.process_publication_detailed(base, ID)
    assert result.version_status[0]["status"] == "ok", result.version_status
    assert len(result.bibtex) == 1
    assert result.bibtex[0]["citation_key"] == "x"
    assert result.bibtex[0]["title"] == "A Study in Parsing"
    export_tables(result.elements, result.hierarchy, result.bibtex, tmp_path / "tables")
    assert pq.read_schema(tmp_path / "tables/bibtex.parquet").equals(BIBTEX_SCHEMA)
    assert validate_tables(tmp_path / "tables")["passed"]


def test_m1_bib_and_equivalent_bbl_remain_ok(tmp_path):
    """Loading the compilation root's BBL must not demote a healthy .bib."""
    from m1.src.utils.common import paper_key

    base = tmp_path / "run"
    paper = base / "papers" / paper_key(ID)
    source = paper / "versions/v1/source"
    source.mkdir(parents=True)
    (paper / "metadata.json").write_text(
        json.dumps({"arxiv_id": ID, "versions": [1]}))
    files = {
        "main.tex": (r"\documentclass{article}\begin{document}"
                     r"\section{Intro} Cites \cite{x}.\bibliography{refs}"
                     r"\end{document}"),
        "refs.bib": "@article{x,title={Shared Title},year={2020}}",
        "main.bbl": (r"\entry{x}{article}{}"
                     r"\field{title}{Shared Title}"
                     r"\field{year}{2020}"
                     r"\endentry"),
    }
    for name, content in files.items():
        (source / name).write_text(content, encoding="utf-8")
    manifest = make_manifest(ID, 1)
    manifest.update(download_status="ok", extract_status="ok",
                    source_format="tar", source_files=list(files),
                    error_code=None)
    (source.parent / "manifest.json").write_text(json.dumps(manifest))

    result = pipeline.process_publication_detailed(base, ID)
    assert result.version_status[0]["status"] == "ok", result.version_status
    assert result.version_status[0]["bibliography_parse_warnings"] == 0
    assert len(result.bibtex) == 1
    assert result.bibtex[0]["citation_key"] == "x"
    assert result.bibtex[0]["title"] == "Shared Title"
    export_tables(result.elements, result.hierarchy, result.bibtex,
                  tmp_path / "tables")
    assert validate_tables(tmp_path / "tables")["passed"]


def test_m1_bib_and_conflicting_bbl_are_partial(tmp_path):
    from m1.src.utils.common import paper_key

    base = tmp_path / "run"
    paper = base / "papers" / paper_key(ID)
    source = paper / "versions/v1/source"
    source.mkdir(parents=True)
    (paper / "metadata.json").write_text(
        json.dumps({"arxiv_id": ID, "versions": [1]}))
    files = {
        "main.tex": (r"\documentclass{article}\begin{document}"
                     r"\section{Intro} Cites \cite{x}.\bibliography{refs}"
                     r"\end{document}"),
        "refs.bib": "@article{x,title={Original},year={2020}}",
        "main.bbl": (r"\entry{x}{article}{}"
                     r"\field{title}{Conflicting}"
                     r"\field{year}{2020}"
                     r"\endentry"),
    }
    for name, content in files.items():
        (source / name).write_text(content, encoding="utf-8")
    manifest = make_manifest(ID, 1)
    manifest.update(download_status="ok", extract_status="ok",
                    source_format="tar", source_files=list(files),
                    error_code=None)
    (source.parent / "manifest.json").write_text(json.dumps(manifest))
    result = pipeline.process_publication_detailed(base, ID)
    assert result.version_status[0]["status"] == "partial", result.version_status
    assert result.version_status[0]["bibliography_parse_warnings"] == 1
    assert len(result.bibtex) == 1
    assert result.bibtex[0]["title"] == "Original"


@pytest.mark.parametrize(("message", "missing", "code", "impact"), [
    ("MISSING_BIB_RESOURCE: refs", False, "MISSING_BIB_RESOURCE", "info"),
    ("MISSING_BIB_RESOURCE: refs", True, "MISSING_BIB_RESOURCE", "affects_output"),
    ("AMSREFS_UNSUPPORTED_FIELD key=x field=conference", False, "AMSREFS_UNSUPPORTED_FIELD", "info"),
    ("AMSREFS_UNSUPPORTED_FIELD key=x field=title", False, "AMSREFS_UNSUPPORTED_FIELD", "affects_output"),
    ("NEW_WARNING: unknown", False, "NEW_WARNING", "affects_output"),
])
def test_diagnostic_impact(message, missing, code, impact):
    from m2.src.parsing.diagnostics import classify

    assert classify(message, missing_keys=missing) == (code, impact)


@pytest.mark.parametrize(("second_year", "citation", "reasons", "info"), [
    ("2020", "first", [], ["BIB_DUPLICATE_FIELD_IDENTICAL", "MISSING_BIB_RESOURCE"]),
    ("2021", "first", ["BIB_DUPLICATE_FIELD_DIFFERENT"], ["MISSING_BIB_RESOURCE"]),
    ("2020", "absent", ["MISSING_BIB_RESOURCE", "MISSING_CITATION_KEYS"],
     ["BIB_DUPLICATE_FIELD_IDENTICAL"]),
])
def test_output_impact_in_processing_report(multiversion, tmp_path, second_year, citation, reasons, info):
    source = multiversion / "papers" / ID.replace(".", "-") / "versions/v1/source"
    (source / "refs.bib").write_text(
        "@article{first,title={Shared reference},author={Doe, Jane},year={2020},year={" + second_year + "}}")
    (source / "article.tex").write_text(
        r"\documentclass{article}\begin{document}See \cite{" + citation +
        r"}.\bibliography{refs,unavailable}\end{document}")
    ids = tmp_path / "ids.txt"
    ids.write_text(ID)
    output = tmp_path / "out"
    run(multiversion, ids, output, "impact")
    report = json.loads((output / "impact/processing_report.json").read_text())
    rows = report["publications"][0]["version_status"]
    assert [r["status"] for r in rows] == ["partial" if reasons else "ok", "ok", "no_tex", "failed"]
    assert rows[0]["partial_reasons"] == reasons
    assert rows[0]["info_diagnostics"] == info
    assert all(isinstance(row[key], list) for row in rows for key in ("partial_reasons", "info_diagnostics"))


def test_info_missing_resource_does_not_erase_hierarchy_reason(multiversion):
    source = multiversion / "papers" / ID.replace(".", "-") / "versions/v1/source"
    (source / "article.tex").write_text(
        r"\documentclass{article}\begin{document}Text.\bibliography{unavailable}")
    row = pipeline.process_publication_detailed(multiversion, ID).version_status[0]
    assert row["status"] == "partial"
    assert "DOCUMENT_END_MISSING" in row["partial_reasons"]
    assert row["info_diagnostics"] == ["MISSING_BIB_RESOURCE"]


def test_cli_writes_ambiguous_root_review_beside_qa(multiversion, tmp_path):
    directory = multiversion / "papers" / ID.replace(".", "-") / "versions/v1/source"
    (directory / "unused.tex").write_text(
        r"\documentclass{article}\title{" + "x" * 170 + r"}\verb|\begin{thebibliography}|")
    ids = tmp_path / "ids.txt"
    ids.write_text(ID)
    run(multiversion, ids, tmp_path / "out", "ambiguous")
    tables = tmp_path / "out/ambiguous/tables"
    assert (tables / "qa_report.json").is_file()
    review, = json.loads((tables / "root_review.json").read_text())
    assert (review["arxiv_id"], review["version"]) == (ID, 1)
    assert [r["root_file"] for r in review["candidates"]] == ["article.tex", "unused.tex"]
    candidate = review["candidates"][1]
    assert candidate["title"] == "x" * 160
    assert candidate["readme_role"] is None
    assert not candidate["has_thebibliography"]


def test_direct_root_choice_requires_reason(multiversion):
    with pytest.raises(ValueError, match=rf"{ID}v1 stage=root_selection INVALID_ROOT_SELECTION"):
        pipeline.process_publication_detailed(multiversion, ID, root_choices={
            1: {"root_file": "article.tex", "reason": " "}})
