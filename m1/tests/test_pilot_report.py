from pathlib import Path

import pytest

from m1.src.config import load_config
from m1.src.downloader.http import make_manifest
from m1.src.tools import pilot_report as pilot
from m1.src.tools.benchmark import select_sample
from m1.src.utils.common import load_ids, write_json_atomic


def test_pilot200_membership_and_seed():
    cfg = load_config(pilot.DEFAULT_CONFIG)
    ids = load_ids(cfg['ids_file'])
    assert len(ids) == len(set(ids)) == 200
    assert cfg['preserve_source_assets'] and cfg['output_root'].name == 'pilot200_preserve'
    for index, owner in enumerate(pilot.OWNERS.values()):
        population = load_ids(pilot.CONFIGS / 'team' / f'ids_{owner}.txt')
        for block in range(50):
            position = index * 50 + block
            assert [ids[position]] == select_sample(population[100*block:100*(block+1)], 1, 20261128+position)


def test_key_and_field_comparison_exposes_loss_changes_and_enrichment():
    before = {'gone': {}, 'shared': {'year': 2001, 'authors': ['A'], 'title': None}}
    after = {'new': {}, 'shared': {'year': 2002, 'title': 'Added'}}
    assert pilot.compare_entries(before, after) == dict(added=['new'], lost=['gone'], changed_fields=[
        dict(key='shared', field='authors', before=['A'], after=None),
        dict(key='shared', field='year', before=2001, after=2002)])


def test_filtered_file_references_have_literal_paths_and_commands():
    text = (r'\input{../a.dat}\lstinputlisting[language=Python]{tool.py}'
            r'\pgfplotstableread[col sep=comma]{table.csv}\data'
            r'\includegraphics{a.dat}\verb|\input{fake}|\iffalse\input{fake}\fi'
            '\n% \\input{fake}\n' r'\input{other}')
    hits = pilot.filtered_references(text, 'sections/p.tex', 'main.tex',
                                     {'a.dat', 'sections/tool.py', 'table.csv', 'fake', 'sub/other'})
    assert [(h['command'], h['path']) for h in hits] == [
        ('input', 'a.dat'), ('lstinputlisting', 'sections/tool.py'), ('pgfplotstableread', 'table.csv')]


def test_summary_denominators_and_diagnostic_counts():
    r = dict(arxiv_id='2507.01659', version=1, root_candidates=['a', 'b'], partial_reasons=['A', 'A'],
             info_diagnostics=['B'], bbl_comparison={'added': ['k'], 'lost': [], 'changed_fields': []},
             filtered_references=[], bytes=dict.fromkeys(('archive', 'original_assets', 'source', 'total')))
    s = pilot.summarize([r, dict(r, version=2, root_candidates=[], bbl_comparison=None)])
    assert s['multi_root']['value'] == 0.5 and s['multi_root']['denominator'] == 2
    assert s['multiple_roots'] == [('2507.01659', 1, ['a', 'b'])]
    assert s['bbl_gains']['numerator'] == s['bbl_gains']['denominator'] == 1
    assert s['partial_reasons'] == {'A': 2} and s['info_diagnostics'] == {'B': 2}
    assert pilot.summarize([])['bbl_gains']['value'] is None


def test_cache_seed_skips_missing_and_rejects_truncated_archive(tmp_path):
    identity = '2507.01659'
    run = tmp_path / 'old'
    rel = Path('papers/2507-01659')
    write_json_atomic(run / rel / 'metadata.json', dict(arxiv_id=identity, versions=[1, 2],
                      latest_version=2, revised_dates=['date'], status='ok'))
    for v in (1, 2):
        archive = Path(f'tmp/2507-01659/v{v}/source.archive')
        (run / archive).parent.mkdir(parents=True)
        (run / archive).write_bytes(b'%PDF')
        record = make_manifest(identity, v)
        record.update(download_status='ok', archive_path=str(archive), archive_bytes=4 if v == 1 else 10,
                      source_format='pdf', extract_status='empty')
        write_json_atomic(run / rel / f'versions/v{v}/manifest.json', record)
    paper = pilot.inspect_cache(identity, [run])
    assert paper['missing_versions'] == [2]
    cfg = dict(output_root=tmp_path, run_id='new')
    dst = pilot.prepare_run(cfg, [paper])
    saved = pilot.read(dst / rel / 'versions/v1/manifest.json')
    assert saved['download_status'] == 'ok' and saved['extract_status'] == 'pending'
    assert (dst / saved['archive_path']).read_bytes() == b'%PDF'
    assert not (dst / rel / 'versions/v2/manifest.json').exists()
    with pytest.raises(FileExistsError):
        pilot.prepare_run(cfg, [paper])
