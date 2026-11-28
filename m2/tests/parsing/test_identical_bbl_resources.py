from pathlib import Path
from m2.src.parsing.contracts import SourceVersion
from m2.src.parsing.gathering import gather_source


def test_identical_bbl_paths_can_resolve_without_choosing_different_data(tmp_path):
    files={'nested/main.tex':r'\documentclass{article}\input{main.bbl.tex}',
           'nested/main.bbl.tex':r'\bibitem{x} A.', 'main.bbl.tex':r'\bibitem{x} A.'}
    for name,text in files.items():
        p=tmp_path/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
    result=gather_source(SourceVersion('2507.00001',1,tmp_path,tuple(files),'ok','ok'))
    assert result.status=='ok'
    assert len(result.bib_files)==1 and not result.warnings


def test_different_bbl_paths_remain_explicitly_unresolved(tmp_path):
    files={'nested/main.tex':r'\documentclass{article}\input{main.bbl.tex}',
           'nested/main.bbl.tex':r'\bibitem{x} A.', 'main.bbl.tex':r'\bibitem{y} B.'}
    for name,text in files.items():
        p=tmp_path/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
    result=gather_source(SourceVersion('2507.00001',1,tmp_path,tuple(files),'ok','ok'))
    assert result.status=='partial'
    assert any('AMBIGUOUS_PATH' in w for w in result.warnings)
