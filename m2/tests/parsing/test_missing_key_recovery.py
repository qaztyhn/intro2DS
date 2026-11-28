"""Citation completeness checks use actual citations and canonical mappings."""
import json

import pytest

from m1.src.downloader.http import make_manifest
from m2.src.parsing.bibliography import cited_keys
from m2.src.parsing.pipeline import process_publication_detailed

ID = '2507.01659'


def run_source(tmp_path, latex, bibliography=None):
    run = tmp_path / 'run'
    paper = run / 'papers' / ID.replace('.', '-')
    version = paper / 'versions/v1'
    source = version / 'source'
    source.mkdir(parents=True)
    (paper / 'metadata.json').write_text(json.dumps({'arxiv_id': ID, 'versions': [1]}))
    (source / 'main.tex').write_text(latex, encoding='utf-8')
    files = ['main.tex']
    if bibliography is not None:
        (source / 'refs.bib').write_text(bibliography, encoding='utf-8')
        files.append('refs.bib')
    manifest = make_manifest(ID, 1)
    manifest.update(download_status='ok', extract_status='ok', source_format='tar', source_files=files, error_code=None)
    (version / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return process_publication_detailed(run, ID)


def test_nocite_wildcard_and_bst_control_are_not_reference_keys():
    text = r'\nocite{*,Smith}\bstctlcite{IEEEexample:BSTcontrol}\cite{Missing}\citep[see]{Real}'
    assert cited_keys(text) == {'Smith', 'Missing', 'Real'}
    assert cited_keys(r'\cite{*}') == {'*'}


def test_case_only_citation_key_does_not_make_valid_version_partial(tmp_path):
    result = run_source(tmp_path,
        r'\documentclass{article}\begin{document}Text \cite{SMITH2024}.\bibliography{refs}\end{document}',
        '@article{Smith2024,title={Actual Title},author={Smith, John},year={2024}}')
    assert result.version_status[0]['missing_citation_keys'] == []
    assert result.version_status[0]['status'] == 'ok', result.warnings


def test_genuine_unknown_citations_still_reported_after_case_resolution(tmp_path):
    result = run_source(tmp_path,
        r'\documentclass{article}\begin{document}\cite{SMITH2024,missing}\bibliography{refs}\end{document}',
        '@article{Smith2024,title={Actual Title},year={2024}}')
    assert result.version_status[0]['missing_citation_keys'] == ['missing']


def test_inline_amsrefs_entries_are_extracted_and_excluded_from_hierarchy(tmp_path):
    result = run_source(tmp_path,
        r'\documentclass{article}\begin{document}\section{Body}Text \cite{SMITH}.'
        r'\begin{bibdiv}\begin{biblist}'
        r'\bib{Smith}{article}{author={Smith, John},title={Hidden Bibliography Title},date={2024}}'
        r'\end{biblist}\end{bibdiv}\end{document}')
    assert result.version_status[0]['status'] == 'ok', result.warnings
    assert len(result.bibtex) == 1
    assert result.bibtex[0]['authors'] == ['John Smith']
    assert result.bibtex[0]['year'] == 2024
    assert not any('Hidden Bibliography Title' in row['content'] for row in result.elements)


def test_metadata_control_citations_do_not_create_false_missing_keys(tmp_path):
    result = run_source(tmp_path,
        r'\documentclass{article}\begin{document}\nocite{*}'
        r'\bstctlcite{IEEEexample:BSTcontrol}Actual text.\end{document}')
    assert result.version_status[0]['missing_citation_keys'] == []
