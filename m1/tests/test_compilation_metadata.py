import io
import json
import tarfile

from m1.src.stages.extract import extract_source
from m1.src.tools.recover_sources import recover_version


def archive(tmp_path, readme):
    target = tmp_path/'source.archive'
    with tarfile.open(target, 'w:gz') as tar:
        for name, raw in {'main.tex':b'\\documentclass{article}Body', '00README.json':readme,
                          'unrelated.json':b'{"not":"source"}'}.items():
            member = tarfile.TarInfo(name); member.size = len(raw)
            tar.addfile(member, io.BytesIO(raw))
    return target


def test_exact_name_opt_in_retains_only_compilation_readme(tmp_path):
    source = archive(tmp_path, b'{"sources":[]}')
    normal = extract_source(source, tmp_path/'normal')
    recovery = extract_source(source, tmp_path/'recovery', source_names={'00README.json'})
    assert normal['source_files'] == ['main.tex']
    assert recovery['source_files'] == ['00README.json','main.tex']
    assert not (tmp_path/'recovery/unrelated.json').exists()


def test_recovery_records_compilation_provenance_without_json_as_tex(tmp_path):
    raw = b'{"sources":[{"filename":"main.tex","usage":"toplevel"}]}'
    source = archive(tmp_path, raw)
    row = recover_version({'arxiv_id':'2507.01659','version':1,'archive':str(source)}, tmp_path/'run')
    version = tmp_path/'run/papers/2507-01659/versions/v1'
    hint = json.loads((version/'recovery_compilation.json').read_text())
    assert hint['arxiv_id'] == '2507.01659'
    assert hint['toplevels'] == ['main.tex']
    assert (version/'original_assets/00README.json').read_bytes() == raw
    assert not list((version/'source').glob('*.json*'))
    assert not row['warnings']


def test_malformed_readme_is_retained_and_reported_not_used(tmp_path):
    source = archive(tmp_path, b'{invalid}')
    row = recover_version({'arxiv_id':'2507.01659','version':1,'archive':str(source)}, tmp_path/'run')
    version = tmp_path/'run/papers/2507-01659/versions/v1'
    assert not (version/'recovery_compilation.json').exists()
    assert (version/'original_assets/00README.json').read_bytes() == b'{invalid}'
    assert any('compilation metadata' in w for w in row['warnings'])
