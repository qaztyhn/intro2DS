"""Preserve loss/uncertainty diagnostics when M1 emits derived TeX/BibTeX."""
import json

import pytest

from m1.src.downloader.http import make_manifest
from m2.src.parsing.m1_adapter import load_recovery_warnings
from m2.src.parsing.pipeline import process_publication_detailed

ID = '2507.01659'


def corpus(tmp_path, warnings=None, *, converted=False, empty=False):
    run = tmp_path / 'run'
    paper = run / 'papers' / ID.replace('.', '-')
    version = paper / 'versions/v1'
    source = version / 'source'
    source.mkdir(parents=True)
    (paper / 'metadata.json').write_text(json.dumps({'arxiv_id': ID, 'versions': [1]}))
    files = {}
    if not empty:
        files['main.tex'] = (r'\documentclass{article}\begin{document}'
                             r'An example \cite{x}.\bibliography{refs}'
                             + (r'\input{main.bbl.tex}' if converted else '') + r'\end{document}')
        files['refs.bib'] = '@article{x,title={Actual Title},author={Smith, John},year={2024}}'
        if converted:
            files['main.bbl.tex'] = (r'\ifx\natexlab\undefined\def\natexlab#1{#1}\fi'
                                     r'\begin{thebibliography}{1}\bibitem{x} John Smith.'
                                     r'\newblock Actual Title.\newblock \emph{Nature}, 2024.'
                                     r'\end{thebibliography}')
    for name, data in files.items():
        (source / name).write_text(data, encoding='utf-8')
    manifest = make_manifest(ID, 1)
    manifest.update(download_status='ok', extract_status='empty' if empty else 'ok',
                    source_format='pdf' if empty else 'tar', source_files=list(files),
                    error_code='NO_TEX_SOURCE' if empty else None)
    if warnings is not None:
        manifest['recovery_bibliography_warnings'] = warnings
    path = version / 'manifest.json'
    path.write_text(json.dumps(manifest), encoding='utf-8')
    return run, path


def test_conversion_warning_keeps_version_partial_without_losing_usable_data(tmp_path):
    run, path = corpus(tmp_path, ['AMSREFS_UNSUPPORTED_FIELD key=x field=book'])
    original = path.read_bytes()
    result = process_publication_detailed(run, ID)
    row = result.version_status[0]
    assert row['status'] == 'partial'
    assert row['stage'] == 'bibliography' and row['reason'] == 'BIBLIOGRAPHY_WARNINGS'
    assert row['bibliography_parse_warnings'] == 1
    assert len(result.bibtex) == 1 and result.elements
    assert any('RECOVERY_CONVERSION_WARNING' in w for w in result.warnings)
    assert path.read_bytes() == original


@pytest.mark.parametrize('warnings', [None, []])
def test_baseline_and_lossless_recovery_keep_ok(tmp_path, warnings):
    run, _ = corpus(tmp_path, warnings)
    assert process_publication_detailed(run, ID).version_status[0]['status'] == 'ok'


def test_converted_classic_bbl_supplement_merges_with_existing_bib_without_warning(tmp_path):
    run, _ = corpus(tmp_path, converted=True)
    result = process_publication_detailed(run, ID)
    assert result.version_status[0]['status'] == 'ok', result.warnings
    assert result.version_status[0]['bibliography_parse_warnings'] == 0
    assert len(result.bibtex) == 1
    assert result.bibtex[0]['journal'] == 'Nature'
    assert not any('Actual Title' in row['content'] for row in result.elements)


@pytest.mark.parametrize('warnings', ['not a list', [None], [''], ['x' * 16385]])
def test_malformed_recovery_warning_field_fails_closed(tmp_path, warnings):
    run, _ = corpus(tmp_path, warnings)
    with pytest.raises(ValueError, match='recovery_bibliography_warnings'):
        load_recovery_warnings(run, ID, 1)
    assert process_publication_detailed(run, ID).version_status[0]['status'] == 'failed'


def test_no_tex_not_promoted_to_partial_by_recovery_metadata(tmp_path):
    run, _ = corpus(tmp_path, ['Incomplete bibliography'], empty=True)
    assert process_publication_detailed(run, ID).version_status[0]['status'] == 'no_tex'
