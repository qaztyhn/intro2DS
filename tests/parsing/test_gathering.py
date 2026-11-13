from pathlib import Path
import shutil

import pytest

from src.parsing.contracts import SourceVersion
from src.parsing import gathering as module


def source(tmp_path, files):
    base = tmp_path / 'source'
    base.mkdir()
    for name, content in files.items():
        p = base / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode('utf-8'))
    return SourceVersion('2507.01659', 1, base, tuple(files), 'ok', 'ok')


@pytest.mark.parametrize('fixture,root,files', [
    ('single_file', 'paper.tex', ['paper.tex']),
    ('multi_file', 'article.tex', ['article.tex', 'chapters/introduction.tex', 'methods.tex']),
    ('nested_include', 'manuscript.tex', ['manuscript.tex', 'sections/first.tex', 'sections/second.tex']),
])
def test_fixture_gathering(tmp_path, fixture, root, files):
    base = tmp_path / 'source'
    shutil.copytree(Path(__file__).parent / 'fixtures' / fixture, base)
    names = tuple(p.relative_to(base).as_posix() for p in base.rglob('*') if p.is_file())
    result = module.gather_source(SourceVersion('2507.01659', 1, base, names, 'ok', 'ok'))
    assert result.status == 'ok', result.warnings
    assert result.root_file == root
    assert result.included_files == files
    assert 'UNUSED_SOURCE_MUST_NOT_ENTER' not in result.latex
    if fixture == 'multi_file':
        assert list(result.bib_files) == ['refs.bib']
        assert '\\bibliography{refs}' in result.latex
        assert '\\cite{example2025}' in result.latex


def test_comments_percent_literals_and_unused_bibliography(tmp_path):
    item = source(tmp_path, {
        'root.tex': '\\documentclass{article}\n% \\input{missing}\nA% comment\nB\\% C\n'
                    '\\verb|% \\input{missing}|\n\\begin{verbatim}\\input{missing}\\end{verbatim}',
        'unused.bib': '@article{x,title={Unused}}'})
    result = module.gather_source(item)
    assert result.status == 'ok'
    assert 'AB\\% C' in result.latex
    assert '% \\input{missing}' in result.latex  # Literal content is preserved.
    assert result.bib_files == {}


@pytest.mark.parametrize('root', ['main.tex', 'paper.tex', 'nested/document.tex'])
def test_root_name_is_not_hardcoded(tmp_path, root):
    result = module.gather_source(source(tmp_path, {root: '\\documentclass{article}X'}))
    assert result.status == 'ok' and result.root_file == root


def test_repeated_include_expands_twice_but_inventory_is_unique(tmp_path):
    result = module.gather_source(source(tmp_path, {
        'r.tex': '\\documentclass{article}A\\input{part}B\\input{part}C', 'part.tex': 'X'}))
    assert result.status == 'ok'
    assert result.latex.endswith('AXBXC')
    assert result.included_files == ['r.tex', 'part.tex']


def test_missing_include_keeps_command_and_returns_partial(tmp_path):
    result = module.gather_source(source(tmp_path, {'r.tex': '\\documentclass{article}\\input{absent}'}))
    assert result.status == 'partial'
    assert '\\input{absent}' in result.latex
    assert any('MISSING_DEPENDENCY' in w and '2507.01659v1' in w for w in result.warnings)


def test_cycle_is_not_global_dedup_and_does_not_recurse_forever(tmp_path):
    result = module.gather_source(source(tmp_path, {'r.tex': '\\documentclass{article}\\input{a}',
        'a.tex': '\\input{b}', 'b.tex': '\\input{a}'}))
    assert result.status == 'partial'
    assert any('INCLUDE_CYCLE' in w for w in result.warnings)
    assert result.included_files == ['r.tex', 'a.tex', 'b.tex']


@pytest.mark.parametrize('files,code', [
    ({'a.tex': 'No class'}, 'NO_ROOT'),
    ({'a.tex': '\\documentclass{article}', 'main.tex': '\\documentclass{article}'}, 'AMBIGUOUS_ROOT'),
    ({'a.tex': '\\documentclass{article}\\documentclass{book}'}, 'MULTIPLE_ROOT_DECLARATIONS'),
])
def test_root_failures_are_explicit(tmp_path, files, code):
    result = module.gather_source(source(tmp_path, files))
    assert result.status == 'failed' and not result.latex
    assert any(code in w for w in result.warnings)


def test_relative_parent_include_inside_source_and_ambiguity(tmp_path):
    item = source(tmp_path, {'r.tex': '\\documentclass{article}\\input{section/a}',
        'section/a.tex': '\\input{../shared}', 'shared.tex': 'SHARED'})
    result = module.gather_source(item)
    assert result.status == 'ok' and 'SHARED' in result.latex
    # Both file-relative and root-relative matches must not be chosen silently.
    (item.source_dir / 'section/part.tex').write_text('LOCAL')
    (item.source_dir / 'part.tex').write_text('GLOBAL')
    (item.source_dir / 'section/a.tex').write_text('\\input{part}')
    item = SourceVersion(item.arxiv_id, 1, item.source_dir,
        (*item.source_files, 'section/part.tex', 'part.tex'), 'ok', 'ok')
    result = module.gather_source(item)
    assert result.status == 'partial' and any('AMBIGUOUS_PATH' in w for w in result.warnings)
    assert 'LOCAL' not in result.latex and 'GLOBAL' not in result.latex


@pytest.mark.parametrize('target', ['../outside.tex', '/etc/passwd', 'C:/secret.tex', '\\filename'])
def test_unsafe_or_dynamic_include_is_never_opened(tmp_path, target):
    item = source(tmp_path, {'r.tex': '\\documentclass{article}\\input{' + target + '}'})
    result = module.gather_source(item)
    assert result.status == 'partial'
    assert len(result.included_files) == 1
    assert any('UNSAFE_PATH' in w or 'DYNAMIC_PATH' in w for w in result.warnings)


def test_unlisted_existing_file_is_not_gathered(tmp_path):
    item = source(tmp_path, {'r.tex': '\\documentclass{article}\\input{extra}'})
    (item.source_dir / 'extra.tex').write_text('UNDECLARED')
    result = module.gather_source(item)
    assert result.status == 'partial' and 'UNDECLARED' not in result.latex


def test_includeonly_and_known_conditionals(tmp_path):
    result = module.gather_source(source(tmp_path, {
        'r.tex': '\\documentclass{article}\\includeonly{a}\\include{a}\\include{b}'
                 '\\iffalse\\input{missing}\\else YES\\fi\\iftrue TRUE\\else FALSE\\fi',
        'a.tex': 'ALLOWED', 'b.tex': 'EXCLUDED'}))
    assert result.status == 'ok', result.warnings
    assert 'ALLOWED' in result.latex and 'EXCLUDED' not in result.latex
    assert 'YES' in result.latex and 'TRUE' in result.latex and 'FALSE' not in result.latex
    assert result.included_files == ['r.tex', 'a.tex']


def test_unknown_condition_and_macro_directive_not_followed(tmp_path):
    result = module.gather_source(source(tmp_path, {'r.tex': '\\documentclass{article}'
        '\\newcommand{\\load}{\\input{missing}}\\newif\\iffoo'
        '\\iffoo\\input{missing}\\else\\input{also_missing}\\fi'}))
    assert result.status == 'partial'
    assert any('DEFERRED_MACRO_DIRECTIVE' in w for w in result.warnings)
    assert any('UNRESOLVED_CONDITIONAL' in w for w in result.warnings)
    assert not any('MISSING_DEPENDENCY' in w for w in result.warnings)


def test_macro_and_literal_documentclass_are_not_roots(tmp_path):
    result = module.gather_source(source(tmp_path, {'r.tex': '\\newcommand{\\demo}{\\documentclass{article}}'
        '\\begin{verbatim}\\documentclass{book}\\end{verbatim}'}))
    assert result.status == 'failed' and any('NO_ROOT' in w for w in result.warnings)


def test_bib_resources_only_from_active_tex_and_preserve_figures(tmp_path):
    result = module.gather_source(source(tmp_path, {
        'r.tex': '\\documentclass{article}\\addbibresource[location=local]{refs.bib}'
                 '\\begin{figure}\\includegraphics{not-retained.png}\\caption{Text}\\end{figure}',
        'refs.bib': '@article{x,title={T}}', 'unused.tex': '\\bibliography{missing}'}))
    assert result.status == 'ok', result.warnings
    assert result.bib_files == {'refs.bib': '@article{x,title={T}}'}
    assert '\\includegraphics{not-retained.png}' in result.latex


def test_no_tex_and_source_failure_states(tmp_path):
    no_tex = SourceVersion('2507.01659', 2, tmp_path / 'absent', (), 'ok', 'empty', 'NO_TEX_SOURCE')
    assert module.gather_source(no_tex).status == 'no_tex'
    failed = SourceVersion('2507.01659', 3, tmp_path, (), 'failed', 'pending', 'NOT_FOUND')
    result = module.gather_source(failed)
    assert result.status == 'failed' and any('NOT_FOUND' in w for w in result.warnings)


def test_declared_missing_file_blocks_root_certainty(tmp_path):
    item = source(tmp_path, {'r.tex': '\\documentclass{article}'})
    item = SourceVersion(item.arxiv_id, 1, item.source_dir, (*item.source_files, 'missing.tex'), 'ok', 'ok')
    result = module.gather_source(item)
    assert result.status == 'failed' and any('ROOT_SCAN_INCOMPLETE' in w for w in result.warnings)


def test_encoding_fallback_reported_only_for_used_file(tmp_path):
    item = source(tmp_path, {'r.tex': '\\documentclass{article}Body', 'unused.tex': b'\xff unused'})
    assert module.gather_source(item).status == 'ok'
    (item.source_dir / 'r.tex').write_bytes(b'\\documentclass{article}\n\xff')
    result = module.gather_source(item)
    assert result.status == 'partial' and any('ENCODING_LATIN1_FALLBACK' in w for w in result.warnings)


def test_limits_are_explicit_and_input_is_unchanged(tmp_path, monkeypatch):
    item = source(tmp_path, {'r.tex': '\\documentclass{article}\\input{part}\\input{part}', 'part.tex': 'X' * 100})
    before = {p: p.read_bytes() for p in item.source_dir.rglob('*') if p.is_file()}
    monkeypatch.setattr(module, 'MAX_OUTPUT_CHARS', 120)
    result = module.gather_source(item)
    assert result.status == 'failed' and any('OUTPUT_SIZE_LIMIT' in w for w in result.warnings)
    assert all(p.read_bytes() == value for p, value in before.items())


def test_symlink_source_refused_before_content_read(tmp_path, monkeypatch):
    item = source(tmp_path, {'r.tex': '\\documentclass{article}'})
    target = item.source_dir / 'r.tex'
    original = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda path: path == target or original(path))
    result = module.gather_source(item)
    assert result.status == 'failed' and any('symlink' in w for w in result.warnings)


def test_literal_import_and_nested_subimport(tmp_path):
    result = module.gather_source(source(tmp_path, {
        'r.tex': '\\documentclass{article}\\import{parts/}{first}',
        'parts/first.tex': 'FIRST\\subimport{more/}{second.tex}',
        'parts/more/second.tex': 'SECOND'}))
    assert result.status == 'ok'
    assert 'FIRSTSECOND' in result.latex


def test_includeonly_normalizes_dot_paths_and_does_not_hide_dynamic_target(tmp_path):
    result = module.gather_source(source(tmp_path, {
        'r.tex': '\\documentclass{article}\\includeonly{./a.tex}\\include{a}\\include{\\target}',
        'a.tex': 'ALLOWED'}))
    assert 'ALLOWED' in result.latex
    assert result.status == 'partial' and any('DYNAMIC_PATH' in w for w in result.warnings)


def test_inactive_root_and_comment_environment_do_not_create_ambiguity(tmp_path):
    result = module.gather_source(source(tmp_path, {'r.tex': '\\documentclass{article}REAL',
        'unused.tex': '\\iffalse\\documentclass{book}\\fi\\begin{comment}\\documentclass{report}\\end{comment}'}))
    assert result.status == 'ok' and result.root_file == 'r.tex'


def test_inline_bibliography_kept_for_tv3_and_endinput_stops_tail(tmp_path):
    result = module.gather_source(source(tmp_path, {
        'r.tex': '\\documentclass{article}\\input{b}',
        'b.tex': '\\begin{thebibliography}{1}\\bibitem{k}Entry\\end{thebibliography}\n\\endinput\n\\input{missing}'}))
    assert result.status == 'ok'
    assert '\\bibitem{k}' in result.latex and 'missing' not in result.latex


def test_file_and_include_depth_limits_are_reported(tmp_path, monkeypatch):
    item = source(tmp_path, {'r.tex': '\\documentclass{article}\\input{a}', 'a.tex': '\\input{b}', 'b.tex': 'B'})
    monkeypatch.setattr(module, 'MAX_DEPTH', 2)
    result = module.gather_source(item)
    assert result.status == 'failed' and any('INCLUDE_DEPTH_LIMIT' in w for w in result.warnings)
    monkeypatch.setattr(module, 'MAX_FILE_BYTES', 2)
    result = module.gather_source(item)
    assert result.status == 'failed' and any('SOURCE_SIZE_LIMIT' in w for w in result.warnings)


def test_math_iff_does_not_hide_later_includes(tmp_path):
    result = module.gather_source(source(tmp_path, {
        'r.tex': '\\documentclass{article}$a\\iff b$\\input{part}', 'part.tex': 'REACHED'}))
    assert result.status == 'ok', result.warnings
    assert 'REACHED' in result.latex


def test_conditional_macro_with_unbraced_control_token_does_not_follow_branches(tmp_path):
    result = module.gather_source(source(tmp_path, {
        'r.tex': '\\documentclass{article}\\ifdef\\somecmd{\\input{absent}}{\\input{also_absent}}'}))
    assert result.status == 'partial'
    assert any('UNRESOLVED_CONDITIONAL' in w for w in result.warnings)
    assert not any('MISSING_DEPENDENCY' in w for w in result.warnings)
