import io
import json
import tarfile

import pytest

from m1.src.tools.recover_sources import convert_assets, recover_version


def convert(tmp_path, files):
    original = tmp_path/'original'
    original.mkdir()
    for name, data in files.items():
        path = original/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode())
    source = tmp_path/'source'
    records, warnings = convert_assets(original, source)
    return source, records, warnings


def test_classic_bbl_missing_bib_fallback_preserves_original(tmp_path):
    tex = r'\documentclass{article}\begin{document}\cite{x}\bibliography{missing}\end{document}'
    bbl = r'\begin{thebibliography}{1}\bibitem{x} A title. 2020.\end{thebibliography}'
    source, records, warnings = convert(tmp_path, {'main.tex':tex, 'main.bbl':bbl})
    assert not warnings
    assert (source/'main.bbl.tex').read_text() == bbl
    assert r'\input{main.bbl.tex}' in (source/'main.tex').read_text()
    assert (tmp_path/'original/main.tex').read_text() == tex
    assert all(r.get('derived_sha256') for r in records)


def test_biber_explicit_input_converts_to_bib(tmp_path):
    source, _, warnings = convert(tmp_path, {
        'main.tex':r'\documentclass{article}\input{ref.bbl}',
        'ref.bbl':r'\entry{x}{article}{}\field{title}{Real Title}\field{year}{2020}\endentry'})
    assert not warnings
    assert '@article{x,' in (source/'ref.bbl.bib').read_text()
    assert r'\bibliography{ref.bbl.bib}' in (source/'main.tex').read_text()


def test_empty_bbl_is_retained_as_explicit_empty_tex(tmp_path):
    raw=r'\begin{thebibliography}{}\end{thebibliography}'
    source,records,warnings=convert(tmp_path,{'main.tex':r'\documentclass{article}\bibliography{missing}',
                                           'main.bbl':raw})
    assert not warnings
    assert (source/'main.bbl.tex').read_text()==raw
    assert any(r.get('conversion')=='empty_bibliography_to_tex' for r in records)


def test_actual_bib_is_not_replaced_by_bbl(tmp_path):
    tex = r'\documentclass{article}\bibliography{refs}'
    source, _, _ = convert(tmp_path, {'main.tex':tex,
        'refs.bib':'@article{x,title={Actual}}', 'main.bbl':r'\bibitem{x} Generated'})
    main = (source/'main.tex').read_text()
    assert tex in main
    assert r'\input{main.bbl.tex}' in main


def test_identical_root_bbl_copies_are_not_a_resource_conflict(tmp_path):
    source, records, warnings = convert(tmp_path, {
        'nested/main.tex':r'\documentclass{article}\bibliography{missing}',
        'main.bbl':r'\bibitem{x} A.', 'nested/main.bbl':r'\bibitem{x} A.'})
    assert not warnings
    assert any(r.get('equivalent_bbl_resources') for r in records)
    assert r'\input{main.bbl.tex}' in (source/'nested/main.tex').read_text()


def test_different_root_bbl_copies_stay_ambiguous(tmp_path):
    _, _, warnings = convert(tmp_path, {
        'nested/main.tex':r'\documentclass{article}\bibliography{missing}',
        'main.bbl':r'\bibitem{x} A.', 'nested/main.bbl':r'\bibitem{y} B.'})
    assert any('ambiguous root BBL' in w for w in warnings)


def test_rare_text_assets_and_non_source_txt(tmp_path):
    source, records, warnings = convert(tmp_path, {
        'main.ltx':r'\documentclass{article}\input{tables/a.inc}',
        'tables/a.inc':r'\section{Recovered} Real text.',
        'other.bib.txt':'@article{x,title={Actual bibliography}}',
        'anthology.bib.txt':'Please download the bibliography elsewhere.',
        'bad.sty':'\0binary'})
    assert (source/'main.ltx.tex').is_file()
    assert r'\input{tables/a.inc.tex}' in (source/'main.ltx.tex').read_text()
    assert (source/'other.bib.txt.bib').is_file()
    assert not (source/'anthology.bib.txt.tex').exists()
    assert not (source/'bad.sty.tex').exists()
    assert len(warnings) == 2


def test_support_only_does_not_invent_document(tmp_path):
    source, _, warnings = convert(tmp_path, {'ref.bbl':r'\bibitem{x} A.'})
    assert not list(source.iterdir())
    assert warnings


def test_plain_txt_is_tex_only_when_reached_by_active_inputs(tmp_path):
    source, records, warnings = convert(tmp_path, {
        'main.tex':r'\documentclass{article}\input{sections/a}\iffalse\input{inactive.txt}\fi',
        'sections/a.tex':r'\input{examples/plain.txt}',
        'examples/plain.txt':'Plain narrative example without any LaTeX commands.',
        'inactive.txt':'Not part of active body.', 'chat.txt':'Unrelated chat log.'})
    assert (source/'examples/plain.txt.tex').read_text().startswith('Plain narrative')
    assert not (source/'chat.txt.tex').exists()
    assert not (source/'inactive.txt.tex').exists()
    assert any(r.get('referenced_on_compilation_path') for r in records)


def test_readme_root_prevents_unused_template_text_becoming_source(tmp_path):
    source, _, _ = convert(tmp_path, {
        '00README.json':'{"sources":[{"filename":"main.tex","usage":"toplevel"},{"filename":"template.tex","usage":"ignore"}]}',
        'main.tex':r'\documentclass{article}\input{body.txt}',
        'template.tex':r'\documentclass{article}\input{chat.txt}',
        'body.txt':'Actual paper body.', 'chat.txt':'Unused template data.'})
    assert (source/'body.txt.tex').exists()
    assert not (source/'chat.txt.tex').exists()


def test_bibtex_style_is_retained_as_evidence_not_falsely_converted_to_tex(tmp_path):
    source, records, warnings = convert(tmp_path, {'main.tex':r'\documentclass{article}',
        'plain.bst':'ENTRY { title } {} {}\nREAD\nITERATE {call.type$}'})
    assert not warnings and not (source/'plain.bst.tex').exists()
    assert any(r.get('conversion')=='bibliography_style_source_only' for r in records)


@pytest.mark.parametrize('directive',[r'\bibliography{refs.bib.txt}',
    r'\bibliography{refs}',r'\addbibresource[location=local]{refs.bib.txt}'])
def test_converted_bibliography_directive_is_rewritten(tmp_path,directive):
    source, _, warnings = convert(tmp_path, {'main.tex':r'\documentclass{article}'+directive,
        'refs.bib.txt':'@article{x,title={Real bibliography},year={2020}}'})
    assert not warnings
    main=(source/'main.tex').read_text()
    assert '{refs.bib.txt.bib}' in main
    if 'location' in directive:
        assert '[location=local]' in main


def test_amsrefs_to_bib_retains_repeated_authors(tmp_path):
    source, _, warnings = convert(tmp_path, {'main.tex':r'\documentclass{article}\bibliography{missing}',
        'main.bbl':r'\bib{x}{article}{author={Doe, Jane},author={Smith, John},title={Real},date={2024},}'})
    assert not warnings
    assert 'Doe, Jane and Smith, John' in (source/'main.bbl.bib').read_text()
    assert r'\bibliography{main.bbl.bib}' in (source/'main.tex').read_text()


def test_conversion_collision_preserves_real_original(tmp_path):
    source, _, warnings = convert(tmp_path, {'main.tex':r'\documentclass{article}',
        'main.bbl':r'\bibitem{x} A.', 'main.bbl.tex':'Actual existing source'})
    assert warnings
    assert (source/'main.bbl.tex').read_text() == 'Actual existing source'


def test_recover_archive_creates_valid_new_run(tmp_path):
    archive = tmp_path/'input.archive'
    with tarfile.open(archive, 'w:gz') as tar:
        for name, value in {'main.tex':r'\documentclass{article}\bibliography{missing}',
                            'main.bbl':r'\bibitem{x} A title. 2020.'}.items():
            data=value.encode(); member=tarfile.TarInfo(name); member.size=len(data)
            tar.addfile(member,io.BytesIO(data))
    item = dict(arxiv_id='2507.01659',version=1,member_id='24120058',archive=str(archive))
    output = tmp_path/'recovered'
    row=recover_version(item,output)
    assert row['status']=='recovered'
    directory=output/'papers/2507-01659/versions/v1'
    manifest=json.loads((directory/'manifest.json').read_text())
    assert set(manifest['source_files'])=={'main.tex','main.bbl.tex'}
    provenance=json.loads((directory/'recovery_provenance.json').read_text())
    assert provenance['archive_sha256'] and not provenance['validation']
    with pytest.raises(FileExistsError):
        recover_version(item,output)
