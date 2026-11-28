"""AMSRefs records found in the local M1 archive recovery sample."""

from m2.src.parsing.amsrefs_bbl import (
    amsrefs_bbl_to_bibtex, is_amsrefs_bbl, parse_amsrefs_bbl)
from m2.src.parsing.bibliography import extract_bibliography_detailed, normalize_entry, parse_bib
from m2.src.parsing.contracts import GatheredDocument


def test_repeated_authors_and_date_roundtrip_to_bibtex():
    raw = (r'\begin{biblist}\bib{x}{article}{author={Smith, John},'
           r'author={Doe, Jane},title={A {Nested} Title},date={2024-05-03},'
           r'journal={Nature},doi={10.1000/example},pages={1\ndash 9},}\end{biblist}')
    converted, warnings = amsrefs_bbl_to_bibtex(raw)
    entries, parse_warnings = parse_bib(converted)
    assert warnings == parse_warnings == []
    assert len(entries) == 1
    fields = normalize_entry(entries[0]).fields
    assert fields['authors'] == ['John Smith', 'Jane Doe']
    assert fields['title'] == 'A Nested Title'
    assert fields['year'] == 2024
    assert fields['journal'] == 'Nature'
    assert fields['doi'] == '10.1000/example'
    assert entries[0].fields['pages'] == r'1\ndash 9'


def test_bibliography_dispatch_normalizes_amsrefs_directly():
    doc = GatheredDocument('2507.01672', 1, 'article.tex', r'\cite{x}', [],
                           {'article.bbl': r'\bib{x}{book}{author={Smith, John},title={The Book},date={2002},}'}, 'ok')
    entries, warnings = extract_bibliography_detailed(doc)
    assert warnings == []
    assert entries[0].entry_type == 'book'
    assert entries[0].fields['year'] == 2002
    assert entries[0].fields['authors'] == ['John Smith']


def test_comment_example_is_not_amsrefs():
    assert not is_amsrefs_bbl('% example: \\bib{x}{article}{title={X}}\n')
    assert is_amsrefs_bbl('% header\n' + r'\bib{x}{article}{title={X}}')


def test_nested_metadata_is_diagnosed_and_not_flattened():
    entries, warnings = parse_amsrefs_bbl(
        r'\bib{x}{article}{title={Known},book={title={Book},publisher={Other}},'
        r'author={family={Smith},given={John}},date={2024}}')
    assert entries[0].fields == {'title': 'Known', 'date': '2024'}
    assert any('AMSREFS_UNSUPPORTED_FIELD' in w and 'book' in w for w in warnings)
    assert any('AMSREFS_NESTED_FIELD' in w and 'author' in w for w in warnings)


def test_truncated_entry_does_not_consume_later_good_record():
    entries, warnings = parse_amsrefs_bbl(
        r'\bib{broken}{article}{title={Missing close}'
        r'\bib{good}{article}{title={Kept},date={2024}}')
    assert [e.original_key for e in entries] == ['good']
    assert any('AMSREFS_MALFORMED_ENTRY' in w for w in warnings)


def test_duplicate_scalar_conflict_keeps_first_and_reports():
    entries, warnings = parse_amsrefs_bbl(
        r'\bib{x}{article}{title={First},title={Second},date={2024}}')
    assert entries[0].fields['title'] == 'First'
    assert any('AMSREFS_DUPLICATE_FIELD' in w for w in warnings)


def test_unsafe_key_is_not_serialized_as_malformed_bibtex():
    converted, warnings = amsrefs_bbl_to_bibtex(r'\bib{bad,key}{article}{title={Title}}')
    assert converted == ''
    assert any('UNSERIALIZABLE_ENTRY' in w for w in warnings)
