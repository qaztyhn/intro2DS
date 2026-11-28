"""Offline pilot inventory, cache preparation and per-version M1/M2 reporting."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import posixpath
import shutil
from tempfile import TemporaryDirectory

from m1.src.config import load_config
from m1.src.downloader.http import make_manifest
from m1.src.metrics import directory_size_bytes
from m1.src.schema import no_symlinks, require_arxiv_id, require_version, safe_path
from m1.src.stages.source_assets import POLICY
from m1.src.tools.audit_sources import audit_run
from m1.src.tools.make_ids import CONFIGS, OWNERS
from m1.src.tools.recover_sources import convert_assets, text_bytes
from m1.src.utils.common import load_ids, paper_key, write_json_atomic
from m2.src.parsing import gathering as g
from m2.src.parsing.benchmark import id_list_digest
from m2.src.parsing.bibliography import extract_bibliography_detailed
from m2.src.parsing.m1_adapter import load_versions
from m2.src.parsing.qa_report import ratio

DEFAULT_CONFIG = CONFIGS / 'pilot200_preserve.yaml'


def read(path):
    return json.loads(no_symlinks(path).read_bytes())


def cache_runs(roots):
    runs = set()
    for root in roots:
        if not root.is_dir():
            raise FileNotFoundError(root)
        for path, dirs, _ in os.walk(root):
            if 'papers' in dirs:
                runs.add(Path(path).absolute())
                dirs.remove('papers')
            dirs[:] = [d for d in dirs if d not in {'tmp', 'tables', 'parts'} and not d.startswith('.')]
    return sorted(runs)


def inspect_cache(identity, runs):
    key = paper_key(identity)
    metadata = references = None
    latest = 0
    known = set()
    archives = {}
    rejected = []
    for run in runs:
        paper = safe_path(run, f'papers/{key}')
        meta = paper / 'metadata.json'
        if meta.is_file():
            data = read(meta)
            if data.get('arxiv_id') != identity:
                raise ValueError(f'{identity} version=unknown stage=inventory IDENTITY_MISMATCH: {meta}')
            versions = [require_version(v) for v in data.get('versions') or []]
            known.update(versions)
            n = data.get('latest_version')
            if (data.get('status') == 'ok' and type(n) is int and n > latest
                    and versions == list(range(1, n + 1)) and (n == 1 or data.get('revised_dates') is not None)):
                metadata, latest = str(meta), n
        ref = paper / 'references.json'
        if references is None and ref.is_file():
            record = read(ref)
            if record.get('arxiv_id') == identity and record.get('status') in ('ok', 'empty'):
                references = str(ref)
        for manifest in sorted(paper.glob('versions/v*/manifest.json')):
            record = read(manifest)
            v = require_version(record['version'])
            if record.get('arxiv_id') != identity or manifest.parent.name != f'v{v}':
                raise ValueError(f'{identity}v{v} stage=inventory IDENTITY_MISMATCH: {manifest}')
            known.add(v)
            if v in archives or record.get('download_status') != 'ok' or not record.get('archive_path'):
                continue
            if record['archive_path'] != f'tmp/{key}/v{v}/source.archive':
                rejected.append(dict(arxiv_id=identity, version=v, stage='inventory',
                                     code='UNEXPECTED_ARCHIVE_PATH', manifest=str(manifest)))
                continue
            raw = safe_path(run, record['archive_path'])
            if not raw.is_file():
                continue
            size = raw.stat().st_size
            error = None
            if not size or size != record.get('archive_bytes'):
                error = 'ARCHIVE_SIZE_MISMATCH'
            else:
                audit = manifest.parent / 'source_assets_audit.json'
                expected = read(audit).get('archive_sha256') if audit.is_file() else None
                if expected:
                    with raw.open('rb') as stream:
                        if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                            error = 'ARCHIVE_HASH_MISMATCH'
            if error:
                rejected.append(dict(arxiv_id=identity, version=v, stage='inventory', code=error, path=str(raw)))
            else:
                archives[v] = dict(path=str(raw), source_format=record['source_format'], bytes=size)
    if known and max(known) > latest:
        metadata = None
    return dict(arxiv_id=identity, metadata=metadata, references=references, archives=archives,
                missing_versions=sorted(known - archives.keys()), rejected=rejected)


def inventory(config, roots):
    ids = load_ids(config['ids_file'])
    owners = {}
    for owner in OWNERS.values():
        for identity in load_ids(CONFIGS / 'team' / f'ids_{owner}.txt'):
            if identity in owners:
                raise ValueError(f'{identity} version=unknown stage=inventory OVERLAPPING_ASSIGNMENT')
            owners[identity] = owner
    runs = cache_runs(roots)
    papers = [inspect_cache(require_arxiv_id(i), runs) for i in ids]
    return dict(ids=len(ids), by_member=dict(sorted(Counter(owners[i] for i in ids).items())),
                unknown_version_lists=[p['arxiv_id'] for p in papers if not p['metadata']],
                missing_archives=[(p['arxiv_id'], v) for p in papers for v in p['missing_versions']],
                provisional_v1_missing=[p['arxiv_id'] for p in papers if not p['metadata']
                                        and 1 not in p['archives'] and 1 not in p['missing_versions']],
                rejected_cache=[r for p in papers for r in p['rejected']],
                cached_archives=sum(len(p['archives']) for p in papers),
                s2_publications_to_fetch=sum(not p['references'] for p in papers), papers=papers)


def prepare_run(config, papers):
    """Seed only valid download state. Fresh extraction must generate preserve audits."""
    run = config['output_root'] / config['run_id']
    run.parent.mkdir(parents=True, exist_ok=True)
    if run.exists():
        raise FileExistsError(run)
    with TemporaryDirectory(dir=run.parent) as temp:
        stage = Path(temp) / 'run'
        stage.mkdir()
        for p in papers:
            identity = p['arxiv_id']
            key = paper_key(identity)
            paper = stage / 'papers' / key
            paper.mkdir(parents=True)
            for name in ('metadata', 'references'):
                if p[name]:
                    shutil.copyfile(p[name], paper / f'{name}.json')
            for v, asset in p['archives'].items():
                rel = f'tmp/{key}/v{v}/source.archive'
                raw = stage / rel
                raw.parent.mkdir(parents=True)
                shutil.copyfile(asset['path'], raw)
                if raw.stat().st_size != asset['bytes']:
                    raise ValueError(f'{identity}v{v} stage=prepare ARCHIVE_SIZE_CHANGED')
                manifest = make_manifest(identity, v)
                manifest.update(download_status='ok', archive_path=rel, archive_bytes=asset['bytes'],
                                source_format=asset['source_format'])
                write_json_atomic(paper / f'versions/v{v}/manifest.json', manifest)
        stage.rename(run)
    return run


def compare_entries(control, preserve):
    """Compare normalized entries within one version; keep concrete field evidence."""
    common = sorted(control.keys() & preserve.keys())
    changes = []
    for key in common:
        for field, before in sorted(control[key].items()):
            after = preserve[key].get(field)
            if before not in (None, '', []) and before != after:
                changes.append(dict(key=key, field=field, before=before, after=after))
    return dict(added=sorted(preserve.keys() - control.keys()), lost=sorted(control.keys() - preserve.keys()),
                changed_fields=changes)


def entries(doc):
    parsed, _ = extract_bibliography_detailed(doc)
    return {e.original_key: dict(e.fields, entry_type=e.entry_type) for e in parsed}


def gather(version, choice):
    return g.gather_source_with_root(version, choice['root_file']) if choice else g.gather_source(version)


def compare_without_bbl(version, doc, choice, audit):
    original = version.source_dir.parent / 'original_assets'
    if doc.status not in ('ok', 'partial') or not original.is_dir() or not audit.get('archive_scan_complete'):
        return None
    if any(Path(r['original']).name.startswith('.env') for r in audit['records']):
        raise ValueError('restricted asset in original_assets')
    full = entries(doc)
    if not any(r['original'].lower().endswith('.bbl') for r in audit['records']):
        return compare_entries(full, full)
    with TemporaryDirectory() as tmp:
        base = Path(tmp).resolve()
        shutil.copytree(original, base / 'original_assets',
                        ignore=lambda _, names: [n for n in names if n.lower().endswith('.bbl')])
        records, _ = convert_assets(base / 'original_assets', base / 'source')
        write_json_atomic(base / 'source_assets_audit.json', dict(policy=POLICY, arxiv_id=version.arxiv_id,
                          version=version.version, records=records))
        hint = original.parent / 'recovery_compilation.json'
        if hint.exists():
            shutil.copyfile(hint, base / hint.name)
        files = tuple(sorted(r['derived'] for r in records if 'derived' in r))
        stripped = gather(replace(version, source_dir=base / 'source', source_files=files), choice)
        if stripped.status not in ('ok', 'partial') or stripped.root_file != doc.root_file:
            return None
        return compare_entries(entries(stripped), full)


def extra_commands(text):
    """Literal listing and PGF table commands in a span outside gathering events."""
    i = 0
    while (i := text.find('\\', i)) >= 0:
        special = g.literal(text, i)
        if special:
            i = special[1]
            continue
        name, i = g.command(text, i)
        if name != 'lstinputlisting' and not name.startswith('pgfplotstable'):
            continue
        options = g.group(text, i, '[', ']')
        arg = g.group(text, options[1] if options else i)
        if arg:
            value, i = arg
            yield name, value


def reference_commands(text):
    pos = 0
    for start, end, kind, value in g.events(text):
        yield from extra_commands(text[pos:start])
        if kind in ('input', 'include', 'subfile', 'subfileinclude'):
            yield kind, value
        elif kind == 'conditional' and value is not None:
            yield from reference_commands(value)
        pos = end
    yield from extra_commands(text[pos:])


def filtered_references(text, origin, root, filtered):
    hits = []
    for command, raw in reference_commands(g.strip_comments(text)[0]):
        suffix = '.tex' if command in ('input', 'include', 'subfile', 'subfileinclude') else ''
        targets, _ = g._path_candidates(filtered, ('', posixpath.dirname(root), posixpath.dirname(origin)),
                                        raw.strip(), suffix)
        for path in sorted(targets):
            hits.append(dict(path=path, suffix=Path(path).suffix.lower(), command=command, origin=origin))
    return hits


def version_report(run, version, row, durations):
    directory = version.source_dir.parent
    choice = row['root_selection']
    doc = gather(version, choice)
    result = dict(arxiv_id=version.arxiv_id, version=version.version, status=row['status'],
                  partial_reasons=row['partial_reasons'], info_diagnostics=row['info_diagnostics'],
                  root_candidates=None, bbl_comparison=None, filtered_references=None,
                  m1_stage_seconds=durations or None)
    raw = None
    manifest = directory / 'manifest.json'
    if manifest.exists():
        path = read(manifest).get('archive_path')
        if path:
            raw = safe_path(run, path)
    sizes = {name: directory_size_bytes(directory / name) if (directory / name).is_dir() else None
             for name in ('original_assets', 'source')}
    sizes['archive'] = raw.stat().st_size if raw and raw.is_file() else None
    sizes['total'] = sum(sizes.values()) if None not in sizes.values() else None
    result['bytes'] = sizes
    if version.extract_status == 'empty':
        result['root_candidates'] = []
    if version.extract_status != 'ok':
        return result
    context = g.Context(version)
    tex = {n for n in context.names if n.lower().endswith('.tex')}
    originals = g._load_document_assets(context)
    eligible = tex if originals is None else tex & originals
    # Count document roots before compilation metadata or dependency graph selection.
    roots = [n for n in sorted(eligible) if g.root_count(context.clean(n))]
    result['root_candidates'] = roots or [n for n in sorted(tex - eligible) if g.root_count(context.clean(n))]
    audit_path = directory / 'source_assets_audit.json'
    if not audit_path.is_file():
        return result
    audit = read(audit_path)
    result['bbl_comparison'] = compare_without_bbl(version, doc, choice, audit)
    if doc.root_file and audit.get('archive_scan_complete'):
        filtered = {r['path'] for r in audit['inventory'] if r['disposition'] == 'filtered'}
        origins = {r['derived']: r['original'] for r in audit['records'] if 'derived' in r}
        hits = []
        for derived in doc.included_files:
            origin = origins[derived]
            text, _ = text_bytes(safe_path(directory / 'original_assets', origin).read_bytes())
            hits.extend(filtered_references(text, origin, origins[doc.root_file], filtered))
        result['filtered_references'] = hits
    return result


def summarize(rows):
    roots = [r for r in rows if r['root_candidates'] is not None]
    multi = [r for r in roots if len(r['root_candidates']) > 1]
    compared = [r['bbl_comparison'] for r in rows if r['bbl_comparison'] is not None]
    regressions = sum(bool(c['lost'] or c['changed_fields']) for c in compared)
    refs = [hit for r in rows for hit in r['filtered_references'] or []]
    disk = {}
    for name in ('archive', 'original_assets', 'source', 'total'):
        sizes = [r['bytes'][name] for r in rows if r['bytes'][name] is not None]
        disk[name] = dict(bytes=sum(sizes) if sizes else None, versions_measured=len(sizes))
    return dict(versions=len(rows), storage=disk, multi_root=ratio(len(multi), len(roots), 'versions with a measured root scan'),
                multiple_roots=[(r['arxiv_id'], r['version'], r['root_candidates']) for r in multi],
                partial_reasons=dict(sorted(Counter(code for r in rows for code in set(r['partial_reasons'])).items())),
                info_diagnostics=dict(sorted(Counter(code for r in rows for code in set(r['info_diagnostics'])).items())),
                bbl_gains=ratio(sum(bool(c['added']) for c in compared), len(compared),
                                'versions comparable with and without original BBL inputs'),
                versions_with_key_loss_or_field_change=regressions,
                filtered_reference_hits_by_suffix=dict(Counter(h['suffix'] for h in refs)),
                filtered_reference_versions_examined=sum(r['filtered_references'] is not None for r in rows))


def report(run, m2):
    processing = read(m2 / 'processing_report.json')
    audits = audit_run(run)
    issues = [dict(**err, stage='audit', code='INVALID_PUBLICATION_METADATA', version=None)
              for err in audits['publication_errors']]
    rows = []
    durations = defaultdict(Counter)
    metrics = run / 'metrics_events.jsonl'
    if metrics.is_file():
        with metrics.open(encoding='utf-8') as handle:
            for line in handle:
                event = json.loads(line)
                if event['event'] == 'finished' and event.get('version') is not None:
                    durations[event['arxiv_id'], event['version']][event['stage']] += event['duration_s']
    seen = set()
    for pub in processing['publications']:
        identity = require_arxiv_id(pub['arxiv_id'])
        if identity in seen:
            raise ValueError(f'{identity} version=unknown stage=report DUPLICATE_PUBLICATION')
        seen.add(identity)
        if 'version_status' not in pub:
            issues.append(dict(arxiv_id=identity, version=None, stage='publication',
                               code=pub.get('error_type', 'PUBLICATION_FAILED'), detail=pub.get('error')))
            continue
        versions = {v.version: v for v in load_versions(run, identity)}
        status = pub['version_status']
        if sorted(versions) != sorted(r['version'] for r in status) or any(r['arxiv_id'] != identity for r in status):
            raise ValueError(f'{identity} version=unknown stage=report RUN_VERSION_MISMATCH')
        for r in status:
            v = r['version']
            try:
                row = version_report(run, versions[v], r, durations[identity, v])
            except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
                issues.append(dict(arxiv_id=identity, version=v, stage='report',
                                   code='MEASUREMENT_FAILED', detail=str(exc)))
                row = dict(arxiv_id=identity, version=v, status=r['status'],
                           partial_reasons=r['partial_reasons'], info_diagnostics=r['info_diagnostics'],
                           root_candidates=None, bbl_comparison=None, filtered_references=None,
                           bytes=dict.fromkeys(('archive', 'original_assets', 'source', 'total')),
                           m1_stage_seconds=durations[identity, v] or None)
            rows.append(row)
    for identity in sorted({r['arxiv_id'] for r in audits['versions']} - seen):
        issues.append(dict(arxiv_id=identity, version=None, stage='report', code='MISSING_M2_PUBLICATION'))
    rows.sort(key=lambda r: (r['arxiv_id'], r['version']))
    benchmark = read(m2.parent / f'benchmark_{m2.name}.json')
    if benchmark['run_dir_name'] != run.name or benchmark['id_list']['sha256'] != id_list_digest(list(seen)):
        raise ValueError('publication=batch version=all stage=report BENCHMARK_INPUT_MISMATCH')
    summary = summarize(rows)
    summary['m2_seconds_per_recorded_version'] = ratio(benchmark['end_to_end_wall_seconds'], len(rows),
        'all M2 version status rows; numerator is measured end-to-end batch seconds')
    return dict(summary=summary, versions=rows, issues=issues,
                table_validation=read(m2 / 'tables/qa_report.json')['validation']['passed'])


def markdown(data):
    s = data['summary']
    lines = ['# Pilot report', '', f"Versions in M2 status rows: {s['versions']}.",
             f"Table validation: {data['table_validation']}. Publication issues: {len(data['issues'])}."]
    for label in ('multi_root', 'bbl_gains', 'm2_seconds_per_recorded_version'):
        r = s[label]
        lines.append(f"- {label}: {r['numerator']}/{r['denominator']} ({r['denominator_definition']}).")
    for key in ('storage', 'partial_reasons', 'info_diagnostics', 'versions_with_key_loss_or_field_change',
                'filtered_reference_hits_by_suffix', 'filtered_reference_versions_examined'):
        lines.append(f"- {key}: {json.dumps(s[key], sort_keys=True)}")
    lines += ['', '## Version evidence', '',
              'Sizes are logical bytes. Times are recorded M1 version-stage seconds. null means unmeasured.', '']
    for r in data['versions']:
        lines += [f"### {r['arxiv_id']}v{r['version']} ({r['status']})", '']
        for k in ('root_candidates', 'partial_reasons', 'info_diagnostics', 'bbl_comparison',
                  'filtered_references', 'm1_stage_seconds', 'bytes'):
            lines.append(f"- {k}: `{json.dumps(r[k], ensure_ascii=False, sort_keys=True)}`")
        lines.append('')
    if data['issues']:
        lines += ['## Issues', '', '```json', json.dumps(data['issues'], indent=2), '```']
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('dry-run', 'prepare'):
        sub = commands.add_parser(name)
        sub.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
        sub.add_argument('--cache-root', type=Path, action='append', required=True)
    sub = commands.add_parser('report')
    sub.add_argument('--run', type=Path, required=True)
    sub.add_argument('--m2', type=Path, required=True)
    sub.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.command != 'report':
        config = load_config(args.config)
        inv = inventory(config, args.cache_root)
        papers = inv.pop('papers')
        print(json.dumps(inv, indent=2))
        if args.command == 'prepare':
            print(prepare_run(config, papers))
        return
    output = args.output.resolve()
    if output.exists() or any(output.is_relative_to(p.resolve()) for p in (args.run, args.m2)):
        raise ValueError('report output must be new and outside both inputs')
    data = report(args.run, args.m2)
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as tmp:
        stage = Path(tmp) / 'report'
        stage.mkdir()
        write_json_atomic(stage / 'report.json', data)
        (stage / 'report.md').write_text(markdown(data), encoding='utf-8')
        stage.rename(output)
    print(json.dumps(data['summary'], sort_keys=True))


if __name__ == '__main__':
    main()
