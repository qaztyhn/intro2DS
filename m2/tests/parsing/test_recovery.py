"""Regressions derived from local real-data failures, using synthetic inputs."""
import json

import pytest

from m2.src.parsing.bibliography import parse_bib, extract_bibliography_detailed
from m2.src.parsing.cli import load_root_choices
from m2.src.parsing.contracts import GatheredDocument
from m2.src.parsing.gathering import gather_source, gather_source_with_root
from m2.src.parsing.pipeline import process_publication_detailed
from .test_gathering import source
from .test_pipeline import multiversion, ID  # noqa: F401


def test_reviewed_root_choice_excludes_independent_auxiliary_document(tmp_path):
    item = source(tmp_path, {
        'article.tex': r'\documentclass{article}\begin{document}MAIN\end{document}',
        'video.tex': r'\documentclass{article}\begin{document}VIDEO\end{document}',
    })
    assert gather_source(item).status == 'failed'
    result = gather_source_with_root(item, 'article.tex')
    assert result.status == 'partial' and result.root_file == 'article.tex'
    assert result.included_files == ['article.tex']
    assert 'MAIN' in result.latex and 'VIDEO' not in result.latex
    assert any('ROOT_SELECTED_BY_CONFIG' in w and 'video.tex' in w for w in result.warnings)


@pytest.mark.parametrize('selected', ['../escape.tex', '/outside.tex', 'not-listed.tex', 'body.tex'])
def test_root_choice_cannot_bypass_manifest_or_active_root_checks(tmp_path, selected):
    item = source(tmp_path, {'a.tex': r'\documentclass{article}TEXT', 'body.tex': 'NOT A ROOT'})
    result = gather_source_with_root(item, selected)
    assert result.status == 'failed' and result.latex == ''


def test_author_defined_if_macro_does_not_truncate_following_content(tmp_path):
    item = source(tmp_path, {
        'a.tex': r'\documentclass{article}\input{defs}\begin{document}'
                 r'$\ifthen p q$\input{body}\end{document}',
        'defs.tex': r'\newcommand{\ifthen}[2]{\mathsf{if} #1 \mathsf{then} #2}',
        'body.tex': 'BODY AFTER CUSTOM MACRO',
    })
    result = gather_source(item)
    assert result.status == 'ok' and 'BODY AFTER CUSTOM MACRO' in result.latex
    assert r'\ifthen p q' in result.latex
    assert not any('UNTERMINATED_CONDITIONAL' in w for w in result.warnings)


@pytest.mark.parametrize('macro,args', [
    ('ifcsundef', r'{key}{\input{ghost}}{\input{other}}'),
    ('ifdraft', r'{\input{ghost}}{\input{other}}'),
    ('ifstrequal', r'{a}{b}{\input{ghost}}{\input{other}}'),
])
def test_package_conditionals_preserve_unknown_branches_and_continue(tmp_path, macro, args):
    item = source(tmp_path, {'a.tex': r'\documentclass{article}\begin{document}' + '\\' + macro + args + r'\input{body}\end{document}',
                             'body.tex': 'AFTER CONDITIONAL'})
    result = gather_source(item)
    assert result.status == 'partial'
    assert 'AFTER CONDITIONAL' in result.latex
    assert not any('UNTERMINATED' in w or 'MISSING_DEPENDENCY' in w for w in result.warnings)


def test_newif_declaration_takes_precedence_over_package_macro_name(tmp_path):
    item = source(tmp_path, {'a.tex': r'\documentclass{article}\newif\ifdraft\begin{document}'
                             r'\ifdraft\input{ghost}\else\input{other}\fi AFTER\end{document}'})
    result = gather_source(item)
    assert result.status == 'partial' and 'AFTER' in result.latex
    assert not any('UNTERMINATED' in w or 'ghost' in w for w in result.warnings)
    assert any('MISSING_DEPENDENCY' in w and 'other' in w for w in result.warnings)


def test_missing_bibliography_is_distinct_from_failed_source_include(tmp_path):
    item = source(tmp_path, {'a.tex': r'\documentclass{article}\begin{document}'
                             r'\bibliography{absent}\end{document}'})
    result = gather_source(item)
    assert result.status == 'partial'
    assert any('MISSING_BIB_RESOURCE' in w for w in result.warnings)
    assert not any('MISSING_DEPENDENCY' in w for w in result.warnings)


BROKEN = r'''@article{first,
 title = {Shared reference},
 author = {Doe, Jane},
 year = {2020},
 publisher = {{\\\}Broken Press},
 doi = {10.1000/example},
 url = {https://example.org/valid},
}
@book{next, title={Next}, year={2024}}
'''


def test_malformed_publisher_is_quarantined_without_swallowing_following_fields():
    entries, warnings = parse_bib(BROKEN)
    assert [e.original_key for e in entries] == ['first', 'next']
    assert entries[0].fields['doi'] == '10.1000/example'
    assert entries[0].fields['title'] == 'Shared reference'
    assert 'publisher' not in entries[0].fields
    assert any("quarantined malformed field 'publisher'" in w for w in warnings)
    assert any('recovered independently valid fields' in w for w in warnings)


def test_recovery_does_not_invent_a_missing_title_or_key():
    text = '@article{bad,\n publisher = {broken,\n year = {2020},\n}\n@book{good,title={Good}}'
    entries, warnings = parse_bib(text)
    assert [e.original_key for e in entries] == ['good']
    assert warnings


def test_detailed_bibliography_reports_recovery_without_mutating_document():
    doc = GatheredDocument(ID, 1, 'a.tex', '', ['a.tex'], {'r.bib': BROKEN}, 'ok')
    entries, warnings = extract_bibliography_detailed(doc)
    assert len(entries) == 2 and warnings
    assert doc.status == 'ok' and not doc.warnings and doc.bib_files['r.bib'] == BROKEN


def test_pipeline_reports_recovered_bibliography_and_unresolved_keys_as_partial(multiversion):
    src = multiversion / 'papers' / ID.replace('.', '-') / 'versions/v1/source'
    (src / 'refs.bib').write_text(BROKEN, encoding='utf-8')
    body = src / 'body.tex'
    body.write_text(body.read_text() + r' A missing cite \cite{undefined}.')
    result = process_publication_detailed(multiversion, ID)
    row = result.version_status[0]
    assert row['status'] == 'partial' and row['stage'] == 'bibliography'
    assert row['bibliography_parse_warnings'] > 0
    assert row['missing_citation_keys'] == ['undefined']
    assert result.version_status[1]['status'] == 'ok'


def test_root_choices_require_reason_safe_path_and_canonical_version(tmp_path):
    path = tmp_path / 'choices.json'
    choice = {ID: {'1': {'root_file': 'article.tex', 'reason': 'Reviewed title and body'}}}
    path.write_text(json.dumps(choice))
    assert load_root_choices(path)[ID][1]['root_file'] == 'article.tex'
    for bad in [{ID: {'01': choice[ID]['1']}}, {ID: {'1': {'root_file': '../outside', 'reason': 'x'}}},
                {ID: {'1': {'root_file': 'article.tex', 'reason': ''}}}]:
        path.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            load_root_choices(path)


def test_pipeline_records_reviewed_root_and_reason(multiversion):
    src = multiversion / 'papers' / ID.replace('.', '-') / 'versions/v1/source'
    (src / 'video.tex').write_text(r'\documentclass{article}\begin{document}VIDEO\end{document}')
    path = src.parent / 'manifest.json'
    manifest = json.loads(path.read_text());manifest['source_files'].append('video.tex')
    path.write_text(json.dumps(manifest))
    assert process_publication_detailed(multiversion, ID).version_status[0]['status'] == 'failed'
    choice = {'root_file': 'article.tex', 'reason': 'Reviewed main article versus video description'}
    result = process_publication_detailed(multiversion, ID, root_choices={1: choice})
    assert result.version_status[0]['status'] == 'partial'
    assert result.version_status[0]['root_selection'] == choice
    assert not any('VIDEO' in e['content'] for e in result.elements)
