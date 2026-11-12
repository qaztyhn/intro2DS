from src.parsing.bibliography import extract_bibliography, parse_bib
from src.parsing.contracts import GatheredDocument


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