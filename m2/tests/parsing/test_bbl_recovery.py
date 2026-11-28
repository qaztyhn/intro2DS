"""Regression checks for bounded BBL recovery and derived BibTeX files."""

import pytest

from m2.src.parsing.biblatex_bbl import biblatex_bbl_to_bibtex, parse_biblatex_bbl
from m2.src.parsing.bibliography import extract_bibliography_detailed, normalize_entry, parse_bib
from m2.src.parsing.contracts import GatheredDocument


def document(files):
    return GatheredDocument('2507.00001', 1, 'main.tex', r'\cite{x}', [], files, 'ok')


@pytest.mark.parametrize('separator',['','\n'])
@pytest.mark.parametrize('broken',[
    r'\field{title}{Broken',
    r'\name{author}{1}{}{family={Broken',
    r'\field{title}{Literal \entry{fake}{book}{} title}\field{note}{Broken',
])
def test_unclosed_group_does_not_hide_intact_following_biber_records(broken,separator):
    raw=(r'\entry{bad}{article}{}'+broken+separator+
         r'\entry{good}{article}{}\field{title}{Good Title}\field{year}{2024}\endentry'+
         r'\entry{second}{book}{}\field{title}{Second Title}\endentry')
    entries,warnings=parse_biblatex_bbl(raw)
    assert [e.original_key for e in entries]==['good','second']
    assert entries[0].fields=={'title':'Good Title','year':'2024'}
    assert len(warnings)==1 and 'BBL_TRUNCATED_ENTRY key=bad' in warnings[0]


def test_missing_endentry_does_not_promote_literal_entry_inside_a_closed_field():
    raw=r'\entry{bad}{article}{}\field{title}{Literal \entry{fake}{book}{} title}'
    entries,warnings=parse_biblatex_bbl(raw)
    assert not entries
    assert len(warnings)==1 and 'BBL_TRUNCATED_ENTRY key=bad' in warnings[0]


def test_intact_biber_field_and_verbatim_entry_literals_remain_metadata():
    raw=(r'\entry{real}{article}{}\field{title}{Literal \entry{fake}{book}{} title}'
         r'\verb{url}\verb https://example.org/\entry{fakeurl}{book}{}\endverb\endentry')
    entries,warnings=parse_biblatex_bbl(raw)
    assert [e.original_key for e in entries]==['real']
    assert not warnings


def test_missing_endentry_quarantines_record_and_recovers_next():
    entries, warnings = parse_biblatex_bbl(
        r'\entry{broken}{article}{}\field{title}{Broken}'
        r'\entry{good}{article}{}\field{title}{Good}\field{year}{2024}\endentry')
    assert [e.original_key for e in entries] == ['good']
    assert entries[0].fields == {'title': 'Good', 'year': '2024'}
    assert any('BBL_TRUNCATED_ENTRY key=broken' in w for w in warnings)


def test_comment_with_entry_example_does_not_hide_classic_bbl():
    entries, warnings = extract_bibliography_detailed(document({
        'main.bbl': '% Biber example: \\entry{key}{type}{}\n'
                    r'\begin{thebibliography}{1}\bibitem{x} John Smith.'
                    r'\newblock Actual Title.\newblock Nature, 2020.\end{thebibliography}'}))
    assert [e.original_key for e in entries] == ['x']
    assert entries[0].fields['title'] == 'Actual Title'
    assert warnings == []


def test_equivalent_bib_and_bbl_merge_complementary_fields_without_warning():
    entries, warnings = extract_bibliography_detailed(document({
        'refs.bib': '@article{x,title={Actual Title},author={John Smith},year={2020}}',
        'main.bbl': r'\entry{x}{article}{}\field{title}{Actual Title}'
                    r'\name{author}{1}{}{{{family={Smith},given={John}}}}'
                    r'\field{year}{2020}\field{doi}{10.1000/recovered}\endentry'}))
    assert len(entries) == 1
    assert entries[0].fields['doi'] == '10.1000/recovered'
    assert entries[0].fields['authors'] == ['John Smith']
    assert warnings == []


def test_conflicting_bbl_does_not_supply_metadata_to_other_reference():
    entries, warnings = extract_bibliography_detailed(document({
        'refs.bib': '@article{x,title={Original},year={2020}}',
        'main.bbl': r'\entry{x}{article}{}\field{title}{Different}'
                    r'\field{doi}{10.1000/wrong}\endentry'}))
    assert entries[0].fields['title'] == 'Original'
    assert not entries[0].fields['doi']
    assert any('duplicate key' in w for w in warnings)


def test_native_bib_precedes_alphabetically_earlier_derived_bbl_bib():
    entries, warnings = extract_bibliography_detailed(document({
        'references.bib': '@article{x,title={Original},year={2020}}',
        'main.bbl.bib': '@article{x,title={Different},doi={10.1000/wrong}}'}))
    assert entries[0].fields['title'] == 'Original'
    assert not entries[0].fields['doi']
    assert any('duplicate key' in w for w in warnings)


def test_initials_equivalent_only_with_exact_title_year_keep_native_full_names():
    entries, warnings = extract_bibliography_detailed(document({
        'references.bib': '@article{x,title={Actual Title},author={Izquierdo, Sergio and Civera, Javier},year={2024}}',
        'main.bbl.bib': '@article{x,title={Actual Title},author={Izquierdo, S. and Civera, J.},year={2024}}'}))
    assert warnings == []
    assert entries[0].fields['authors'] == ['Sergio Izquierdo', 'Javier Civera']


def test_multiple_given_name_initials_preserve_order():
    entries, warnings = extract_bibliography_detailed(document({
        'references.bib': '@article{x,title={Actual Title},author={Jatavallabhula, Krishna Murthy},year={2024}}',
        'main.bbl.bib': '@article{x,title={Actual Title},author={Jatavallabhula, K. M.},year={2024}}'}))
    assert warnings == []
    assert entries[0].fields['authors'] == ['Krishna Murthy Jatavallabhula']


def test_explicitly_truncated_fallback_author_list_can_match_prefix():
    entries, warnings = extract_bibliography_detailed(document({
        'references.bib': '@article{x,title={Actual Title},author={Smith, John and Doe, Jane},year={2024}}',
        'main.bbl.bib': '@article{x,title={Actual Title},author={Smith, J. and others},year={2024}}'}))
    assert warnings == []
    assert entries[0].fields['authors'] == ['John Smith', 'Jane Doe']
    assert not entries[0].fields.get('authors_truncated')


def test_initials_without_strong_identity_do_not_suppress_duplicate_warning():
    entries, warnings = extract_bibliography_detailed(document({
        'references.bib': '@article{x,title={Actual Title},author={Smith, John}}',
        'main.bbl.bib': '@article{x,title={Actual Title},author={Smith, J.}}'}))
    assert any('duplicate key' in w for w in warnings)
    assert entries[0].fields['authors'] == ['John Smith']


def test_conflicting_surnames_title_or_missing_middle_names_remain_warnings():
    native = '@article{x,title={Actual Title},author={Smith, John Paul},year={2024}}'
    for other in [
        '@article{x,title={Actual Title},author={Jones, J. P.},year={2024}}',
        '@article{x,title={Other Title},author={Smith, J. P.},year={2024}}',
        '@article{x,title={Actual Title},author={Smith, J.},year={2024}}',
    ]:
        entries, warnings = extract_bibliography_detailed(document({'references.bib': native, 'main.bbl.bib': other}))
        assert any('duplicate key' in w for w in warnings)
        assert entries[0].fields['authors'] == ['John Paul Smith']


def test_classic_bbl_duplicate_does_not_change_bibtex_parse_mode():
    entries, warnings = extract_bibliography_detailed(document({
        'refs.bib': '@article{x,title={Actual Title},author={Smith, John and Doe, Jane},year={2020}}',
        'main.bbl': r'\begin{thebibliography}{1}\bibitem{x} John Smith and Jane Doe.'
                    r'\newblock Actual Title.\newblock \emph{Nature}, 2020.\end{thebibliography}'}))
    assert warnings == []
    assert entries[0].fields['authors'] == ['John Smith', 'Jane Doe']
    assert 'raw' not in entries[0].fields
    assert entries[0].fields['journal'] == 'Nature'


def test_biber_conversion_roundtrips_supported_values():
    original = (r'\entry{x}{article}{}\field{title}{A {Nested} Title}'
                r'\name{author}{1}{}{{{family={Smith},given={John}}}}'
                r'\field{year}{2024}\verb{url}\verb https://example.org/a%20b \endverb'
                r'\endentry')
    text, warnings = biblatex_bbl_to_bibtex(original)
    assert warnings == []
    entries, parse_warnings = parse_bib(text)
    assert parse_warnings == [] and len(entries) == 1
    fields = normalize_entry(entries[0]).fields
    assert fields['title'] == 'A Nested Title'
    assert fields['authors'] == ['John Smith']
    assert fields['year'] == 2024
    assert fields['url'] == 'https://example.org/a%20b'


def test_conversion_rejects_unbalanced_verbatim_value():
    text, warnings = biblatex_bbl_to_bibtex(
        r'\entry{x}{article}{}\field{title}{Good}'
        r'\verb{url}\verb https://example.org/}bad \endverb\endentry')
    entries, parse_warnings = parse_bib(text)
    assert parse_warnings == []
    assert entries[0].fields == {'title': 'Good'}
    assert any('BBL_UNSERIALIZABLE_FIELD' in w for w in warnings)


def test_repeated_bbl_scalars_keep_first_and_distinguish_equal_values():
    from m2.src.parsing.amsrefs_bbl import parse_amsrefs_bbl
    from m2.src.parsing.diagnostics import diagnostic_codes

    biber, warnings = parse_biblatex_bbl(
        r'\entry{x}{article}{}\field{year}{2020}\field{year}{2020}\field{year}{2021}\endentry')
    assert biber[0].fields['year'] == '2020'
    assert diagnostic_codes(warnings) == (['BIB_DUPLICATE_FIELD_DIFFERENT'], ['BIB_DUPLICATE_FIELD_IDENTICAL'])

    ams, warnings = parse_amsrefs_bbl(r'\bib{x}{article}{year={2020},year={2020},year={2021}}')
    assert ams[0].fields['year'] == '2020'
    assert diagnostic_codes(warnings) == (['AMSREFS_DUPLICATE_FIELD'], ['BIB_DUPLICATE_FIELD_IDENTICAL'])
