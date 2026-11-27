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
    original = pipeline.gather_source

    def gather(source):
        if source.version == 1:
            raise RuntimeError('synthetic gather failure')
        return original(source)

    monkeypatch.setattr(pipeline, 'gather_source', gather)
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
