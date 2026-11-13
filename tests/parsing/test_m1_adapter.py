import json
from pathlib import Path

import pytest

from src.parsing.m1_adapter import load_versions, M1AdapterError


ID = '2507.01659'


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def manifest(version=1):
    return dict(arxiv_id=ID, version=version, versioned_id=f'{ID}v{version}',
        download_status='ok', extract_status='ok', parse_status='pending', source_format='tex',
        source_files=['paper.tex'], archive_path=None, archive_bytes=20,
        unpacked_total_bytes=20, kept_source_bytes=20, error_code=None)


def corpus(tmp_path, expected=(1,), observed=(1,)):
    run = tmp_path / 'run'
    paper = run / 'papers/2507-01659'
    save(paper / 'metadata.json', {'arxiv_id': ID, 'versions': list(expected), 'status': 'ok'})
    for v in observed:
        save(paper / f'versions/v{v}/manifest.json', manifest(v))
    return run, paper


def test_all_versions_union_and_missing_manifest_represented(tmp_path, caplog):
    run, paper = corpus(tmp_path, expected=(1, 2), observed=(1, 3))
    result = load_versions(run, ID)
    assert [v.version for v in result] == [1, 2, 3]
    assert result[1].error_code == 'MISSING_MANIFEST'
    assert result[1].extract_status == 'failed'
    assert result[2].source_dir == paper / 'versions/v3/source'
    assert 'VERSION_NOT_IN_METADATA' in caplog.text


def test_does_not_read_semantic_scholar_references(tmp_path, monkeypatch):
    run, paper = corpus(tmp_path)
    path = paper / 'references.json'
    path.write_text('{broken JSON')
    original = Path.open
    def guard(target, *args, **kwargs):
        if target == path: pytest.fail('M2 adapter must not read S2 references')
        return original(target, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', guard)
    assert load_versions(run, ID)[0].source_files == ('paper.tex',)


def test_missing_metadata_recovers_observed_versions_with_warning(tmp_path, caplog):
    run, paper = corpus(tmp_path, observed=(1, 2))
    (paper / 'metadata.json').unlink()
    assert [v.version for v in load_versions(run, ID)] == [1, 2]
    assert 'MISSING_METADATA' in caplog.text


@pytest.mark.parametrize('versions', [[True], [0], [-1], [1, 1], '1', [1, '2']])
def test_invalid_metadata_versions_fail_explicitly(tmp_path, versions):
    run, paper = corpus(tmp_path)
    save(paper / 'metadata.json', {'arxiv_id': ID, 'versions': versions})
    with pytest.raises(M1AdapterError, match='INVALID_VERSIONS'):
        load_versions(run, ID)


def test_wrong_metadata_identity_fails_publication(tmp_path):
    run, paper = corpus(tmp_path)
    save(paper / 'metadata.json', {'arxiv_id': '2507.01660', 'versions': [1]})
    with pytest.raises(M1AdapterError, match='IDENTITY_MISMATCH'):
        load_versions(run, ID)


@pytest.mark.parametrize('mutation', [
    {'arxiv_id': '2507.01660'}, {'version': 2}, {'source_files': ['../outside.tex']},
    {'source_files': ['image.png']}, {'source_files': 'paper.tex'},
])
def test_invalid_manifest_is_version_failure_not_silent_drop(tmp_path, mutation):
    run, paper = corpus(tmp_path)
    save(paper / 'versions/v1/manifest.json', {**manifest(), **mutation})
    result = load_versions(run, ID)
    assert len(result) == 1 and result[0].error_code == 'INVALID_MANIFEST'


def test_unreadable_json_is_version_failure(tmp_path):
    run, paper = corpus(tmp_path)
    (paper / 'versions/v1/manifest.json').write_text('{bad')
    assert load_versions(run, ID)[0].error_code == 'UNREADABLE_OR_UNSAFE_MANIFEST'


def test_noncanonical_version_alias_rejected(tmp_path):
    run, paper = corpus(tmp_path)
    (paper / 'versions/v01').mkdir()
    with pytest.raises(M1AdapterError, match='INVALID_VERSION_DIRECTORY'):
        load_versions(run, ID)


def test_failed_discovery_does_not_hide_existing_sources(tmp_path):
    run, paper = corpus(tmp_path)
    save(paper / 'metadata.json', {'arxiv_id': ID, 'versions': None, 'status': 'failed'})
    assert load_versions(run, ID)[0].download_status == 'ok'
