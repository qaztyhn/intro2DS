"""Recovery cases observed in the four-range corpus audit."""

from m2.src.parsing.contracts import SourceVersion
from m2.src.parsing.gathering import Context, gather_source, root_count


def source(tmp_path, files):
    root = tmp_path / 'source'
    root.mkdir()
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    return SourceVersion('2507.01710', 1, root, tuple(files), 'ok', 'ok')


def test_dotted_basename_resolves_with_tex_suffix(tmp_path):
    result = gather_source(source(tmp_path, {
        'main.tex': r'\documentclass{article}\begin{document}\input{sections/01.introduction}\end{document}',
        'sections/01.introduction.tex': 'Recovered section.'}))
    assert result.status == 'ok', result.warnings
    assert 'Recovered section.' in result.latex


def test_exact_path_wins_over_appended_suffix(tmp_path):
    context = Context(source(tmp_path, {'main.tex': 'X', 'main.tex.tex': 'Y'}))
    context.root = 'main.tex'
    assert context.resolve('main.tex', 'main.tex', '.tex') == 'main.tex'


def test_dotted_appended_suffix_remains_ambiguous_across_directories(tmp_path):
    context = Context(source(tmp_path, {
        'nested/main.tex': 'X', 'nested/01.intro.tex': 'A', '01.intro.tex': 'B'}))
    context.root = 'nested/main.tex'
    assert context.resolve('nested/main.tex', '01.intro', '.tex') is None
    assert any('AMBIGUOUS_PATH' in w for w in context.warnings)


def test_declared_boolean_finds_hidden_root_and_only_active_body(tmp_path):
    result = gather_source(source(tmp_path, {
        'measure.tex': r'\newif\ifblind\blindfalse'
                       r'\ifblind\documentclass{IEEEtran}\else\documentclass{acmart}\fi'
                       r'\begin{document}\ifblind\input{missing}\else Visible.\fi\end{document}'}))
    assert result.status == 'ok', result.warnings
    assert result.root_file == 'measure.tex'
    assert r'\documentclass{acmart}' in result.latex
    assert 'IEEEtran' not in result.latex and 'missing' not in result.latex


def test_nested_boolean_assignments_share_execution_order(tmp_path):
    result = gather_source(source(tmp_path, {
        'paper.tex': r'\newif\ifTR\TRtrue\newif\ifBlind\Blindtrue'
                     r'\ifTR\ifBlind\documentclass{article}\else\documentclass{book}\fi'
                     r'\else\documentclass{report}\fi'
                     r'\begin{document}\iftrue\Blindfalse\fi'
                     r'\ifBlind BAD\else GOOD\fi\end{document}'}))
    assert result.status == 'ok', result.warnings
    assert 'GOOD' in result.latex and 'BAD' not in result.latex
    assert r'\documentclass{article}' in result.latex


def test_boolean_set_in_reached_input_is_used(tmp_path):
    result = gather_source(source(tmp_path, {
        'main.tex': r'\documentclass{article}\newif\ifdraft\input{options}'
                    r'\begin{document}\ifdraft Draft\else Final\fi\end{document}',
        'options.tex': r'\drafttrue'}))
    assert result.status == 'ok', result.warnings
    assert 'Draft' in result.latex and 'Final' not in result.latex


def test_local_assignment_does_not_leak_past_group(tmp_path):
    result = gather_source(source(tmp_path, {
        'main.tex': r'\documentclass{article}\newif\ifdraft\draftfalse'
                    r'\begin{document}{\drafttrue\ifdraft Local\fi}'
                    r'\ifdraft BAD\else Final\fi\end{document}'}))
    assert result.status == 'ok', result.warnings
    assert 'Local' in result.latex and 'Final' in result.latex and 'BAD' not in result.latex


def test_unknown_condition_does_not_choose_branch_from_stale_boolean(tmp_path):
    result = gather_source(source(tmp_path, {
        'main.tex': r'\documentclass{article}\newif\ifdraft\draftfalse'
                    r'\ifunknown\drafttrue\fi'
                    r'\begin{document}\ifdraft\input{missing}\else Final\fi\end{document}'}))
    assert result.status == 'partial'
    assert any('UNRESOLVED_CONDITIONAL' in w for w in result.warnings)
    assert not any('MISSING_DEPENDENCY' in w for w in result.warnings)


def test_package_ifdefempty_is_not_a_primitive_requiring_fi(tmp_path):
    result = gather_source(source(tmp_path, {
        'main.tex': r'\documentclass{article}\begin{document}'
                    r'\ifdefempty{\vldbavailabilityurl}{}{\input{unknown_branch}}'
                    r'After condition.\end{document}'}))
    assert result.status == 'partial'
    assert 'After condition.' in result.latex
    assert any('UNRESOLVED_CONDITIONAL' in w for w in result.warnings)
    assert not any('UNTERMINATED_CONDITIONAL' in w for w in result.warnings)
    assert not any('MISSING_DEPENDENCY' in w for w in result.warnings)


def test_unknown_root_conditional_stays_unresolved():
    assert root_count(r'\ifpdf\documentclass{article}\else\documentclass{book}\fi') == 0


def test_newif_default_is_false_without_explicit_setter(tmp_path):
    result = gather_source(source(tmp_path, {
        'main.tex': r'\newif\ifdraft\ifdraft\documentclass{book}'
                    r'\else\documentclass{article}\fi'
                    r'\begin{document}\ifdraft\input{missing}\else Final\fi\end{document}'}))
    assert result.status == 'ok', result.warnings
    assert 'Final' in result.latex and 'missing' not in result.latex


def test_global_boolean_assignment_survives_group(tmp_path):
    result = gather_source(source(tmp_path, {
        'main.tex': r'\documentclass{article}\newif\ifdraft'
                    r'\begin{document}{\global\drafttrue}'
                    r'\ifdraft Draft\else BAD\fi\end{document}'}))
    assert result.status == 'ok', result.warnings
    assert 'Draft' in result.latex and 'BAD' not in result.latex


def test_global_assignment_in_input_survives_callers_group(tmp_path):
    result = gather_source(source(tmp_path, {
        'main.tex': r'\documentclass{article}\newif\ifdraft'
                    r'\begin{document}{\input{options}}'
                    r'\ifdraft Draft\else BAD\fi\end{document}',
        'options.tex': r'\iftrue\global\drafttrue\fi'}))
    assert result.status == 'ok', result.warnings
    assert 'Draft' in result.latex and 'BAD' not in result.latex


def test_converted_bbl_is_bibliography_not_executed_body(tmp_path):
    bbl = (r'\ifx\natexlab\undefined\def\natexlab#1{#1}\fi'
           r'\begin{thebibliography}{1}\bibitem{x} Unique bibliography text.'
           r'\end{thebibliography}')
    result = gather_source(source(tmp_path, {
        'main.tex': r'\documentclass{article}\begin{document}Body \cite{x}.'
                    r'\input{main.bbl.tex}\end{document}',
        'main.bbl.tex': bbl}))
    assert result.status == 'ok', result.warnings
    assert result.bib_files == {'main.bbl.tex': bbl}
    assert result.included_files == ['main.tex']
    assert 'Unique bibliography text' not in result.latex
    assert r'\ifx' not in result.latex
