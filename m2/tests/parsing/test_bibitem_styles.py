"""Classic BBL author formatting regressions from the recovered corpus."""
import pytest

from m2.src.parsing.bibliography import normalize_entry, parse_bibitems


def item(key, author):
    return '\\bibitem{' + key + '}' + author + r", ``A reference title,'' \emph{Nature}, 2024."


@pytest.mark.parametrize('marker', ['------', r'\bysame', r'\BIBdash', r'\BIBdash{}'])
def test_repeated_author_chain_uses_preceding_explicit_author(marker):
    text = r'\begin{thebibliography}{9}' + item('a', 'J.~Smith and A.~Doe') + item('b', marker) + item('c', marker) + r'\end{thebibliography}'
    entries, warnings = parse_bibitems(text)
    assert warnings == []
    assert [normalize_entry(e).fields['authors'] for e in entries] == [['J. Smith', 'A. Doe']] * 3
    assert entries[1].fields['author_repeated_from'] == 'a'
    assert entries[2].fields['author_repeated_from'] == 'a'
    assert marker in entries[1].fields['raw']


def test_repeated_author_does_not_cross_bibliography_list_boundary():
    text = (r'\begin{thebibliography}{1}' + item('a', 'J.~Smith') + r'\end{thebibliography}'
            r'\begin{thebibliography}{1}' + item('b', '------') + r'\end{thebibliography}')
    entries, warnings = parse_bibitems(text)
    assert normalize_entry(entries[1]).fields['authors'] == []
    assert 'author' not in entries[1].fields
    assert any('BIBITEM_REPEATED_AUTHOR_UNRESOLVED key=b' in w for w in warnings)


def test_repeated_author_without_predecessor_is_not_guessed():
    entries, warnings = parse_bibitems(item('a', r'\bysame'))
    assert normalize_entry(entries[0]).fields['authors'] == []
    assert any('BIBITEM_REPEATED_AUTHOR_UNRESOLVED' in w for w in warnings)


def test_malformed_preceding_head_breaks_author_history():
    text = item('a', 'J.~Smith') + r'\bibitem[broken label' + item('b', '------')
    entries, warnings = parse_bibitems(text)
    assert [e.original_key for e in entries] == ['a', 'b']
    assert normalize_entry(entries[1]).fields['authors'] == []
    assert any('BIBITEM_REPEATED_AUTHOR_UNRESOLVED key=b' in w for w in warnings)


def test_non_name_preceding_author_breaks_history():
    text = item('a', 'J.~Smith') + item('b', '???') + item('c', '------')
    entries, warnings = parse_bibitems(text)
    assert normalize_entry(entries[2]).fields['authors'] == []
    assert any('BIBITEM_REPEATED_AUTHOR_UNRESOLVED key=c' in w for w in warnings)


def test_ieee_spacing_switches_removed_from_parsed_author_but_retained_raw():
    text = item('a', r'\BIBentryALTinterwordspacing H.~Wang and S.~Ma') + r'\BIBentrySTDinterwordspacing'
    entries, warnings = parse_bibitems(text)
    assert warnings == []
    assert normalize_entry(entries[0]).fields['authors'] == ['H. Wang', 'S. Ma']
    assert r'\BIBentryALTinterwordspacing' in entries[0].fields['raw']
    assert r'\BIBentrySTDinterwordspacing' in entries[0].fields['raw']


def test_natbib_known_name_wrappers_do_not_enter_authors():
    entries, warnings = parse_bibitems(item('a', r'\bibinfo{author}{\bibfnamefont{John} \bibnamefont{Smith}}'))
    assert warnings == []
    assert normalize_entry(entries[0]).fields['authors'] == ['John Smith']
    assert r'\bibinfo' in entries[0].fields['raw']


def test_unknown_author_macro_is_retained_and_diagnosed_not_expanded():
    entries, warnings = parse_bibitems(item('a', r'\unknownauthor{J.~Smith}') + item('b', '------'))
    assert r'\unknownauthor' in entries[0].fields['raw']
    assert r'\unknownauthor' in entries[0].fields['author']
    assert normalize_entry(entries[0]).fields['authors'] == []
    assert normalize_entry(entries[1]).fields['authors'] == []
    assert any('BIBITEM_AUTHOR_MACRO_UNSUPPORTED key=a' in w for w in warnings)
    assert any('BIBITEM_REPEATED_AUTHOR_UNRESOLVED key=b' in w for w in warnings)


def test_acl_newblock_author_year_is_not_part_of_last_surname():
    entries, warnings = parse_bibitems(r'\bibitem{x} John Smith and Jane Doe. 2020.'
                                      r'\newblock A Real Title.\newblock A Venue, 2020.')
    assert warnings == []
    assert normalize_entry(entries[0]).fields['authors'] == ['John Smith', 'Jane Doe']
    assert entries[0].fields['author_raw'] == 'John Smith and Jane Doe. 2020.'


def test_mismatching_terminal_author_year_is_not_silently_removed():
    entries, _ = parse_bibitems(r'\bibitem{x} The Consortium 2019.'
                               r'\newblock A Real Title.\newblock A Venue, 2020.')
    assert entries[0].fields['author'] == 'The Consortium 2019.'


def test_broken_preceding_author_groups_do_not_supply_repeat():
    entries, warnings = parse_bibitems(item('a', r'{John Smith') + item('b', r'\bysame'))
    assert normalize_entry(entries[1]).fields['authors'] == []
    assert any('BIBITEM_REPEATED_AUTHOR_UNRESOLVED key=b' in w for w in warnings)


def test_counted_others_is_truncation_not_fabricated_author():
    entries, warnings = parse_bibitems(r'\bibitem{x} John Smith and Jane Doe and 109 others. 2024.'
                                      r'\newblock A Real Title.\newblock Nature, 2024.')
    fields = normalize_entry(entries[0]).fields
    assert warnings == []
    assert fields['authors'] == ['John Smith', 'Jane Doe']
    assert fields['authors_truncated'] is True
    assert '109 others' in fields['raw']


def test_venue_fields_do_not_include_volume_pages_year_suffix():
    entries, warnings = parse_bibitems(r'\bibitem{x} John Smith.'
                                      r'\newblock A Real Title.\newblock Nature, vol. 4, pp. 12--34, 2024.')
    assert warnings == []
    assert normalize_entry(entries[0]).fields['journal'] == 'Nature'


def test_double_brace_emphasized_title_style_with_linked_venue():
    raw = (r'\bibitem{x} N.~Irges and L.~Karageorgos, \emph{{A {Nested} Title}},'
           r'\href{https://doi.org/10.1000/abc}{\emph{Nucl. Phys. B} {\bfseries 1010} (2025) 42}.')
    entries, warnings = parse_bibitems(raw)
    fields = normalize_entry(entries[0]).fields
    assert warnings == []
    assert fields['parse_mode'] == 'emphasized_title'
    assert fields['authors'] == ['N. Irges', 'L. Karageorgos']
    assert fields['title'] == 'A Nested Title'
    assert fields['journal'] == 'Nucl. Phys. B'
    assert fields['year'] == 2025
    assert r'\href' in fields['raw']


@pytest.mark.parametrize('body', [
    r'An explanation about books, \emph{{A Possible Title}}, \emph{Some Venue}, 2024.',
    r'J.~Smith, \emph{This might be a journal}, 2024.',
    r'J.~Smith, \emph{{A Title Without A Separate Venue}}, 2024.',
])
def test_ambiguous_emphasis_without_required_name_title_venue_evidence_stays_raw(body):
    entries, _ = parse_bibitems(r'\bibitem{x}' + body)
    assert entries[0].fields['parse_mode'] == 'raw_only'
    assert 'title' not in entries[0].fields


@pytest.mark.parametrize('suffix', [', 2025', ', May 2025', r', 2025{\natexlab{b}}'])
def test_newblock_title_terminal_matching_date_keeps_raw_provenance(suffix):
    entries, _ = parse_bibitems(r'\bibitem{x} John Smith. 2025.'
                               r'\newblock A Real Title' + suffix + r'.\newblock Nature, 2025.')
    fields = normalize_entry(entries[0]).fields
    assert fields['title'] == 'A Real Title'
    assert fields['year'] == 2025
    assert fields['year_source'] == 'newblock_author_date'
    assert suffix in fields['title_raw']
    assert fields['title_date_raw'].startswith(',')


def test_contextual_venue_year_does_not_take_forecast_year_from_title():
    entries, _ = parse_bibitems(r'\bibitem{x} John Smith.'
                               r'\newblock Forecasts for 2032.\newblock Nature (2024), pp. 1--12.')
    fields = normalize_entry(entries[0]).fields
    assert fields['year'] == 2024
    assert fields['title'] == 'Forecasts for 2032'


def test_distinct_year_inside_title_is_not_stripped_as_publication_date():
    entries, _ = parse_bibitems(r'\bibitem{x} John Smith. 2025.'
                               r'\newblock The Forecast, 2032.\newblock Nature, 2025.')
    fields = normalize_entry(entries[0]).fields
    assert fields['title'] == 'The Forecast, 2032'
    assert fields['year'] == 2025


def test_revtex_explicit_bibinfo_year_precedes_erratum_note_year():
    raw = (r'\bibitem{x}\BibitemOpen\bibfield{author}{'
           r'\bibinfo{author}{\bibfnamefont{J.}\ \bibnamefont{Smith}} and '
           r'\bibinfo{author}{\bibfnamefont{A.}~\bibnamefont{Doe}}}'
           r'\href{\doibase 10.1000/example}{\bibfield{journal}{\bibinfo{journal}{Nature}}'
           r'\bibinfo{year}{2009}}\bibinfo{note}{Erratum: Nature (2010)}\BibitemShut{NoStop}')
    entries, warnings = parse_bibitems(raw)
    fields = normalize_entry(entries[0]).fields
    assert warnings == []
    assert fields['authors'] == ['J. Smith', 'A. Doe']
    assert fields['year'] == 2009
    assert fields['year_source'] == 'explicit_bibinfo'
    assert fields['journal'] == 'Nature'
    assert fields['title'] is None
    assert '2010' in fields['note']


def test_revtex_author_control_space_newline_and_collaboration_preserved():
    raw = (r'\bibitem{x}\bibinfo{author}{\bibfnamefont{H.~Y.~J.}' + '\\\n'
           + r'\bibnamefont{Chan}}\bibinfo{collaboration}{Example Collaboration}\bibinfo{year}{2025}')
    entries, warnings = parse_bibitems(raw)
    fields = normalize_entry(entries[0]).fields
    assert warnings == []
    assert fields['authors'] == ['H. Y. J. Chan']
    assert fields['collaboration'] == 'Example Collaboration'


def test_nested_note_bibinfo_year_does_not_override_explicit_year():
    entries, warnings = parse_bibitems(r'\bibitem{x}\bibinfo{author}{J. Smith}'
                                      r'\bibinfo{year}{2009}\bibinfo{note}{Erratum (\bibinfo{year}{2010})}')
    assert warnings == []
    assert normalize_entry(entries[0]).fields['year'] == 2009


@pytest.mark.parametrize('identifier', [r'\doarXiv{2504.10387}', r'\dodoi{10.1000/example}', r'\url{https://example.org}'])
def test_omitted_astronomy_title_does_not_turn_identifier_into_title(identifier):
    entries, warnings = parse_bibitems(r'\bibitem{x} Chan, H. Y. J., Smith, J., {et~al.} 2025.'
                                      r'\newblock ' + identifier)
    fields = normalize_entry(entries[0]).fields
    assert warnings == []
    assert fields['title'] is None
    assert fields['authors'] == []
    assert fields['year'] == 2025
    assert fields['parse_mode'] == 'raw_only'
    assert identifier in fields['raw']


def test_unknown_whole_macro_cannot_become_title_or_authors():
    entries, warnings = parse_bibitems(r'\bibitem{x} John Smith, 2025.\newblock\unknownpublication{Anything}')
    fields = normalize_entry(entries[0]).fields
    assert fields['title'] is None and fields['authors'] == []
    assert any('BIBITEM_TITLE_MACRO_UNSUPPORTED' in w for w in warnings)
    assert r'\unknownpublication' in fields['raw']


@pytest.mark.parametrize('identifier', [r'\path{doi:10.1000/example}', r'\path{doi:https://doi.org/10.1000/example}'])
def test_pure_doi_path_is_identifier_not_title(identifier):
    entries, warnings = parse_bibitems(r'\bibitem{x} John Smith, 2024.\newblock ' + identifier)
    fields = normalize_entry(entries[0]).fields
    assert warnings == []
    assert fields['title'] is None and fields['authors'] == []
    assert fields['doi'] == '10.1000/example'


@pytest.mark.parametrize('suffix,volume', [(', volume~19.', '19'), (r', volume 141 of {\em Encyclopaedia of Mathematics}.', '141')])
def test_newblock_book_volume_suffix_becomes_metadata(suffix, volume):
    entries, warnings = parse_bibitems(r'\bibitem{x} John Smith.\newblock {\em A Book Title}'
                                      + suffix + r'\newblock A Publisher, 2024.')
    fields = normalize_entry(entries[0]).fields
    assert warnings == []
    assert fields['title'] == 'A Book Title'
    assert fields['volume'] == volume
    assert 'volume' in fields['title_raw']
    if 'of' in suffix:
        assert 'Encyclopaedia' in fields['series']


def test_numeric_month_title_date_requires_valid_month_and_matching_year():
    entries, _ = parse_bibitems(r'\bibitem{x} John Smith. 2020.\newblock A Manual, 10 2020.'
                               r'\newblock A Publisher, 2020.')
    fields = normalize_entry(entries[0]).fields
    assert fields['title'] == 'A Manual' and fields['year'] == 2020
    entries, _ = parse_bibitems(r'\bibitem{x} John Smith. 2020.\newblock A Manual, 13 2020.'
                               r'\newblock A Publisher, 2020.')
    assert normalize_entry(entries[0]).fields['title'] == 'A Manual, 13 2020'


@pytest.mark.parametrize('label', [r'{Donati} {et~al.}(2023{\natexlab{b}}){Donati}, Smith', r'\citeauthoryear{Donati and Others}{Donati}{2023}'])
def test_author_year_label_does_not_mistake_page_2015_for_year(label):
    entries, warnings = parse_bibitems(r'\bibitem[' + label + r']{x}'
                                      r'{Donati}, J.~F., {et~al.} 2023{\natexlab{b}}, \mnras, 525, 2015')
    fields = normalize_entry(entries[0]).fields
    assert warnings == []
    assert fields['year'] == 2023 and fields['year_source'] == 'author_year_label'


def test_ambiguous_raw_year_candidates_are_preserved_without_selecting_last_page():
    entries, warnings = parse_bibitems(r'\bibitem{x} Unstructured text 2023, something else 2046')
    fields = normalize_entry(entries[0]).fields
    assert fields['year'] is None
    assert '2023' in fields['year_raw'] and '2046' in fields['year_raw']
    assert any('BIBITEM_YEAR_UNRESOLVED' in w for w in warnings)


def test_explicit_bibinfo_year_has_precedence_over_label_year():
    entries, _ = parse_bibitems(r'\bibitem[Smith(2024)]{x}\bibinfo{author}{John Smith}\bibinfo{year}{2023}')
    assert normalize_entry(entries[0]).fields['year'] == 2023


def test_person_bibinfo_is_author_only_in_explicit_author_field():
    entries, warnings = parse_bibitems(r'\bibitem{x}\bibfield{author}{\bibinfo{person}{John Smith},'
                                      r'\bibinfo{person}{Jane Doe}}\bibinfo{date}{2024-03-01}'
                                      r'\bibinfo{organization}{IEEE}\bibinfo{numpages}{35}')
    fields = normalize_entry(entries[0]).fields
    assert warnings == []
    assert fields['authors'] == ['John Smith', 'Jane Doe']
    assert fields['year'] == 2024 and fields['organization'] == 'IEEE'
    assert fields['numpages'] == '35'
    entries, warnings = parse_bibitems(r'\bibitem{x}\bibinfo{person}{John Smith}\bibinfo{year}{2024}')
    assert normalize_entry(entries[0]).fields['authors'] == []
    assert any('UNSUPPORTED_FIELD: person' in w for w in warnings)


def test_auxiliary_month_date_does_not_override_explicit_publication_year():
    entries, warnings = parse_bibitems(r'\bibitem{x}\bibinfo{author}{John Smith}'
                                      r'\bibinfo{date}{Nov.}\bibinfo{year}{2022}')
    fields = normalize_entry(entries[0]).fields
    assert warnings == []
    assert fields['year'] == 2022 and fields['date_raw'] == 'Nov.'


def test_invalid_or_ambiguous_explicit_dates_do_not_guess_year():
    for date in ['2024-15-01', '02/03/2024']:
        entries, warnings = parse_bibitems(r'\bibitem{x}\bibinfo{date}{' + date + '}')
        fields = normalize_entry(entries[0]).fields
        assert fields['year'] is None
        assert fields['date_raw'] == date
        assert any('DATE_UNRESOLVED' in w for w in warnings)


def test_acl_known_year_disambiguator_removed_only_as_part_of_terminal_date():
    entries, warnings = parse_bibitems(r'\bibitem[Smith(2025{\natexlab{a}})]{x}'
                                      r'John Smith. 2025{\natexlab{a}}.'
                                      r'\newblock A Title.\newblock A Venue, 2025.')
    fields = normalize_entry(entries[0]).fields
    assert warnings == []
    assert fields['authors'] == ['John Smith']
    assert r'\natexlab{a}' in fields['author_raw']
    entries, warnings = parse_bibitems(r'\bibitem{x}\natexlaba John Smith.'
                                      r'\newblock A Title.\newblock A Venue, 2025.')
    assert normalize_entry(entries[0]).fields['authors'] == []
    assert any('AUTHOR_MACRO_UNSUPPORTED' in w for w in warnings)


def test_semantically_identical_literal_accent_conditionals_in_names():
    author = r'Y.~Suba\ifmmode\mbox{\c{s}}\else\c{s}\fi{}\ifmmode\imath\else~\i\fi{}'
    entries, warnings = parse_bibitems(r'\bibitem{x}\bibinfo{author}{' + author + r'}\bibinfo{year}{2017}')
    fields = normalize_entry(entries[0]).fields
    assert warnings == []
    assert fields['authors'] == ['Y. Subaşi']
    assert r'\ifmmode' in fields['raw']


def test_different_or_unknown_conditional_name_branches_stay_unresolved():
    entries, warnings = parse_bibitems(r'\bibitem{x}\bibinfo{author}{John \ifmmode\alpha\else\i\fi Smith}')
    assert normalize_entry(entries[0]).fields['authors'] == []
    assert any('AUTHOR_MACRO_UNSUPPORTED' in w for w in warnings)
