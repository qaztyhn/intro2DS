"""Archive intake must survive source removal and avoid counting duplicate runs."""
import io
import json
from pathlib import Path
import tarfile
import zipfile

import pytest

from m2.src.parsing import archive_reports as A


def fixture_files(identity='2507.11659', prefix='run/papers', validation=True):
    key = identity.replace('.', '-')
    base = f'{prefix}/{key}/'
    records = {
        'metadata.json': {'arxiv_id': identity, 'versions': [1], 'status': 'ok'},
        'references.json': {'status': 'ok', 'references': [{'ref_submission_date': '2020-01-01', 'ref_arxiv_id': None}]},
        'versions/v1/manifest.json': {'arxiv_id': identity, 'version': 1, 'download_status': 'ok',
            'extract_status': 'ok', 'source_format': 'tex', 'source_files': ['article.tex']},
    }
    if validation: records['validation.json'] = {'status': 'ok', 'issues': []}
    files = {base + name: json.dumps(value).encode() for name, value in records.items()}
    files[base + 'versions/v1/source/article.tex'] = b'\\documentclass{article}\\begin{document}Example.\\end{document}'
    return files


def zip_input(tmp_path, files, name='source.zip'):
    path = tmp_path / name
    with zipfile.ZipFile(path, 'x') as z:
        for name, data in files.items(): z.writestr(name, data)
    return path


def ids_file(tmp_path, ids, name='ids.txt'):
    path = tmp_path / name
    path.write_text('\n'.join(ids) + '\n')
    return path


def test_zip_intake_is_idempotent_and_reports_rebuild_after_source_removal(tmp_path):
    archive = zip_input(tmp_path, fixture_files())
    ids = ids_file(tmp_path, ['2507.11659', '2507.11660'])
    output = tmp_path / 'reports'
    A.execute(archive, '24120046', ids, output)
    first = (output / 'M1_REPORT.json').read_bytes()
    A.execute(archive, '24120046', ids, output)
    assert (output / 'M1_REPORT.json').read_bytes() == first
    archive.unlink()
    (output / 'M1_REPORT.json').unlink()
    A.execute(None, None, None, output, rebuild_only=True)
    assert (output / 'M1_REPORT.json').read_bytes() == first
    report = json.loads(first)
    assert report['observed_unique_publications'] == 1
    assert report['expected_unique_publications'] == 2
    member = report['by_member']['24120046']
    assert member['recorded_validation_status'] == {'ok': 1, 'paper_missing': 1}
    assert member['references_total'] == member['references_with_date'] == 1
    assert member['audit']['availability'] == 'absent'
    assert member['audit']['observed_record_counts'] is None
    assert member['artifact_available_for_m2_publications'] == 1


def test_tar_flat_layout_appends_to_same_reports_and_unknown_is_not_failure(tmp_path):
    output = tmp_path / 'reports'
    archive = zip_input(tmp_path, fixture_files())
    A.execute(archive, '24120046', ids_file(tmp_path, ['2507.11659']), output)
    second = tmp_path / 'flat.tar.gz'
    with tarfile.open(second, 'w:gz') as t:
        for name, data in fixture_files('2505.15666', 'corpus', validation=False).items():
            info = tarfile.TarInfo(name);info.size = len(data);t.addfile(info, io.BytesIO(data))
    A.execute(second, '24120085', ids_file(tmp_path, ['2505.15666'], 'second_ids.txt'), output)
    report = json.loads((output / 'M1_REPORT.json').read_text())
    assert report['observed_unique_publications'] == 2
    assert report['by_member']['24120085']['recorded_validation_status'] == {'unknown': 1}
    assert json.loads((output / 'M2_PROFILING.json').read_text())['combined']['counts']['versions'] == 2


def test_changed_member_source_requires_review_and_preserves_existing_report(tmp_path):
    output = tmp_path / 'reports'
    ids = ids_file(tmp_path, ['2507.11659'])
    A.execute(zip_input(tmp_path, fixture_files()), '24120046', ids, output)
    first = (output / 'M1_REPORT.json').read_bytes()
    other = fixture_files();other['extra.txt'] = b'new run'
    with pytest.raises(ValueError, match='different snapshot'):
        A.execute(zip_input(tmp_path, other, 'second.zip'), '24120046', ids, output)
    assert (output / 'M1_REPORT.json').read_bytes() == first


def test_corrupt_snapshot_is_not_silently_used(tmp_path):
    output = tmp_path / 'reports'
    A.execute(zip_input(tmp_path, fixture_files()), '24120046', ids_file(tmp_path, ['2507.11659']), output)
    registry = json.loads((output / 'registry.json').read_text())
    (output / registry['sources'][0]['snapshot'] / 'publications.jsonl').write_text('{}\n')
    with pytest.raises(ValueError, match='verification'):
        A.execute(None, None, None, output, rebuild_only=True)


def test_links_and_unsafe_archive_members_are_rejected(tmp_path):
    with pytest.raises(ValueError): A.group_path('../run/papers/2507-11659/metadata.json')
    archive = tmp_path / 'linked.tar'
    with tarfile.open(archive, 'w') as t:
        info = tarfile.TarInfo('corpus/2507-11659/source');info.type = tarfile.SYMTYPE;info.linkname = '/outside';t.addfile(info)
    with pytest.raises(ValueError, match='link in archive'):
        A.execute(archive, '24120046', ids_file(tmp_path, ['2507.11659']), tmp_path / 'reports')


def test_conflicting_owner_overlap_is_visible_not_double_counted(tmp_path):
    output = tmp_path / 'reports'
    ids = ids_file(tmp_path, ['2507.11659'])
    A.execute(zip_input(tmp_path, fixture_files()), '24120046', ids, output)
    changed = fixture_files(prefix='corpus')
    A.execute(zip_input(tmp_path, changed, 'other.zip'), '24120085', ids, output)
    report = json.loads((output / 'M1_REPORT.json').read_text())
    assert report['observed_unique_publications'] == 1
    assert report['publication_overlap'] == {'2507.11659': ['24120046', '24120085']}
    assert report['combined_observed_validation_status'] is None
    assert json.loads((output / 'M2_PROFILING.json').read_text())['combined'] is None


def test_nested_stored_batches_exclude_backups_and_keep_container_provenance(tmp_path):
    def inner(files):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
            for name, data in files.items(): z.writestr(name, data)
        return buffer.getvalue()
    first = fixture_files(prefix='A_REAL_000/papers')
    second = fixture_files('2507.11660', 'A_REAL_001/papers')
    outer = zip_input(tmp_path, {
        'Data/A_REAL_000.zip': inner(first), 'Data/A_REAL_001.zip': inner(second),
        'Data/BACKUPS/A_REAL_000.zip': inner(first),
        'Data/FINAL_AUDIT/M1_FINAL_SUMMARY.json': b'{"total_papers":2}',
    })
    output = tmp_path / 'reports'
    selection = {'directory': 'Data', 'pattern': 'A_REAL_[0-9][0-9][0-9].zip'}
    A.execute(outer, '24120027', ids_file(tmp_path, ['2507.11659', '2507.11660']), output, selection=selection)
    report = json.loads((output / 'M1_REPORT.json').read_text())
    assert report['observed_unique_publications'] == 2
    entry = report['sources'][0]
    snap = output / entry['snapshot']
    inventory = json.loads((snap / 'inventory.json').read_text())
    assert len(inventory['nested_zip_selection']['selected']) == 2
    with zipfile.ZipFile(snap / 'source_reports.zip') as z:
        assert json.loads(z.read('Data/FINAL_AUDIT/M1_FINAL_SUMMARY.json'))['total_papers'] == 2
    rows = [json.loads(line) for line in (snap / 'versions.jsonl').read_text().splitlines()]
    assert all('Data/A_REAL_' in next(iter(row['source_artifacts'].values()))['archive_member'] for row in rows)
    with pytest.raises(ValueError, match='selection differs'):
        A.execute(outer, '24120027', tmp_path / 'ids.txt', output)


def test_stored_window_is_bounded_and_compressed_outer_is_rejected(tmp_path):
    outer = zip_input(tmp_path, {'inner.zip': b'payload', 'next.txt': b'outside'})
    with zipfile.ZipFile(outer) as z, A.StoredZipWindow(outer, z.getinfo('inner.zip')) as window:
        assert window.read() == b'payload' and window.read() == b''
        window.seek(-2, 2)
        assert window.read(100) == b'ad'
        with pytest.raises(ValueError): window.seek(-1)
    compressed = tmp_path / 'compressed.zip'
    with zipfile.ZipFile(compressed, 'w', zipfile.ZIP_DEFLATED) as z: z.writestr('inner.zip', b'payload')
    with zipfile.ZipFile(compressed) as z:
        with pytest.raises(ValueError, match='ZIP_STORED'):
            A.StoredZipWindow(compressed, z.getinfo('inner.zip'))


def test_temporary_archives_are_not_publications_but_source_subfolders_are():
    assert A.group_path('run/tmp/2507-11659/v1/source.archive') is None
    assert A.group_path('run/papers/2507-11659/versions/v1/source/audit/chapter.tex') == (
        '2507.11659', 'versions/v1/source/audit/chapter.tex')


def test_claimed_success_does_not_override_stored_no_tex_validation_conflict(tmp_path):
    files = fixture_files(prefix='A_REAL_000/papers')
    base = 'A_REAL_000/papers/2507-11659/'
    files.pop(base + 'versions/v1/source/article.tex')
    manifest = json.loads(files[base + 'versions/v1/manifest.json'])
    manifest.update(extract_status='empty', source_files=[], error_code='NO_TEX_SOURCE')
    files[base + 'versions/v1/manifest.json'] = json.dumps(manifest).encode()
    files[base + 'validation.json'] = json.dumps({'status': 'failed', 'issues': [
        {'severity': 'error', 'code': 'MISSING_SOURCE', 'message': 'needs source directory'}]}).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as z:
        for name, data in files.items(): z.writestr(name, data)
    outer = zip_input(tmp_path, {'Data/A_REAL_000.zip': buffer.getvalue(),
        'Data/FINAL_AUDIT/M1_FINAL_SUMMARY.json': json.dumps({'total_papers': 1, 'ok_papers': 1,
            'failed_papers': 0, 'failed_paper_details': []}).encode()})
    output = tmp_path / 'reports'
    A.execute(outer, '24120027', ids_file(tmp_path, ['2507.11659']), output,
              selection={'directory': 'Data', 'pattern': '*.zip'})
    summary = json.loads((output / 'M1_REPORT.json').read_text())['by_member']['24120027']
    assert summary['recorded_validation_status'] == {'failed': 1}
    assert summary['recorded_no_tex_validation_conflicts'][0]['arxiv_id'] == '2507.11659'
    assert summary['source_report_comparisons'][0]['count_mismatches']['ok_papers'] == {'claimed': 1, 'observed': 0}
    edges = json.loads((output / 'M2_EDGE_CASES.json').read_text())['versions']
    assert 'stored_validation_no_tex_conflict' in edges[0]['tags']
