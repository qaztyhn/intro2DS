from m2.src.parsing.bibliography import clean_text, extract_bibliography_detailed, parse_bibitems, normalize_entry
from m2.src.parsing.contracts import GatheredDocument


def reconcile(native, fallback):
    doc = GatheredDocument('2507.00001', 1, 'main.tex', '', [],
                           {'refs.bib': native, 'main.bbl.bib': fallback}, 'ok')
    sink = []
    entries, warnings = extract_bibliography_detailed(doc, conflict_sink=sink)
    return entries, warnings, sink


def test_doi_url_alternative_keeps_native_and_preserves_fallback():
    entries, warnings, rows = reconcile(
        '@article{x,title={A real study},doi={10.1000/abc},url={https://publisher.example/article}}',
        '@article{x,title={A real study},doi={10.1000/abc},url={https://doi.org/10.1000/abc}}')
    assert not warnings
    assert entries[0].fields['url'] == 'https://publisher.example/article'
    assert entries[0].fields['alternative_urls'] == ['https://doi.org/10.1000/abc']
    assert rows[0]['identity'] == 'doi' and not rows[0]['conflicting_fields']


def test_repeated_doi_resolver_prefix_is_a_formatting_artifact():
    entries, warnings, _ = reconcile(
        '@article{x,title={A real study},doi={10.1000/abc},url={https://publisher.example/article}}',
        '@article{x,title={A real study},doi={10.1000/abc},url={https://doi.org/https://doi.org/10.1000/abc}}')
    assert not warnings
    assert entries[0].fields['alternative_urls'] == ['https://doi.org/10.1000/abc']


def test_repository_link_is_not_a_journal():
    entries, _ = parse_bibitems(r'\bibitem{x} A. Author.\newblock Actual title.'
                              r'\newblock \url{https://github.com/org/project}.')
    fields = normalize_entry(entries[0]).fields
    assert fields['journal'] is None and fields['url'] == 'https://github.com/org/project'


def test_arxiv_abs_pdf_url_equivalence_is_host_checked():
    _, warnings, _ = reconcile(
        '@article{x,title={A real study},url={https://arxiv.org/abs/2301.00001}}',
        '@article{x,title={A real study},url={https://arxiv.org/pdf/2301.00001v2.pdf}}')
    assert not warnings
    _, warnings, _ = reconcile(
        '@article{x,title={A real study},url={https://arxiv.org/abs/2301.00001}}',
        '@article{x,title={A real study},url={https://notarxiv.org/abs/2301.00001}}')
    assert warnings


def test_independent_missing_fields_merge_under_explicit_identity_but_conflict_stays_visible():
    entries, warnings, rows = reconcile(
        '@article{x,title={A real study},doi={10.1000/abc},year={2020}}',
        '@article{x,title={A real study},doi={10.1000/abc},year={2021},journal={Recovered Journal}}')
    assert entries[0].fields['year'] == 2020
    assert entries[0].fields['journal'] == 'Recovered Journal'
    assert any('conflicting_fields=year' in w for w in warnings)
    assert rows[0]['filled_fields'] == ['journal']
    assert rows[0]['native']['year'] == 2020 and rows[0]['fallback']['year'] == 2021


def test_shared_key_without_identity_cannot_mix_conflicting_records():
    entries, warnings, rows = reconcile(
        '@article{x,title={Native work},year={2020}}',
        '@article{x,title={Other work},year={2020},journal={Wrong Journal}}')
    assert warnings and not entries[0].fields['journal']
    assert not rows[0]['allow_enrichment']


def test_arxiv_substring_on_unrelated_host_cannot_establish_identity():
    entries, warnings, rows = reconcile(
        '@article{x,title={Native work},url={https://notarxiv.org/abs/2301.00001}}',
        '@article{x,title={Other work},url={https://arxiv.org/abs/2301.00001},journal={Wrong}}')
    assert warnings
    assert not entries[0].fields['journal']
    assert rows[0]['identity'] is None and not rows[0]['allow_enrichment']


def test_related_doi_in_note_does_not_identify_the_work():
    entries, warnings, rows = reconcile(
        '@article{x,title={Native work},note={See related paper doi:10.1000/abc}}',
        '@article{x,title={Other work},note={See related paper doi:10.1000/abc},journal={Wrong}}')
    assert warnings and not entries[0].fields['journal']
    assert rows[0]['identity'] is None and not rows[0]['allow_enrichment']


def test_long_exact_title_with_missing_year_can_match_ordered_initials():
    entries, warnings, _ = reconcile(
        '@article{x,title={Large scale dataset distillation with domain shift},author={Loo, Noel and Rus, Daniela}}',
        '@article{x,title={Large scale dataset distillation with domain shift},author={Loo, N. and Rus, D.}}')
    assert not warnings and entries[0].fields['authors'] == ['Noel Loo','Daniela Rus']


def test_explicit_arxiv_can_match_initials_when_bbl_omits_title():
    _, warnings, _ = reconcile(
        '@article{x,title={Native title},author={Teodori, Luca},journal={arXiv preprint arXiv:2501.00001}}',
        '@article{x,author={Teodori, L.},eprint={2501.00001}}')
    assert not warnings


def test_unicode_initial_letters_and_distinct_non_latin_names():
    _, warnings, _ = reconcile(
        '@article{x,title={Actual study},year={2020},author={Stawiński, Łukasz}}',
        '@article{x,title={Actual study},year={2020},author={Stawiński, Ł.}}')
    assert not warnings
    _, warnings, _ = reconcile(
        '@article{x,title={Actual study},year={2020},author={王明}}',
        '@article{x,title={Actual study},year={2020},author={李明}}')
    assert warnings


def test_apostrophe_inside_given_name_is_one_component():
    entries,warnings,_=reconcile(
        "@article{x,title={Actual title},doi={10.1000/abc},author={Ando, Shin'ichiro}}",
        '@article{x,title={Actual title},doi={10.1000/abc},author={Ando, S.}}')
    assert not warnings
    assert entries[0].fields['authors']==["Shin'ichiro Ando"]


def test_explicit_arxiv_contradiction_keeps_evidence_in_report():
    _, warnings, rows = reconcile(
        '@article{x,title={A real study},doi={10.1000/a},eprint={2301.00001}}',
        '@article{x,title={A real study},doi={10.1000/a},eprint={2301.00002},journal={Wrong}}')
    assert any('arxiv_id' in w for w in warnings)
    assert not rows[0]['allow_enrichment']
    assert rows[0]['native_identifiers']['arxiv_id']=='2301.00001'
    assert rows[0]['fallback_identifiers']['arxiv_id']=='2301.00002'


def test_different_identifiers_never_merge_missing_fields():
    entries, warnings, rows = reconcile(
        '@article{x,title={A real study},doi={10.1000/a}}',
        '@article{x,title={A real study},doi={10.1000/b},journal={Wrong}}')
    assert warnings and not entries[0].fields['journal']
    assert rows[0]['conflicting_fields'] == ['doi']


def test_href_visible_title_and_nested_braces_are_preserved():
    assert clean_text(r'\href{https://example.org/a}{A {nested} title}') == 'A nested title'
    assert clean_text(r'\href{https://example.org/a}{A $x$ title}') == 'A $x$ title'
    # Malformed/unsupported syntax is retained, never consumed as a later group.
    assert r'\href' in clean_text(r'\href bad {title} and {another}')
    assert clean_text(r'12\penalty0 (4):307--392') == '12(4):307–392'


def test_arxiv_number_label_does_not_override_publication_year():
    text = (r'\bibitem{x} J. Smith, ``Actual title,'' Journal, 2020. '
            r'[\href{https://arxiv.org/abs/2004.08668}{{\ttfamily 2004.08668}}].')
    entries, _ = parse_bibitems(text)
    assert normalize_entry(entries[0]).fields['year'] == 2020


def test_conflicting_explicit_entry_types_reported_without_changing_native_type():
    entries, warnings, rows = reconcile('@article{x,title={Study}}', '@book{x,title={Study}}')
    assert entries[0].entry_type == 'article'
    assert rows[0]['conflicting_fields'] == ['entry_type']
    assert any('BIB_SOURCE_CONFLICT' in w for w in warnings)
