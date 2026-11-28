import hashlib
import json

import pytest

from m1.src.compilation import compilation_record, MAX_COMPILATION_BYTES
from m2.src.parsing.contracts import SourceVersion
from m2.src.parsing.gathering import gather_source


def source(tmp_path, files):
    base = tmp_path/'source'
    base.mkdir()
    for name, text in files.items():
        path = base/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    return SourceVersion('2507.01659', 1, base, tuple(files), 'ok', 'ok')


def hint(tmp_path, sources):
    raw = json.dumps({'sources': sources, 'process': {'compiler': 'pdflatex'}}).encode()
    assets = tmp_path/'original_assets'
    assets.mkdir()
    (assets/'00README.json').write_bytes(raw)
    record = compilation_record(raw, '2507.01659', 1)
    (tmp_path/'recovery_compilation.json').write_text(json.dumps(record))
    return record


def test_verified_unique_toplevel_ignores_template_root_without_partial(tmp_path):
    item = source(tmp_path, {'article.tex':r'\documentclass{article}Real article',
                            'template.tex':r'\documentclass{article}Unused template',
                            'refs.bib':'@article{x,title={Actual reference}}',
                            'article.bbl':r'\bibitem{x} Actual reference'})
    hint(tmp_path, [{'filename':'article.tex','usage':'toplevel'},
                    {'filename':'template.tex','usage':'ignore'},
                    {'filename':'refs.bib','usage':'ignore'},
                    {'filename':'article.bbl','usage':'ignore'}])
    result = gather_source(item)
    assert result.root_file == 'article.tex'
    assert result.status == 'ok', result.warnings
    assert 'Unused template' not in result.latex
    assert 'article.bbl' in result.bib_files


def test_multiple_actual_toplevels_remain_ambiguous(tmp_path):
    item = source(tmp_path, {'main.tex':r'\documentclass{article}Article',
                            'supplement.tex':r'\documentclass{article}Supplement'})
    hint(tmp_path, [{'filename':'main.tex','usage':'toplevel'},
                    {'filename':'supplement.tex','usage':'toplevel'}])
    result = gather_source(item)
    assert result.status == 'failed'
    assert any('AMBIGUOUS_ROOT' in w for w in result.warnings)


def test_multiple_declared_compilations_are_not_overridden_by_include_graph(tmp_path):
    item = source(tmp_path, {'main.tex':r'\documentclass{article}\input{other}',
                            'other.tex':r'\documentclass{article}Standalone other'})
    hint(tmp_path, [{'filename':'main.tex','usage':'toplevel'},
                    {'filename':'other.tex','usage':'toplevel'}])
    assert gather_source(item).status == 'failed'


def test_absent_hint_keeps_existing_ambiguous_behavior(tmp_path):
    result = gather_source(source(tmp_path, {'main.tex':r'\documentclass{article}Main',
                                             'other.tex':r'\documentclass{article}Other'}))
    assert result.status == 'failed'
    assert any('AMBIGUOUS_ROOT' in w for w in result.warnings)


@pytest.mark.parametrize('mutation', ['hash', 'identity', 'version', 'external_path', 'forged_roots'])
def test_untrusted_compilation_sidecar_is_not_used(tmp_path, mutation):
    item = source(tmp_path, {'main.tex':r'\documentclass{article}Main',
                            'other.tex':r'\documentclass{article}Other'})
    record = hint(tmp_path, [{'filename':'main.tex','usage':'toplevel'}])
    if mutation == 'hash': record['readme_sha256'] = '0'*64
    elif mutation == 'identity': record['arxiv_id'] = '2507.00001'
    elif mutation == 'version': record['version'] = 2
    elif mutation == 'external_path': record['readme_file'] = '../outside.json'
    else: record['toplevels'] = ['other.tex']
    (tmp_path/'recovery_compilation.json').write_text(json.dumps(record))
    result = gather_source(item)
    assert result.status == 'failed'
    assert any('INVALID_COMPILATION_METADATA' in w for w in result.warnings)
    assert any('AMBIGUOUS_ROOT' in w for w in result.warnings)


def test_invalid_original_readme_path_is_rejected(tmp_path):
    raw = json.dumps({'sources':[{'filename':'../escape.tex','usage':'toplevel'}]}).encode()
    with pytest.raises(ValueError):
        compilation_record(raw, '2507.01659', 1)


def test_oversized_sidecar_is_bounded_and_ignored(tmp_path):
    item = source(tmp_path, {'main.tex':r'\documentclass{article}Main'})
    (tmp_path/'recovery_compilation.json').write_bytes(b' '*(MAX_COMPILATION_BYTES+1))
    result = gather_source(item)
    assert result.root_file == 'main.tex'
    assert result.status == 'partial'
    assert any('INVALID_COMPILATION_METADATA' in w for w in result.warnings)


def test_hint_cannot_select_manifest_file_without_active_root(tmp_path):
    item = source(tmp_path, {'main.tex':r'\documentclass{article}Main',
                            'other.tex':r'\documentclass{article}Other', 'fragment.tex':'Fragment'})
    hint(tmp_path, [{'filename':'fragment.tex','usage':'toplevel'}])
    assert gather_source(item).status == 'failed'


def test_active_printbibliography_attaches_only_jobname_converted_biber(tmp_path):
    result = gather_source(source(tmp_path, {
        'main.tex':r'\documentclass{article}\begin{document}Text.\printbibliography\end{document}',
        'main.bbl.bib':'@article{x,title={Recovered}}',
        'unused.bbl.bib':'@article{unused,title={Unused}}'}))
    assert result.status == 'ok', result.warnings
    assert list(result.bib_files) == ['main.bbl.bib']


def test_inactive_printbibliography_does_not_attach_converted_bbl(tmp_path):
    result = gather_source(source(tmp_path, {
        'main.tex':r'\documentclass{article}\iffalse\printbibliography\fi',
        'main.bbl.bib':'@article{x,title={Not active}}'}))
    assert result.status == 'ok', result.warnings
    assert not result.bib_files


def test_printbibliography_retains_bbl_ambiguity(tmp_path):
    result = gather_source(source(tmp_path, {
        'main.tex':r'\documentclass{article}\printbibliography',
        'main.bbl.bib':'@article{x,title={Biber}}',
        'main.bbl.tex':r'\bibitem{y} Different bibliography'}))
    assert result.status == 'partial'
    assert not result.bib_files
    assert any('AMBIGUOUS_BBL_RESOURCE' in w for w in result.warnings)
