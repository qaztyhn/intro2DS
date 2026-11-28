"""Direct source regressions from the final sixteen partial pilot versions."""
import pytest

from m2.src.parsing.bibliography import normalize_entry, parse_bib, parse_bibitems


def fields(text):
    entries, warnings = parse_bibitems(r'\bibitem{x}' + text)
    return normalize_entry(entries[0]).fields, warnings


def test_href_wrapped_doi_path_does_not_turn_complete_reference_into_author_list():
    result, warnings = fields(
        r'F.~Asadi, Y.~Hurmuzlu, A complete title, IEEE Transactions on Robotics 40 (2024) 697--713.'
        r'\newblock\href{https://doi.org/10.1109/TRO.2023.3339529}{\path{doi:10.1109/TRO.2023.3339529}}.')
    assert warnings == []
    assert result['title'] is None and result['authors'] == []
    assert result['doi'] == '10.1109/tro.2023.3339529'
    assert result['year'] == 2024


def test_arxiv_display_identifier_tail_is_not_a_publication_year():
    result, warnings = fields(
        r'P.~McFadden and K.~Skenderis, \emph{{The Holographic Universe}},'
        r'\href{https://doi.org/10.1088/1742-6596/222/1/012007}{\emph{J. Phys. Conf. Ser.} (2010) 012007}'
        r'[\href{https://arxiv.org/abs/1001.2007}{{\ttfamily 1001.2007}}].')
    assert warnings == []
    assert result['year'] == 2010


def test_semicolon_family_initial_author_list_preserves_people_not_fragments():
    result, warnings = fields(
        r'Bain, M.; Nagrani, A.; Varol, G.; and Zisserman, A. 2022.'
        r'\newblock Frozen in Time.\newblock arXiv:2104.00650.')
    assert warnings == []
    assert result['authors'] == ['M. Bain', 'A. Nagrani', 'G. Varol', 'A. Zisserman']


def test_semicolon_truncation_and_surname_particles_preserved():
    result, warnings = fields(
        r'Sharma, A.; van~den Oord, A.; et~al. 2024.'
        r'\newblock A Work Title.\newblock A Venue, 2024.')
    assert warnings == []
    assert result['authors'] == ['A. Sharma', 'A. van den Oord']
    assert result['authors_truncated'] is True


def test_unsupported_semicolon_fragments_are_diagnosed_not_guessed():
    result, warnings = fields(r'Smith, prose; something else.\newblock A Title.\newblock A Venue, 2024.')
    assert result['authors'] == []
    assert any('AUTHOR_STYLE_UNSUPPORTED' in w for w in warnings)


def test_family_initials_before_et_al_are_joined_before_truncation():
    result, warnings = fields(r'Serre, J.-P. et~al.\newblock\emph{Linear representations}, volume~42.'
                              r'\newblock Springer, 1977.')
    assert warnings == []
    assert result['authors'] == ['J.-P. Serre']
    assert result['authors_truncated'] is True


def test_two_block_missing_author_layout_does_not_invent_author_and_title():
    result, warnings = fields(r'Multilabel random walker image segmentation using prior models.'
                              r'\newblock In\emph{2005 IEEE Conference}, pages 763--770. IEEE, 2005.')
    assert warnings == []
    assert result['title'] is None and result['authors'] == []


def test_explicit_title_page_suffix_is_metadata_with_raw_preserved():
    result, warnings = fields(r'Hans Jensen.\newblock{\em Electron Correlation in Molecules}, pages 179--206.'
                              r'\newblock Springer US, 1994.')
    assert warnings == []
    assert result['title'] == 'Electron Correlation in Molecules'
    assert result['pages'] == '179--206'
    assert 'pages 179--206' in result['title_raw']


def test_eid_and_literal_apostrophe_name_are_not_unknown_metadata():
    result, warnings = fields(r'\bibinfo{author}{S.~P. O\textquoteright{}Sullivan}'
                              r'\bibinfo{year}{2022}\bibinfo{eid}{A123}')
    assert warnings == []
    assert result['authors'] == ["S. P. O'Sullivan"]
    assert result['eid'] == 'A123'


def test_explicit_collaboration_venue_date_is_not_title_year_without_guessing_people():
    result, warnings = fields(r'{\scshape Planck} collaboration, \emph{{Planck 2018 results}},'
                              r'\href{https://doi.org/10.1000/example}{\emph{A\&A} {\bfseries 641} (2020) A6}.')
    assert warnings == []
    assert result['year'] == 2020
    assert result['year_source'] == 'emphasized_venue_date'
    assert result['authors'] == []


def test_strict_astronomy_author_prefix_supplies_following_repeat_without_guessing():
    raw = (r'\bibitem[Schive et~al.(2014{\natexlab{a}})]{a}'
           r'Schive, H.-Y., Chiueh, T., \& Broadhurst, T. 2014{\natexlab{a}}, Nature Phys., 10, 496.'
           r'\bibitem[Schive et~al.(2020)]{b}---. 2020, Phys. Rev. Lett., 124, 201301.')
    entries, warnings = parse_bibitems(raw)
    assert warnings == []
    expected = ['H.-Y. Schive', 'T. Chiueh', 'T. Broadhurst']
    assert normalize_entry(entries[0]).fields['authors'] == expected
    assert normalize_entry(entries[1]).fields['authors'] == expected
    assert entries[0].fields['author_source'] == 'validated_author_year_prefix'
    assert entries[1].fields['author_repeated_from'] == 'a'


def test_invalid_astronomy_author_prefix_does_not_supply_repeat():
    raw = (r'\bibitem[Example(2024)]{a}A work title, some prose 2024, Nature, 4, 3.'
           r'\bibitem[Example(2025)]{b}---. 2025, Nature, 5, 4.')
    entries, warnings = parse_bibitems(raw)
    assert normalize_entry(entries[0]).fields['authors'] == []
    assert normalize_entry(entries[1]).fields['authors'] == []
    assert any('REPEATED_AUTHOR_UNRESOLVED' in w for w in warnings)


def test_native_bibtex_known_literal_author_formatting_preserves_raw_field():
    raw_author = (r'O\textquoteright{}Sullivan, S. P. and '
                  r'Suba\ifmmode\mbox{\c{s}}\else\c{s}\fi{}\ifmmode\imath\else\i\fi{}, '
                  r'Yi\ifmmode\breve{g}\else\u{g}\fi{}it')
    entries, warnings = parse_bib('@article{x,title={A Title},author={' + raw_author + '},year={2017}}')
    normalized = normalize_entry(entries[0])
    assert warnings == []
    assert normalized.fields['authors'] == ["S. P. O'Sullivan", 'Yiğit Subaşi']
    assert entries[0].fields['author'] == raw_author
    assert normalized.fields['author'] == raw_author


@pytest.mark.parametrize('condition', [
    r'\ifmmode\breveg\else\u{g}\fi',
    r'\ifmmode\breve{g}\else\u{a}\fi',
    r'\ifmmode\unknown{g}\else\u{g}\fi',
])
def test_native_unknown_or_different_conditional_author_branches_remain_literal(condition):
    raw_author = 'Family, Yi' + condition + 'it'
    entries, warnings = parse_bib('@article{x,author={' + raw_author + '}}')
    normalized = normalize_entry(entries[0])
    assert warnings == []
    assert r'\ifmmode' in normalized.fields['authors'][0]
    assert entries[0].fields['author'] == raw_author


def test_complete_ieee_quoted_first_block_is_not_replaced_by_received_note():
    result, warnings = fields(
        r"V.~Patkar and N.~Mehendale, ``Autonomous ground vehicles: technological advancements,''"
        r'{\em International Journal of Intelligent Robotics and Applications}, 2025.'
        r'\newblock Received 24 July 2024; accepted 26 December 2024.')
    assert warnings == []
    assert result['title'] == 'Autonomous ground vehicles: technological advancements'
    assert result['authors'] == ['V. Patkar', 'N. Mehendale']
    assert result['year'] == 2025
    assert result['parse_mode'] == 'quoted_first_block'
    assert 'accepted' in result['continuation_raw']


def test_initialled_author_and_emphasized_book_title_precedes_publisher_block():
    result, warnings = fields(r'M.~M. Wilde, \emph{Quantum information theory}.'
                              r'\newblock Cambridge university press, 2013.')
    assert warnings == []
    assert result['title'] == 'Quantum information theory'
    assert result['authors'] == ['M. M. Wilde']
    assert result['year'] == 2013
    assert result['parse_mode'] == 'emphasized_first_block'


def test_emphasized_title_before_identifier_blocks_stays_title_not_url_label():
    result, warnings = fields(r'J.~Jiang and X.~Sun, \emph{{Optimal space-depth trade-off.}}, 7, 2019.'
                              r'\newblock\href{https://arxiv.org/abs/1907.05087}{{\tt 1907.05087}}.'
                              r'\newblock\href{http://dx.doi.org/10.1137/example}{DOI}.')
    assert warnings == []
    assert result['title'] == 'Optimal space-depth trade-off'
    assert result['authors'] == ['J. Jiang', 'X. Sun']
    assert result['year'] == 2019


def test_smallcaps_wrapped_surname_initial_list_is_not_one_corporate_author():
    result, warnings = fields(r'{\sc Adler, M., and Immerman, N.}'
                              r'\newblock An n! lower bound on formula size.'
                              r'\newblock{\em {ACM} Trans. Comput. Log. 4}, 3 (2003), 296--314.')
    assert warnings == []
    assert result['authors'] == ['M. Adler', 'N. Immerman']
    assert result['journal'] == 'ACM Trans. Comput. Log.'


def test_smallcaps_non_name_group_remains_one_corporate_literal():
    result, warnings = fields(r'{\sc Acme, Incorporated}'
                              r'\newblock A Report.\newblock A Publisher, 2024.')
    assert warnings == []
    assert result['authors'] == ['Acme, Incorporated']


def test_ambiguous_first_block_emphasis_does_not_reclassify_prose_as_person():
    result, _ = fields(r'An explanation about books, \emph{A possible book title}.'
                       r'\newblock A continuation note, 2024.')
    assert result['parse_mode'] != 'emphasized_first_block'


def test_quoted_title_only_reference_does_not_use_edition_note_as_title():
    result, warnings = fields(r"``Road vehicles --- Test scenarios --- Scenario categorization,'' 2024."
                              r'\newblock First edition, February 2024.')
    assert warnings == []
    assert result['title'] == 'Road vehicles — Test scenarios — Scenario categorization'
    assert result['authors'] == [] and result['year'] == 2024
    assert result['parse_mode'] == 'quoted_title_only_first_block'


@pytest.mark.parametrize('venue,expected', [
    (r'{\em J. Artif. Intell. Res. 17\/} (2002), 229--264.', 'J. Artif. Intell. Res.'),
    (r'{\em Fundamenta Mathematicae\/} (1961).', 'Fundamenta Mathematicae'),
    (r'{\em CoRR abs/2301.13329\/} (2023).', 'CoRR'),
    (r'{\em Log. Methods Comput. Sci. 1}, 1 (2005).', 'Log. Methods Comput. Sci.'),
])
def test_balanced_old_em_venue_group_excludes_explicit_publication_metadata(venue, expected):
    result, warnings = fields(r'J.~Smith.\newblock A Work Title.\newblock ' + venue)
    assert warnings == []
    assert result['journal'] == expected


def test_volume_abbreviation_is_metadata_not_book_title():
    result, warnings = fields(r'{\sc Otto, M.}\newblock{\em A Study in Finite Models}, vol.~9 of {\em Lecture Notes in Logic}.'
                              r'\newblock Cambridge University Press, 2017.')
    assert warnings == []
    assert result['title'] == 'A Study in Finite Models'
    assert result['volume'] == '9'
    assert 'Lecture Notes in Logic' in result['series']
