from m2.src.parsing.gathering import gather_source
from m2.tests.parsing.test_compilation_metadata import source


def test_subfile_wrapper_preserves_both_bodies_without_child_preambles(tmp_path):
    result = gather_source(source(tmp_path, {
        'wrapper.tex':(r'\documentclass{article}\usepackage{subfiles}\begin{document}'
                      r'Wrapper start.\subfile{article}\subfile{supplement}Wrapper end.\end{document}'),
        'article.tex':(r'\documentclass[nonexistent-main]{subfiles}\newcommand{\hidden}{IGNORED_PREAMBLE}'
                       r'\begin{document}Main article.\end{document}'),
        'supplement.tex':(r'\documentclass[nonexistent-main]{subfiles}\begin{document}'
                          r'Supplement body.\end{document}')
    }))
    assert result.root_file == 'wrapper.tex'
    assert 'Main article.' in result.latex and 'Supplement body.' in result.latex
    assert 'Wrapper end.' in result.latex
    assert 'IGNORED_PREAMBLE' not in result.latex
    assert result.latex.count(r'\documentclass') == 1
    assert result.latex.count(r'\end{document}') == 1
    assert not any('UNSUPPORTED_SUBFILE' in w for w in result.warnings)


def test_nested_subfile_retains_body_and_preamble_bibliography(tmp_path):
    result = gather_source(source(tmp_path, {
        'main.tex':r'\documentclass{article}\begin{document}\subfile{child}\end{document}',
        'child.tex':(r'\documentclass[main]{subfiles}\addbibresource{refs.bib}'
                      r'\begin{document}Child.\subfile{grandchild}\end{document}'),
        'grandchild.tex':r'\documentclass[child]{subfiles}\begin{document}Grandchild.\end{document}',
        'refs.bib':'@article{x,title={Reference}}'
    }))
    assert result.root_file == 'main.tex'
    assert 'Child.' in result.latex and 'Grandchild.' in result.latex
    assert 'refs.bib' in result.bib_files
    assert 'MISSING_DEPENDENCY' not in ' '.join(result.warnings)


def test_generic_document_in_subfile_command_stays_unsupported(tmp_path):
    result = gather_source(source(tmp_path, {
        'main.tex':r'\documentclass{article}\subfile{ordinary}',
        'ordinary.tex':r'\documentclass{article}\begin{document}Other.\end{document}'
    }))
    assert result.status == 'failed'
    assert any('AMBIGUOUS_ROOT' in w for w in result.warnings)


def test_malformed_subfile_envelope_is_not_guessed(tmp_path):
    result = gather_source(source(tmp_path, {
        'main.tex':r'\documentclass{article}\subfile{fragment}',
        'fragment.tex':'No document envelope'
    }))
    assert result.status == 'partial'
    assert any('UNSUPPORTED_SUBFILE' in w for w in result.warnings)


def test_subfile_include_cycle_remains_bounded(tmp_path):
    result = gather_source(source(tmp_path, {
        'main.tex':r'\documentclass{article}\begin{document}\subfile{child}\end{document}',
        'child.tex':r'\documentclass[main]{subfiles}\begin{document}\subfile{child}\end{document}'
    }))
    assert result.status == 'partial'
    assert any('INCLUDE_CYCLE' in w for w in result.warnings)
