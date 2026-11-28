from m2.src.parsing.biblatex_bbl import is_empty_bibliography
from m2.src.parsing.amsrefs_bbl import amsrefs_bbl_to_bibtex
from m2.src.parsing.bibliography import extract_bibliography_detailed,parse_bib,normalize_entry
from m2.src.parsing.contracts import GatheredDocument


def test_explicit_empty_bibliography_is_not_unsupported_syntax():
    raw='\\begin{thebibliography}{}\n% no records\n\\end{thebibliography}'
    assert is_empty_bibliography(raw)
    doc=GatheredDocument('2507.00001',1,'main.tex','',[],{'main.bbl.tex':raw},'ok')
    entries,warnings=extract_bibliography_detailed(doc)
    assert not entries and not warnings
    assert not is_empty_bibliography(r'\begin{thebibliography}{}unknown\end{thebibliography}')


def test_scalar_ams_review_metadata_is_preserved_and_not_misread_as_year():
    raw=r'\bib{x}{article}{title={Real title},date={2020},type={Preprint},review={\MR{20240000}},}'
    bib,warnings=amsrefs_bbl_to_bibtex(raw)
    assert not warnings
    entries,issues=parse_bib(bib)
    assert not issues
    f=normalize_entry(entries[0]).fields
    assert f['year']==2020 and f['review']==r'\MR{20240000}' and f['type']=='Preprint'


def test_nested_ams_review_still_has_diagnostics_instead_of_flattening():
    _,warnings=amsrefs_bbl_to_bibtex(r'\bib{x}{article}{title={Real title},review={key={value}},}')
    assert any('NESTED_FIELD' in w for w in warnings)
