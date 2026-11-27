from m2.src.parsing.bibliography import (
    build_key_resolver, clean_text, extract_bibliography, normalize_entry, parse_bib, parse_bibitems, unify_bibliography,
)
from m2.src.parsing.contracts import BibEntry, GatheredDocument
from pathlib import Path


import inspect


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


def test_bibitem_empty_url_is_ignored():
    entries, warns = parse_bibitems(r"\bibitem{x} Author. \url{}")
    assert len(entries) == 1
    assert "url" not in entries[0].fields
    assert warns == []


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


def _e(key, title, author="Smith, John", year="2020", etype="article", **extra):
    fields = {"title": title, "author": author, "year": year, **extra}
    return BibEntry(key, etype, fields)


def test_unify_same_entry_same_key():
    r = unify_bibliography("x", {1: [_e("a", "Deep Learning")],
                                 2: [_e("a", "Deep Learning")]})
    assert len(r.entries) == 1
    assert r.key_mapping == {1: {"a": "a"}, 2: {"a": "a"}}
    assert r.warnings == []


def test_unify_renamed_key_by_doi():
    r = unify_bibliography("x", {
        1: [_e("k1", "Deep Learning", doi="10.1000/abc")],
        2: [_e("k1b", "Deep Learning", doi="10.1000/abc")],
    })
    assert len(r.entries) == 1
    assert r.entries[0].original_key == "k1"
    assert r.key_mapping[2]["k1b"] == "k1"
    assert r.entries[0].fields["source_keys"] == {1: ["k1"], 2: ["k1b"]}


def test_unify_same_key_different_paper():
    r = unify_bibliography("x", {
        1: [_e("foo", "Alpha Paper Title")],
        2: [_e("foo", "Beta Different Work", author="Jones, Ann", year="2021")],
    })
    assert [e.original_key for e in r.entries] == ["foo", "foo_2"]
    assert r.key_mapping[1]["foo"] == "foo"
    assert r.key_mapping[2]["foo"] == "foo_2"
    assert r.warnings


def test_unify_merges_additional_fields():
    r = unify_bibliography("x", {
        1: [_e("a", "Deep Learning")],
        2: [_e("a", "Deep Learning", doi="10.1000/abc", journal="Nature")],
    })
    f = r.entries[0].fields
    assert len(r.entries) == 1
    assert f["doi"] == "10.1000/abc" and f["journal"] == "Nature"
    assert r.warnings == []


def test_unify_different_doi_not_merged():
    r = unify_bibliography("x", {
        1: [_e("a", "Same Title", doi="10.1000/aaa")],
        2: [_e("b", "Same Title", doi="10.1000/bbb")],
    })
    assert [e.original_key for e in r.entries] == ["a", "b"]


def test_unify_different_year_not_merged():
    r = unify_bibliography("x", {
        1: [_e("a", "Same Title", year="2020")],
        2: [_e("b", "Same Title", year="2021")],
    })
    assert len(r.entries) == 2


def test_unify_conflicting_field_keeps_latest_and_warns():
    r = unify_bibliography("x", {
        1: [_e("a", "T Title", doi="10.1000/abc", journal="Nature")],
        2: [_e("a", "T Title", doi="10.1000/abc", journal="Science")],
    })
    assert len(r.entries) == 1
    assert r.entries[0].fields["journal"] == "Science"
    assert any("journal" in w for w in r.warnings)


def test_unify_bibitem_across_versions():
    v1, _ = parse_bibitems(SAMPLE_BBL)
    v2, _ = parse_bibitems(SAMPLE_BBL)
    r = unify_bibliography("x", {1: v1, 2: v2})
    assert len(r.entries) == 2
    assert r.key_mapping[2] == {"smith2020": "smith2020", "b": "b"}


def test_unify_duplicate_inside_version():
    r = unify_bibliography("x", {1: [
        _e("a", "Dup Title", doi="10.1000/abc"),
        _e("a_dup", "Dup Title", doi="10.1000/abc"),
    ]})
    assert len(r.entries) == 1
    assert r.key_mapping[1] == {"a": "a", "a_dup": "a"}


def test_unify_empty_version_and_missing_key():
    r = unify_bibliography("x", {1: [], 2: [_e("k", "Some Title Here")]})
    assert r.key_mapping == {1: {}, 2: {"k": "k"}}
    r2 = unify_bibliography("x", {1: [_e("", "Deep Learning")]})
    assert r2.entries[0].original_key == "smith2020deep"
    assert r2.key_mapping == {1: {}}
    assert r2.warnings


def test_unify_deterministic():
    data = {1: [_e("a", "One Title"), _e("b", "Two Title", year="2019")],
            2: [_e("b", "Two Title", year="2019"), _e("c", "One Title")]}
    r1, r2 = unify_bibliography("x", data), unify_bibliography("x", data)
    assert r1.entries == r2.entries and r1.key_mapping == r2.key_mapping


# ---------------------------------------------------------------------------
# Fixture-driven tests
# ---------------------------------------------------------------------------
FIXTURES = Path(__file__).parent / "fixtures"
PUB = "2501.00001"


def load_doc(directory: Path, version: int = 1) -> GatheredDocument:
    main = directory / "main.tex"
    latex = main.read_text(encoding="utf-8") if main.exists() else ""
    bib_files = {p.name: p.read_text(encoding="utf-8")
                 for p in sorted(directory.glob("*.bib"))}
    return GatheredDocument(PUB, version, "main.tex", latex, ["main.tex"],
                            bib_files, "ok")


def load_versions(name: str) -> dict[int, GatheredDocument]:
    base = FIXTURES / name
    return {int(p.name[1:]): load_doc(p, int(p.name[1:]))
            for p in sorted(base.glob("v[0-9]*"))}


def unify_fixture(name: str):
    docs = load_versions(name)
    return unify_bibliography(
        PUB, {v: extract_bibliography(d) for v, d in docs.items()})


def test_fixture_bib_file():
    entries = extract_bibliography(load_doc(FIXTURES / "bibliography_bib"))
    assert [e.original_key for e in entries] == [
        "smith2020deep", "lee2019attn", "web2021"]
    s, lee, web = (e.fields for e in entries)
    assert s["title"] == "Deep Learning for Graphs"
    assert s["authors"] == ["John Smith", "Anna Müller"]
    assert s["year"] == 2020
    assert s["doi"] == "10.1000/deep.1"
    assert s["journal"] == "Nature"
    assert entries[1].entry_type == "inproceedings"
    assert lee["title"] == "Attention is not enough"
    assert lee["booktitle"] == "Proc. of Advances in Neural Information Processing Systems"
    assert lee["month"] == "June" and lee["year"] == 2019
    assert web["url"] == "https://example.org/proj_page"
    assert web["authors"] == [] and web["year"] is None


def test_fixture_bibitem():
    entries = extract_bibliography(load_doc(FIXTURES / "bibliography_bibitem"))
    assert [e.original_key for e in entries] == ["smith2020", "manual"]
    assert entries[0].fields["authors"] == ["J. Smith", "A. Doe"]
    assert entries[0].fields["year"] == 2020
    assert entries[1].fields["eprint"] == "2012.12345"
    assert entries[1].fields["year"] == 2021


def test_fixture_citation_conflict():
    r = unify_fixture("citation_conflict")
    assert [e.original_key for e in r.entries] == ["foo", "renamed_old", "foo_2"]
    assert r.key_mapping == {
        1: {"foo": "foo", "renamed_old": "renamed_old"},
        2: {"foo": "foo_2", "renamed_new": "renamed_old"},
    }
    assert r.entries[1].fields["journal"] == "JMLR"
    assert any("foo" in w for w in r.warnings)


def test_fixture_multi_version():
    docs = load_versions("multi_version")
    assert sorted(docs) == [1, 2, 3]
    r = unify_fixture("multi_version")
    assert [e.original_key for e in r.entries] == ["a", "b", "c"]
    a = r.entries[0]
    assert a.entry_type == "article"
    assert a.fields["doi"] == "10.1000/gn.1" and a.fields["journal"] == "JMLR"
    assert a.fields["source_keys"] == {1: ["a"], 2: ["a"]}
    assert r.entries[1].entry_type == "book"
    assert r.key_mapping == {
        1: {"a": "a", "b": "b"},
        2: {"a": "a", "b": "b", "c": "c"},
        3: {},
    }
    assert r.warnings == []


# ---------------------------------------------------------------------------
# Contract / robustness tests
# ---------------------------------------------------------------------------
def test_public_signatures_match_contract():
    assert list(inspect.signature(extract_bibliography).parameters) == ["document"]
    assert list(inspect.signature(unify_bibliography).parameters) == [
        "arxiv_id", "entries_by_version"]


def test_unify_empty_inputs():
    r = unify_bibliography("x", {})
    assert r.entries == [] and r.key_mapping == {} and r.warnings == []


def test_unify_ignores_dict_order():
    a = {1: [_e("a", "One Title")], 2: [_e("a", "One Title", doi="10.1000/x")]}
    b = {2: a[2], 1: a[1]}
    assert unify_bibliography("x", a) == unify_bibliography("x", b)


def test_parse_bib_never_raises_on_garbage():
    for text in ["", "@", "@article{", "@article{k, title = {", "@string{x}",
                 "@article{k, =x}", "}}}}", '@article{k, title = "unterminated}',
                 "@comment{unclosed"]:
        entries, warns = parse_bib(text)
        assert isinstance(entries, list) and isinstance(warns, list)


def test_parse_bibitems_never_raises_on_garbage():
    for text in ["", "\\bibitem", "\\bibitem{", "\\bibitem[{x}", "\\bibitem{a}"]:
        entries, warns = parse_bibitems(text)
        assert isinstance(entries, list) and isinstance(warns, list)


def test_normalize_weird_values_never_raises():
    e = BibEntry("k", "misc", {"title": None, "author": "{", "year": "abc",
                               "doi": "nope", "url": "{{"})
    f = normalize_entry(e).fields
    assert f["title"] is None and f["authors"] == []
    assert f["year"] is None and f["doi"] is None and f["url"] is None


def _doc_bib(text):
    return GatheredDocument("x", 1, "main.tex", "", ["main.tex"], {"r.bib": text}, "ok")


def test_string_defined_after_use():
    entries, warns = parse_bib('@article{a, journal = jrn, title = {T}}\n@string{jrn = "Nature"}')
    assert entries[0].fields["journal"] == "Nature" and warns == []


def test_commented_entry_skipped_but_percent_in_value_kept():
    entries, _ = parse_bib("% @article{old, title={Old}}\n@misc{u, url = {http://a/b%20c}}\n")
    assert [e.original_key for e in entries] == ["u"]
    assert entries[0].fields["url"] == "http://a/b%20c"


CROSS = """
@proceedings{proc, title = {Proc. of X}, year = 2019, publisher = {ACM}}
@inproceedings{paper1, author = {Lee, Kim}, title = {Own Title}, crossref = {PROC}}
@inproceedings{orphan, title = {Orphan}, crossref = {nope}}
@misc{c1, title = {C1}, crossref = {c2}}
@misc{c2, title = {C2}, crossref = {c1}}
"""


def test_crossref_inheritance_missing_parent_and_cycle():
    entries = {e.original_key: e for e in extract_bibliography(_doc_bib(CROSS))}
    p = entries["paper1"].fields
    assert p["title"] == "Own Title"            # child's own title is kept
    assert p["booktitle"] == "Proc. of X"       # parent's title -> booktitle
    assert p["year"] == 2019 and p["publisher"] == "ACM"
    assert "booktitle" not in entries["orphan"].fields
    assert entries["c1"].fields["title"] == "C1"  # cycle: terminates, own title kept


def test_extract_case_insensitive_duplicates_and_keyless_entries():
    doc = _doc_bib("@misc{Smith2020, title={A}}\n@misc{smith2020, title={B}}\n"
                   "@misc{, title={K1}}\n@misc{, title={K2}}")
    entries = extract_bibliography(doc)
    assert [e.original_key for e in entries] == ["Smith2020", "", ""]
    assert entries[0].fields["title"] == "A"


def test_build_key_resolver():
    resolve = build_key_resolver({"smith2020": "canon", "A": "ca", "a": "cb"})
    assert resolve("smith2020") == "canon"
    assert resolve(" Smith2020 ") == "canon"
    assert resolve("A") == "ca" and resolve("a") == "cb"   # exact match wins
    assert resolve("unknown") is None
    assert build_key_resolver({"Ab": "x", "aB": "y"})("AB") is None  # ambiguous


def test_bibitem_nested_label_braces():
    entries, warns = parse_bibitems(
        "\\bibitem[{{\\em Smith} (2020)}]{k1} Smith, A title, 2020.\n"
        "\\bibitem[{[1]}]{k2} Other, 2019.\n"
        "\\bibitem{k3} Third, 2018.")
    assert [e.original_key for e in entries] == ["k1", "k2", "k3"]
    assert entries[0].fields["label"] == "{{\\em Smith} (2020)}"
    assert "k2" not in entries[0].fields["raw"]
    assert warns == []


def test_bibitem_percent_in_url_kept_real_comment_removed():
    entries, _ = parse_bibitems(
        "\\bibitem{u} See \\url{http://example.com/file%20name}. % real comment\n"
        "\\bibitem{v} Next, 2020.")
    f = entries[0].fields
    assert f["url"] == "http://example.com/file%20name"
    assert "real comment" not in f["raw"] and "%20" in f["raw"]


def test_bibitem_year_ignores_page_ranges():
    def year(body):
        return parse_bibitems("\\bibitem{k} " + body)[0][0].fields["year"]
    assert year("Smith. Title. Journal, 2020, pp. 1980--1990.") == "2020"
    assert year("Smith. Title. Journal, pages 1980-1990, 2021.") == "2021"
    assert year("Smith. Title. Journal 5(2):1980--1990, 2022.") == "2022"


def test_bibitem_conference_goes_to_booktitle_not_journal():
    entries, _ = parse_bibitems(
        "\\bibitem{c} J. Smith.\n\\newblock A Title.\n"
        "\\newblock In \\emph{Proc. ICML}, 2020.\n"
        "\\bibitem{p} J. Doe.\n\\newblock Another.\n"
        "\\newblock \\emph{Proceedings of the National Academy of Sciences}, 5:1--10, 2020.")
    c = normalize_entry(entries[0]).fields
    assert c["booktitle"] == "Proc. ICML" and c["journal"] is None
    p = normalize_entry(entries[1]).fields
    assert p["journal"] == "Proceedings of the National Academy of Sciences"
    assert "booktitle" not in p


def test_vancouver_authors():
    def authors(s):
        e = BibEntry("k", "misc", {"raw": "x", "author": s})
        return normalize_entry(e).fields["authors"]
    assert authors("Smith J, Doe AB, Lee K.") == ["J. Smith", "A. B. Doe", "K. Lee"]
    assert authors("Google AI") == ["Google AI"]


def test_et_al_removed_and_flagged():
    def norm(author, bibitem=False):
        fields = {"author": author}
        if bibitem:
            fields["raw"] = "x"
        return normalize_entry(BibEntry("k", "misc", fields)).fields
    f = norm("Smith, J., et al.", bibitem=True)
    assert f["authors"] == ["J. Smith"] and f["authors_truncated"] is True
    assert norm("Smith et al.", bibitem=True)["authors"] == ["Smith"]
    g = norm("Smith, John and others")
    assert g["authors"] == ["John Smith"] and g["authors_truncated"] is True
    assert "authors_truncated" not in norm("Smith, John")


def test_unify_bibitem_raw_ignores_latex_markup():
    v1, _ = parse_bibitems("\\bibitem{a} John Smith. Deep Learning, 2020.")
    v2, _ = parse_bibitems("\\bibitem{b} John Smith.\n\\newblock Deep \\emph{Learning}, 2020.")
    r = unify_bibliography("x", {1: v1, 2: v2})
    assert len(r.entries) == 1
    assert r.key_mapping == {1: {"a": "a"}, 2: {"b": "a"}}


def test_unify_missing_year_needs_strong_evidence():
    short = unify_bibliography("x", {1: [_e("a", "Introduction")],
                                     2: [_e("b", "Introduction", year="")]})
    assert len(short.entries) == 2          # generic short title: not merged
    long_ = unify_bibliography("x", {1: [_e("a", "Attention Is All You Need")],
                                     2: [_e("b", "Attention Is All You Need", year="")]})
    assert len(long_.entries) == 1          # contract example: year dropped later
    other = unify_bibliography("x", {
        1: [_e("a", "Attention Is All You Need")],
        2: [_e("b", "Attention Is All You Need", author="Jones, Ann", year="")]})
    assert len(other.entries) == 2


def test_possible_duplicates_reported_not_merged():
    r = unify_bibliography("x", {1: [_e("a", "Deep Learning"),
                                     _e("b", "Deep Learning A Survey")]})
    assert len(r.entries) == 2
    assert any("possible duplicate" in w and "'a'" in w and "'b'" in w
               for w in r.warnings)
    r2 = unify_bibliography("x", {1: [_e("a", "Deep Learning"),
                                      _e("b", "Deep Learning A Survey", year="2021")]})
    assert not any("possible duplicate" in w for w in r2.warnings)


def test_journal_macros_expanded():
    def j(v):
        return normalize_entry(BibEntry("k", "article", {"journal": v})).fields["journal"]
    assert j(r"\apj") == "The Astrophysical Journal"
    assert j(r"{\mnras}") == "Monthly Notices of the Royal Astronomical Society"
    assert j(r"\nat") == "Nature"
    assert j(r"\unknownmacro") == r"\unknownmacro"


def test_year_raw_kept_when_unparseable():
    def fields(**kw):
        return normalize_entry(BibEntry("k", "misc", kw)).fields
    f = fields(year="to appear")
    assert f["year"] is None and f["year_raw"] == "to appear"
    assert fields(year="n.d.")["year_raw"] == "n.d."
    assert "year_raw" not in fields(year="2020")
    assert "year_raw" not in fields(title="x")


def test_unify_year_raw_dropped_when_year_known():
    r = unify_bibliography("x", {
        1: [_e("a", "Some Long Title Here")],
        2: [_e("a", "Some Long Title Here", year="to appear")]})
    f = r.entries[0].fields
    assert len(r.entries) == 1 and f["year"] == 2020 and "year_raw" not in f


def test_doi_from_note_requires_prefix_and_is_weak():
    def f(**kw):
        return normalize_entry(BibEntry("k", "misc", kw)).fields
    a = f(note="doi:10.1000/ABC.1")
    assert a["doi"] == "10.1000/abc.1" and a["doi_source"] == "note"
    assert f(note="Dataset at 10.1000/xyz")["doi"] is None       # no prefix
    own = f(doi="10.1000/own", note="doi:10.1000/other")
    assert own["doi"] == "10.1000/own" and "doi_source" not in own


def test_note_doi_is_not_a_merge_key_and_never_beats_own_doi():
    r = unify_bibliography("x", {1: [
        _e("a", "First Distinct Paper Title", note="Data: doi:10.1000/ds"),
        _e("b", "Second Unrelated Study Name", author="Jones, Ann", year="2021",
           note="Data: doi:10.1000/ds"),
    ]})
    assert len(r.entries) == 2
    r2 = unify_bibliography("x", {
        1: [_e("a", "Some Long Title Here", doi="10.1000/own")],
        2: [_e("a", "Some Long Title Here", note="doi:10.1000/other")]})
    f = r2.entries[0].fields
    assert len(r2.entries) == 1 and f["doi"] == "10.1000/own"
    assert "doi_source" not in f


def test_journal_macro_recorded_and_plain_abbreviation_untouched():
    def norm(v):
        return normalize_entry(BibEntry("k", "article", {"journal": v})).fields
    a = norm(r"\apj")
    assert a["journal"] == "The Astrophysical Journal" and a["journal_macro"] == "apj"
    u = norm(r"\unknownmacro")
    assert u["journal"] == r"\unknownmacro" and u["journal_macro"] == "unknownmacro"
    assert norm("ApJ")["journal"] == "ApJ" and "journal_macro" not in norm("ApJ")
    assert norm(r"\apjlett")["journal"] == "The Astrophysical Journal Letters"
    assert norm(r"\psj")["journal"] == "The Planetary Science Journal"


def test_bibitem_quoted_title_ieee_journal_and_conference():
    entries, _ = parse_bibitems(
        "\\bibitem{j} J. Smith and A. Doe, ``Deep learning for graphs,'' "
        "\\emph{Nature}, vol. 5, no. 2, pp. 1--10, 2020.\n"
        "\\bibitem{c} K. Lee, ``Attention revisited,'' in \\emph{Proc. ICML}, 2019.")
    j = normalize_entry(entries[0]).fields
    assert j["parse_mode"] == "quoted"
    assert j["title"] == "Deep learning for graphs"
    assert j["authors"] == ["J. Smith", "A. Doe"]
    assert j["journal"] == "Nature" and j["year"] == 2020
    c = normalize_entry(entries[1]).fields
    assert c["title"] == "Attention revisited"
    assert c["booktitle"] == "Proc. ICML" and c["journal"] is None
    assert c["year"] == 2019


def test_bibitem_straight_quotes_ignore_latex_accent_quote():
    entries, _ = parse_bibitems(
        '\\bibitem{s} M{\\"u}ller, A. "A study of X," Journal of Y, 2018.')
    f = normalize_entry(entries[0]).fields
    assert f["parse_mode"] == "quoted"
    assert f["title"] == "A study of X"
    assert f["authors"] == ["A. Müller"]
    assert f["year"] == 2018 and f["journal"] is None


def test_bibitem_without_quotes_stays_raw_only():
    for body in ("Foo Bar, A manual entry, 2021.",
                 "Hinton's book, 2020.",
                 "``Only a title,'' 2020."):          # nothing before the quote
        e = parse_bibitems("\\bibitem{n} " + body)[0][0]
        assert e.fields["parse_mode"] == "raw_only"
        assert "title" not in e.fields


def test_bibitem_parse_mode_newblock():
    e = parse_bibitems("\\bibitem{a} A. B.\n\\newblock A Title Here.\n\\newblock Journal, 2020.")[0][0]
    assert e.fields["parse_mode"] == "newblock"


def test_bbl_conversion_yields_normalized_bibtex():
    bbl = (r"\begin{thebibliography}{1}" "\n"
           r"\bibitem{x} J.~Smith and A.~Doe." "\n"
           r"\newblock A Great Paper." "\n"
           r"\newblock \emph{Nature}, 2020." "\n"
           r"\end{thebibliography}")
    doc = GatheredDocument("2507.11662", 1, "main.tex",
                           r"\cite{x}", [], {"main.bbl": bbl}, "ok")
    entries = extract_bibliography(doc)
    assert len(entries) == 1
    assert entries[0].original_key == "x"
    assert entries[0].fields["title"] == "A Great Paper"
    assert entries[0].fields["year"] == 2020


def test_bbl_with_no_bibitem_is_reported():
    from m2.src.parsing.bibliography import extract_bibliography_detailed
    doc = GatheredDocument("2507.11662", 1, "main.tex", "", [],
                           {"main.bbl": r"\refsection{0}\entry{x}{article}{}"}, "ok")
    entries, warnings = extract_bibliography_detailed(doc)
    assert entries == []
    assert any("BBL_TRUNCATED_ENTRY" in message for message in warnings)


def test_biber_bbl_structured_fields_and_names_normalize():
    from m2.src.parsing.biblatex_bbl import parse_biblatex_bbl

    bbl = (r"\refsection{0}" "\n"
           r"\datalist[entry]{nyt/global//global/global}" "\n"
           r"\entry{smith2020}{article}{}" "\n"
           r"\name{author}{2}{}{{{hash=abc}{family={Smith},given={John}}}"
           r"{{hash=def}{family={Doe},given={Jane}}}}" "\n"
           r"\field{title}{A {Novel} Study}" "\n"
           r"\field{year}{2020}" "\n"
           r"\field{journaltitle}{Nature}" "\n"
           r"\verb{doi}\verb 10.1000/abc \endverb" "\n"
           r"\verb{url}\verb https://example.org/abc%20def \endverb" "\n"
           r"\endentry" "\n"
           r"\enddatalist" "\n"
           r"\endrefsection")
    entries, warnings = parse_biblatex_bbl(bbl)
    assert warnings == [], warnings
    assert len(entries) == 1
    assert entries[0].original_key == "smith2020"
    assert entries[0].fields["author"] == "Smith, John and Doe, Jane"
    assert entries[0].fields["url"] == "https://example.org/abc%20def"

    document = GatheredDocument(
        "2507.11662", 1, "main.tex", r"\cite{smith2020}", [],
        {"main.bbl": bbl}, "ok")
    normalized = extract_bibliography(document)
    assert len(normalized) == 1
    assert normalized[0].fields["title"] == "A Novel Study"
    assert normalized[0].fields["authors"] == ["John Smith", "Jane Doe"]
    assert normalized[0].fields["year"] == 2020
    assert normalized[0].fields["journal"] == "Nature"
    assert normalized[0].fields["doi"] == "10.1000/abc"
    assert normalized[0].fields["url"] == "https://example.org/abc%20def"


def test_biber_bbl_does_not_override_existing_bib_key():
    doc = GatheredDocument(
        "2507.11662", 1, "main.tex", r"\cite{x}", [],
        {"refs.bib": "@article{x,title={Original},year={2020}}",
         "main.bbl": (r"\entry{x}{article}{}" 
                      r"\field{title}{Different}"
                      r"\field{year}{2020}"
                      r"\endentry")}, "ok")
    from m2.src.parsing.bibliography import extract_bibliography_detailed
    result, warnings = extract_bibliography_detailed(doc)
    assert len(result) == 1
    assert result[0].fields["title"] == "Original"
    assert any("duplicate key" in warning for warning in warnings)


def test_biber_bbl_partial_recovery_reports_truncated_entries():
    from m2.src.parsing.biblatex_bbl import parse_biblatex_bbl
    entries, warnings = parse_biblatex_bbl(
        r"\entry{ok}{article}{}\field{title}{Valid}\endentry"
        r"\entry{broken}{article}{}\field{title}{Incomplete}")
    assert [entry.original_key for entry in entries] == ["ok"]
    assert any("BBL_TRUNCATED_ENTRY" in w for w in warnings)
