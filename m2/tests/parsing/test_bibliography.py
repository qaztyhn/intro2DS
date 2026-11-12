from src.parsing.bibliography import (
    clean_text, extract_bibliography, normalize_entry, parse_bib, parse_bibitems,
)
from src.parsing.contracts import BibEntry, GatheredDocument

def test_basic_article():
    entries, warns = parse_bib("""
    @Article{smith2020,
      author = {Smith, John and Doe, Jane},
      title = {A {Nested} Title},
      year = 2020,
      journal = "Nature",
    }
    """)
    e = entries[0]
    assert (e.original_key, e.entry_type) == ("smith2020", "article")
    assert e.fields["author"] == "Smith, John and Doe, Jane"
    assert e.fields["title"] == "A {Nested} Title"
    assert e.fields["year"] == "2020"
    assert e.fields["journal"] == "Nature"
    assert warns == []


def test_string_macro_concat_and_month():
    entries, _ = parse_bib("""
    @string{nips = "Neural Information Processing Systems"}
    @inproceedings{k1, booktitle = "Proc. " # nips, month = mar, year = {2019}}
    """)
    assert [e.original_key for e in entries] == ["k1"]
    assert entries[0].fields["booktitle"] == "Proc. Neural Information Processing Systems"
    assert entries[0].fields["month"] == "March"


def test_comment_and_preamble_skipped():
    entries, _ = parse_bib(r"""
    @comment{ignored @article{x, title={no}} }
    @preamble{"\newcommand{\foo}{bar}"}
    @misc{real, title={Real}}
    """)
    assert [e.original_key for e in entries] == ["real"]


def test_parenthesis_delimiters():
    entries, _ = parse_bib("@book(b1, title = {T}, year = 2001)")
    assert entries[0].original_key == "b1"
    assert entries[0].fields["year"] == "2001"


def test_malformed_entry_recovers():
    entries, warns = parse_bib(
        "@article{bad, title = {unclosed ,\n@article{good, title = {Good}}"
    )
    assert [e.original_key for e in entries] == ["good"]
    assert len(warns) == 1


def test_duplicate_field_keeps_first():
    entries, warns = parse_bib("@misc{d, title={First}, title={Second}}")
    assert entries[0].fields["title"] == "First"
    assert len(warns) == 1


def test_extract_bibliography_sorted_by_path():
    doc = GatheredDocument(
        arxiv_id="2507.11662", version=1, root_file="main.tex", latex="",
        included_files=[], status="ok",
        bib_files={"b.bib": "@misc{a, title={A}}", "a.bib": "@misc{z, title={Z}}"},
    )
    assert [e.original_key for e in extract_bibliography(doc)] == ["z", "a"]

SAMPLE_BBL = r"""
\begin{thebibliography}{10}
\bibitem[Smith et~al.(2020)]{smith2020}
J.~Smith and A.~Doe.
\newblock Deep learning for {X}.
\newblock \emph{Nature}, 5(2):1--10, 2020.
\doi{10.1000/xyz123}

\bibitem{b}
Foo Bar, Some manual title, arXiv:2012.12345, 2021.
\end{thebibliography}
\section{Appendix} 1999
"""


def test_bibitem_newblock_and_label():
    entries, warns = parse_bibitems(SAMPLE_BBL)
    e = entries[0]
    assert e.original_key == "smith2020"
    assert e.fields["label"] == "Smith et~al.(2020)"
    assert e.fields["author"] == "J.~Smith and A.~Doe."
    assert e.fields["title"] == "Deep learning for {X}."
    assert e.fields["journal"].startswith("\\emph{Nature}")
    assert e.fields["doi"] == "10.1000/xyz123"
    assert e.fields["year"] == "2020"
    assert warns == []


def test_bibitem_manual_no_newblock():
    entries, _ = parse_bibitems(SAMPLE_BBL)
    e = entries[1]
    assert e.original_key == "b"
    assert e.fields["eprint"] == "2012.12345"
    assert e.fields["year"] == "2021"
    assert "title" not in e.fields
    assert "Appendix" not in e.fields["raw"]


def test_bibitem_comment_ignored():
    entries, _ = parse_bibitems("\\bibitem{a} Title A, 2020. % \\bibitem{ghost} x")
    assert [e.original_key for e in entries] == ["a"]


def test_bibitem_empty_key_warns():
    entries, warns = parse_bibitems("\\bibitem{} nothing")
    assert entries == [] and len(warns) == 1


def test_extract_merges_bib_and_bibitem_dedup_by_key():
    doc = GatheredDocument(
        arxiv_id="2507.11662", version=1, root_file="main.tex",
        latex="\\bibitem{k} From latex 2020.\n\\bibitem{k2} Other 2021.",
        included_files=[], status="ok",
        bib_files={"r.bib": "@misc{k, title={From bib}}"},
    )
    entries = extract_bibliography(doc)
    assert [e.original_key for e in entries] == ["k", "k2"]
    assert entries[0].fields["title"] == "From bib"

def test_clean_text_accents_and_markup():
    assert clean_text(r"""Schr{\"o}dinger \& {\'E}cole""") == "Schrödinger & École"
    assert clean_text(r"""Stra\ss e, {\c{C}}elik, \'{\i}""") == "Straße, Çelik, í"
    assert clean_text(r"\emph{Deep}~Learning -- {GAN}s") == "Deep Learning – GANs"


def test_clean_text_preserves_math():
    assert clean_text("A $x^{2}$ {Test}") == "A $x^{2}$ Test"


def test_normalize_bib_entry():
    e = BibEntry("k", "Conference", {
        "title": r'{Deep} Learning of {\"U}ber',
        "author": "Smith, John and van der Berg, Hans and {Google Research} and others",
        "year": "{2020}",
        "doi": "https://doi.org/10.1000/ABC.5",
        "journal": "Nature",
    })
    n = normalize_entry(e)
    assert n.entry_type == "inproceedings"
    f = n.fields
    assert f["title"] == "Deep Learning of Über"
    assert f["authors"] == ["John Smith", "Hans van der Berg", "Google Research"]
    assert f["year"] == 2020
    assert f["doi"] == "10.1000/abc.5"
    assert f["journal"] == "Nature"
    assert f["url"] is None
    assert f["author"].startswith("Smith, John")  # original kept


def test_normalize_missing_fields():
    f = normalize_entry(BibEntry("k", "misc", {})).fields
    assert f["title"] is None and f["authors"] == [] and f["year"] is None
    assert f["doi"] is None and f["journal"] is None and f["url"] is None


def test_normalize_url_and_doi_from_url():
    e = BibEntry("k", "misc", {"url": r"\url{https://doi.org/10.5555/A\_B}"})
    f = normalize_entry(e).fields
    assert f["url"] == "https://doi.org/10.5555/A_B"
    assert f["doi"] == "10.5555/a_b"


def test_normalize_bibitem_entries():
    entries, _ = parse_bibitems(SAMPLE_BBL)
    f = normalize_entry(entries[0]).fields
    assert f["authors"] == ["J. Smith", "A. Doe"]
    assert f["title"] == "Deep learning for X"
    assert f["journal"] == "Nature"
    assert f["year"] == 2020
    assert f["doi"] == "10.1000/xyz123"
    g = normalize_entry(entries[1]).fields
    assert g["authors"] == [] and g["title"] is None
    assert g["year"] == 2021 and g["eprint"] == "2012.12345"


def test_normalize_bibitem_author_lists():
    def authors(s):
        e = BibEntry("k", "misc", {"raw": "x", "author": s})
        return normalize_entry(e).fields["authors"]
    assert authors("A. Smith, B. Jones, and C. Lee.") == ["A. Smith", "B. Jones", "C. Lee"]
    assert authors("Smith, J., Doe, A.") == ["J. Smith", "A. Doe"]


def test_normalize_is_idempotent():
    e = BibEntry("k", "article", {"title": "{A} B", "author": "Doe, Jane", "year": "2001"})
    once = normalize_entry(e)
    assert normalize_entry(once).fields == once.fields