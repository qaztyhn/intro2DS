import json

import pytest

from m1.src.downloader.http import make_manifest
from m1.src.tools.overlay_recovery import build_overlay


def fixture(tmp_path):
    identity='2507.00001'
    original=tmp_path/'original/papers/2507-00001'
    original.mkdir(parents=True)
    metadata={'arxiv_id':identity,'versions':[1,2,3],'title':'Original publication'}
    (original/'metadata.json').write_text(json.dumps(metadata),encoding='utf-8')
    (original/'references.json').write_text('[{"source":"unchanged"}]',encoding='utf-8')
    for number in [1,2]:
        version=original/'versions'/f'v{number}';(version/'source').mkdir(parents=True)
        (version/'source/main.tex').write_text(f'old version {number}',encoding='utf-8')
        (version/'manifest.json').write_text('{}',encoding='utf-8')
    recovered=tmp_path/'recovered/papers/2507-00001/versions/v1'
    (recovered/'source').mkdir(parents=True)
    (recovered/'source/main.tex').write_text('recovered body',encoding='utf-8')
    manifest=make_manifest(identity,1)
    manifest.update(download_status='ok',extract_status='ok',source_format='tex',source_files=['main.tex'])
    (recovered/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    return original,[dict(arxiv_id=identity,version=1,paper_dir=str(original),recovered_run=str(tmp_path/'recovered'))]


def test_overlay_preserves_sibling_versions_metadata_and_missing_version_declaration(tmp_path):
    original,plan=fixture(tmp_path)
    report=build_overlay(plan,tmp_path/'overlay')
    paper=tmp_path/'overlay/papers/2507-00001'
    assert (paper/'metadata.json').read_bytes()==(original/'metadata.json').read_bytes()
    assert (paper/'references.json').read_bytes()==(original/'references.json').read_bytes()
    assert (paper/'versions/v2/source/main.tex').read_text()=='old version 2'
    assert (paper/'versions/v1/source/main.tex').read_text()=='recovered body'
    assert (original/'versions/v1/source/main.tex').read_text()=='old version 1'
    assert report[0]['versions']==[1,2,3]
    assert not (paper/'versions/v3').exists()


def test_invalid_recovery_is_rejected_before_creating_output(tmp_path):
    _,plan=fixture(tmp_path)
    (tmp_path/'recovered/papers/2507-00001/versions/v1/source/main.tex').unlink()
    with pytest.raises(ValueError,match='invalid recovered manifest'):
        build_overlay(plan,tmp_path/'overlay')
    assert not (tmp_path/'overlay').exists()


def test_overlay_rejects_duplicates_and_existing_destination(tmp_path):
    _,plan=fixture(tmp_path)
    with pytest.raises(ValueError,match='duplicate replacement'):
        build_overlay(plan*2,tmp_path/'overlay')
    with pytest.raises(FileExistsError):
        build_overlay(plan,tmp_path/'original')
