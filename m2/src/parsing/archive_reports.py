"""Incremental read-only M1 archive intake; persistent M1/M2 reports, not a corpus.

ZIP uses its central directory; TAR is streamed one publication at a time.
Completed source snapshots are immutable and content-addressed. Reports can be
rebuilt after the input archive is removed. No crawler/parser/API is launched.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import gzip
import hashlib
import fnmatch
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import struct
import tarfile
import time
import uuid
import zipfile

from . import profiling as P

SCHEMA = 1
MAX_JSON = 16 * 2**20
MAX_PAPER_PREFIX = 32 * 2**20
MAX_SAMPLE_BYTES = 4 * 2**20
SAMPLE_LIMIT = 2
PAPER = re.compile(r'(?:^|/)(\d{4}[-.]\d{4,5}|[A-Za-z][A-Za-z0-9.\-]*__\d{7})/(.+)$')
VERSION = re.compile(r'^versions/v([1-9]\d*)/(.+)$')


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    readable = path.name in {'M1_REPORT.json', 'M2_PROFILING.json', 'M2_EDGE_CASES_SUMMARY.json', 'progress.json', 'registry.json'}
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2 if readable else None) + '\n', encoding='utf-8')
    for attempt in range(6):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.1 * 2**attempt)


def sha_file(path, on_progress=None):
    digest = hashlib.sha256()
    total, notice = 0, 2**30
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(2**20), b''):
            digest.update(block)
            total += len(block)
            if on_progress is not None and total >= notice:
                on_progress(total)
                notice = total + 2**30
    return digest.hexdigest()


def group_path(name):
    p = PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts or '\\' in name:
        raise ValueError(f'unsafe archive member: {name}')
    match = PAPER.search(name)
    if not match:
        return None
    prefix = PurePosixPath(name[:match.start(1)]).parts
    if 'audit' in prefix or (prefix and prefix[-1] == 'tmp'):
        return None
    key, relative = match.groups()
    return key.replace('__', '/').replace('-', '.', 1) if '__' not in key else key.replace('__', '/'), relative


class StoredZipWindow(io.RawIOBase):
    """Seekable bounded view of a stored inner ZIP; no giant RAM/disk extraction."""
    def __init__(self, path, info):
        super().__init__()
        if info.compress_type != zipfile.ZIP_STORED or info.flag_bits & 1:
            raise ValueError('Nested batch must be unencrypted ZIP_STORED in outer archive')
        self.handle = Path(path).open('rb')
        self.handle.seek(info.header_offset)
        header = self.handle.read(30)
        try:
            values = struct.unpack('<4s5H3I2H', header)
            if values[0] != b'PK\x03\x04' or values[3] != zipfile.ZIP_STORED:
                raise ValueError('Invalid stored ZIP local header')
            self.start = info.header_offset + 30 + values[-2] + values[-1]
            self.size, self.position = info.file_size, 0
            if info.compress_size != info.file_size or self.start + self.size > Path(path).stat().st_size:
                raise ValueError('Invalid nested ZIP extent')
            if info._end_offset is not None and self.start + self.size > info._end_offset:
                raise ValueError('Overlapping nested ZIP extent')
        except BaseException:
            self.handle.close()
            raise

    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self.position
    def seek(self, offset, whence=0):
        position = offset if whence == 0 else self.position + offset if whence == 1 else self.size + offset if whence == 2 else -1
        if position < 0: raise ValueError('Invalid seek in bounded ZIP view')
        self.position = position
        return position

    def read(self, size=-1):
        remaining = max(0, self.size - self.position)
        size = remaining if size is None or size < 0 else min(size, remaining)
        self.handle.seek(self.start + self.position)
        data = self.handle.read(size)
        self.position += len(data)
        return data

    def close(self):
        if hasattr(self, 'handle'): self.handle.close()
        super().close()


def read_prefix(handle, size, limit):
    digest, prefix, count = hashlib.sha256(), bytearray(), 0
    while True:
        block = handle.read(2**20)
        if not block:
            break
        digest.update(block)
        count += len(block)
        if len(prefix) < limit:
            prefix.extend(block[:limit - len(prefix)])
    if count != size:
        raise ValueError('archive member size differs from bytes read')
    return bytes(prefix), digest.hexdigest()


class Intake:
    def __init__(self, archive, member, expected, building, progress, selection=None):
        self.archive, self.member, self.expected = archive, member, set(expected)
        self.building, self.progress = building, progress
        self.inventory, self.audit, self.tags = Counter(), Counter(), Counter()
        self.audit_seen, self.seen_papers = set(), set()
        self.samples, self.sample_members = [], set()
        self.paper_rows, self.version_rows, self.edge_rows = [], [], []
        self.edge_zip = zipfile.ZipFile(building / 'edge_samples.zip', 'x', zipfile.ZIP_DEFLATED)
        self.event_log = gzip.open(building / 'audit_records.jsonl.gz', 'wt', encoding='utf-8')
        self.control_reports = []
        self.selection = selection
        self.nested_inventory = []
        self.context = None
        self.source_reports = zipfile.ZipFile(building / 'source_reports.zip', 'x', zipfile.ZIP_DEFLATED)

    def audit_file(self, name, handle):
        self.inventory['audit_jsonl_files'] += 1
        for line in handle:
            if not line.strip():
                continue
            self.audit['raw_records'] += 1
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                self.audit['invalid_json_lines'] += 1
                continue
            self.event_log.write(encode({'archive_member': (self.context + '::' if self.context else '') + name, 'record': record}) + '\n')
            digest = hashlib.sha256(encode(record).encode()).digest()
            if digest in self.audit_seen:
                self.audit['duplicate_serialized_records'] += 1
                continue
            self.audit_seen.add(digest)
            stage = str(record.get('stage'))
            if record.get('error_code') or record.get('status') == 'failed':
                key = encode([stage, record.get('error_code'), record.get('http_status')])
                self.audit['error_record:' + key] += 1
            if record.get('http_status') == 429 or record.get('status_code') == 429:
                self.audit['records_with_explicit_http_429'] += 1

    def consume(self, files, name, size, handle):
        self.inventory['file_entries'] += 1
        self.inventory['uncompressed_bytes'] += size
        self.inventory['extension:' + PurePosixPath(name).suffix.lower()] += 1
        if name.endswith(('events.jsonl', 'metrics_events.jsonl')):
            self.audit_file(name, handle)
            return
        location = group_path(name)
        if location is None:
            if name.endswith(('report.json', 'summary.json', 'run.json', 'scope.json', 'provenance.json')) and size <= MAX_JSON:
                data = handle.read()
                self.control_reports.append({'archive_member': name,
                    'sha256': hashlib.sha256(data).hexdigest(), 'bytes': size})
            return
        _, relative = location
        source = VERSION.match(relative)
        is_source = bool(source and source[2].startswith('source/') and PurePosixPath(relative).suffix.lower() in {'.tex', '.bib', '.bbl'})
        wanted_json = relative in {'metadata.json', 'references.json', 'validation.json', 'provenance.json'} or bool(source and source[2] == 'manifest.json')
        if not is_source and not wanted_json:
            return
        if relative in files:
            raise ValueError(f'duplicate publication artifact: {name}')
        used = sum(len(f['data']) for f in files.values())
        limit = MAX_JSON if wanted_json else min(P.MAX_READ_BYTES, max(0, MAX_PAPER_PREFIX - used))
        data, digest = read_prefix(handle, size, limit)
        files[relative] = {'data': data, 'size': size, 'sha256': digest,
                           'archive_member': (self.context + '::' if self.context else '') + name, 'truncated': len(data) < size}

    @staticmethod
    def json_record(files, name):
        item = files.get(name)
        if item is None or item['truncated']:
            return None
        try:
            return json.loads(item['data'])
        except (ValueError, UnicodeError):
            return None

    def finish_paper(self, identity, files):
        if identity in self.seen_papers:
            raise ValueError(f'non-contiguous or duplicated paper {identity}; source not registered')
        self.seen_papers.add(identity)
        meta = self.json_record(files, 'metadata.json')
        validation = self.json_record(files, 'validation.json')
        references = self.json_record(files, 'references.json')
        meta = meta if isinstance(meta, dict) else None
        validation = validation if isinstance(validation, dict) else None
        observed = {int(m[1]) for name in files if (m := VERSION.match(name))}
        declared_raw = (meta or {}).get('versions', [])
        declared = {v for v in declared_raw if type(v) is int and v > 0} if isinstance(declared_raw, list) else set()
        versions = sorted(observed | declared)
        identity_ok = isinstance(meta, dict) and meta.get('arxiv_id') == identity
        refs = references.get('references') if isinstance(references, dict) else references if isinstance(references, list) else None
        rows = refs if isinstance(refs, list) else []
        issue_counts = Counter()
        issue_examples = []
        for issue in (validation or {}).get('issues', []):
            if not isinstance(issue, dict):
                issue_counts['invalid_issue'] += 1
                continue
            issue_counts[encode([issue.get('severity'), issue.get('code')])] += 1
            if issue.get('severity') == 'error' and len(issue_examples) < 10:
                issue_examples.append(issue)
        paper = {'arxiv_id': identity, 'status': 'ok', 'observed': True,
                 'metadata_present': isinstance(meta, dict), 'metadata_identity_matches': identity_ok,
                 'metadata_fields': sorted(meta or {}),
                 'metadata_status': (meta or {}).get('status'), 'metadata_error_code': (meta or {}).get('error_code'),
                 'title': (meta or {}).get('title'), 'n_versions': len(versions), 'versions': versions,
                 'version_mismatch': observed != declared, 'references_json_present': 'references.json' in files,
                 'validation_status': (validation or {}).get('status', 'unknown'),
                 'validation_issue_counts': dict(issue_counts), 'validation_error_examples': issue_examples,
                 'references_status': references.get('status', 'unknown') if isinstance(references, dict) else 'unknown',
                 'reference_fields': sorted({k for r in rows if isinstance(r, dict) for k in r}),
                 'references_error_code': references.get('error_code') if isinstance(references, dict) else None,
                 'reference_count': len(rows) if refs is not None else None,
                 'reference_dates_present': sum(isinstance(r, dict) and bool(r.get('ref_submission_date')) for r in rows),
                 'reference_arxiv_ids_present': sum(isinstance(r, dict) and bool(r.get('ref_arxiv_id')) for r in rows),
                 'reference_s2_ids_present': sum(isinstance(r, dict) and bool(r.get('ref_s2_id')) for r in rows),
                 'provenance': self.json_record(files, 'provenance.json'),
                 'truncated_json': [n for n, f in files.items() if n.endswith('.json') and f['truncated']]}
        self.paper_rows.append(paper)
        for version in versions:
            prefix = f'versions/v{version}/'
            manifest = self.json_record(files, prefix + 'manifest.json')
            manifest = manifest if isinstance(manifest, dict) else None
            def reader(relative):
                p = PurePosixPath(relative) if isinstance(relative, str) else PurePosixPath('..')
                if p.is_absolute() or '..' in p.parts or '\\' in str(p):
                    return None
                item = files.get(prefix + 'source/' + str(p))
                return (item['data'].decode('utf-8', errors='replace'), item['size']) if item is not None else None
            rec = P.profile_version_data(identity, version, manifest, reader)
            rec['source_artifacts'] = {n[len(prefix + 'source/'):]: {k: f[k] for k in ('size', 'sha256', 'truncated', 'archive_member')}
                                       for n, f in files.items() if n.startswith(prefix + 'source/')}
            rec['analysis_truncated'] = any(f['truncated'] for n, f in files.items() if n.startswith(prefix + 'source/'))
            rec['manifest_sha256'] = files.get(prefix + 'manifest.json', {}).get('sha256')
            rec['manifest_fields'] = sorted(manifest or {})
            rec['manifest_identity_matches'] = (manifest.get('arxiv_id') == identity and manifest.get('version') == version) if manifest else None
            rec['custom_if_macros'] = sorted({m[1] for name, item in files.items()
                if name.startswith(prefix + 'source/') and name.endswith('.tex')
                for m in re.finditer(r'\\(?:newcommand|renewcommand|providecommand|def)\s*\{?\\(if[A-Za-z@]+)',
                                     item['data'].decode('utf-8', errors='replace'))})
            rec['raw_bib_brace_count_mismatch'] = [name[len(prefix + 'source/'):] for name, item in files.items()
                if name.startswith(prefix + 'source/') and name.endswith('.bib') and not item['truncated']
                and item['data'].count(b'{') != item['data'].count(b'}')]
            rec['tags'] = self.edge_tags(paper, rec)
            self.version_rows.append(rec)
            for tag in rec['tags']:
                self.tags[tag] += 1
            if rec['tags']:
                edge = {'arxiv_id': identity, 'version': version, 'tags': rec['tags'],
                        'root_candidates': rec['n_root_candidates'], 'missing_includes': rec['missing_includes'],
                        'missing_bib_files': rec['missing_bib_files'], 'analysis_truncated': rec['analysis_truncated'],
                        'source_artifacts': rec['source_artifacts'], 'manifest': manifest}
                self.edge_rows.append(edge)
                eligible = [tag for tag in rec['tags'] if sum(tag in s['tags'] for s in self.samples) < SAMPLE_LIMIT]
                if eligible:
                    selected = {n: f for n, f in files.items() if n == 'metadata.json' or n.startswith(prefix)}
                    total = sum(f['size'] for f in selected.values())
                    complete = all(not f['truncated'] for f in selected.values()) and total <= MAX_SAMPLE_BYTES
                    sample = {'arxiv_id': identity, 'version': version, 'tags': rec['tags'],
                              'complete_retained_artifacts': complete, 'bytes': total,
                              'scope': 'selected version only, not necessarily every version in metadata'}
                    if complete:
                        for name, f in selected.items():
                            target = 'papers/' + P.paper_key(identity) + '/' + name
                            if target not in self.sample_members:
                                self.edge_zip.writestr(target, f['data']);self.sample_members.add(target)
                        sample['archive_prefix'] = 'papers/' + P.paper_key(identity) + '/' + prefix
                    else:
                        sample['reason'] = 'source prefix truncated or representative exceeds 4 MiB budget'
                    self.samples.append(sample)
        if len(self.paper_rows) % 100 == 0:
            self.update_progress(identity)

    @staticmethod
    def edge_tags(paper, rec):
        tags = []
        if paper['n_versions'] > 1: tags.append('multi_version')
        if rec['has_tex']:
            tags.append('multi_file' if rec['multi_file'] else 'single_file')
            if not rec['analysis_truncated'] and rec['n_root_candidates'] != 1:
                tags.append('multiple_roots' if rec['n_root_candidates'] > 1 else 'no_root_candidate')
        if rec['category'] != 'has_tex': tags.append(rec['category'])
        if rec['max_include_depth'] >= 2: tags.append('nested_include')
        if rec['include_cycle']: tags.append('include_cycle_suspected')
        if rec['missing_includes']: tags.append('missing_include_suspected')
        if rec['missing_bib_files']: tags.append('missing_bibliography_suspected')
        if rec['n_bibitem']: tags.append('inline_bibitem')
        if rec['n_bbl']: tags.append('bbl')
        if rec['listed_files_missing']: tags.append('listed_source_missing_or_unsafe')
        if rec['analysis_truncated']: tags.append('bounded_analysis_truncated')
        if rec['custom_if_macros']: tags.append('custom_if_macro')
        if rec['raw_bib_brace_count_mismatch']: tags.append('raw_bib_brace_count_mismatch')
        if not paper['metadata_identity_matches']: tags.append('metadata_identity_missing_or_mismatched')
        if rec['manifest_identity_matches'] is False: tags.append('manifest_identity_mismatch')
        return tags

    def update_progress(self, identity=None):
        state = {'state': 'scanning', 'member': self.member, 'archive': str(self.archive),
                 'papers_scanned': len(self.paper_rows), 'versions_scanned': len(self.version_rows),
                 'last_arxiv_id': identity, 'inventory': dict(self.inventory), 'nested_batch': self.context}
        write_json(self.progress, state)
        print(f"SCAN {self.member}: papers={len(self.paper_rows)} versions={len(self.version_rows)} last={identity}", flush=True)

    def scan_zip(self, z):
        grouped = defaultdict(list)
        others = []
        for info in z.infolist():
            if info.is_dir(): continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError(f'symlink in archive: {info.filename}')
            loc = group_path(info.filename)
            (grouped[loc[0]] if loc else others).append(info)
        for info in others:
            with z.open(info) as handle: self.consume({}, info.filename, info.file_size, handle)
        for identity, infos in sorted(grouped.items()):
            files = {}
            for info in infos:
                with z.open(info) as handle: self.consume(files, info.filename, info.file_size, handle)
            self.finish_paper(identity, files)

    def scan_nested(self, outer):
        directory = PurePosixPath(self.selection['directory'])
        if directory.is_absolute() or '..' in directory.parts:
            raise ValueError('Nested batch directory must be relative and safe')
        selected = [i for i in outer.infolist() if not i.is_dir()
                    and PurePosixPath(i.filename).parent == directory
                    and fnmatch.fnmatchcase(PurePosixPath(i.filename).name, self.selection['pattern'])]
        if not selected: raise ValueError('No nested batch ZIPs match explicit selection')
        ignored = [i.filename for i in outer.infolist() if i.filename.endswith('.zip') and i not in selected]
        self.nested_inventory = {'selected': [], 'excluded_zip_members': ignored}
        for info in outer.infolist():
            path = PurePosixPath(info.filename)
            # Preserve small upstream audit/date-update reports, not old corpus backups.
            if not info.is_dir() and info.file_size <= MAX_JSON and (
                'FINAL_AUDIT' in path.parts or path.name == 'update_report.json'):
                group_path(info.filename)  # validate path even though this is not a paper
                data = outer.read(info)
                self.source_reports.writestr(info.filename, data)
                self.control_reports.append({'archive_member': info.filename, 'sha256': hashlib.sha256(data).hexdigest(),
                                             'bytes': len(data), 'retained_in': 'source_reports.zip'})
        for number, info in enumerate(sorted(selected, key=lambda i: i.filename), 1):
            self.context = info.filename
            print(f'BATCH {number}/{len(selected)} {info.filename}', flush=True)
            with StoredZipWindow(self.archive, info) as view, zipfile.ZipFile(view) as inner:
                before = len(self.paper_rows)
                self.scan_zip(inner)
                self.nested_inventory['selected'].append({'archive_member': info.filename,
                    'bytes': info.file_size, 'declared_outer_crc32': f'{info.CRC:08x}',
                    'publications': len(self.paper_rows) - before, 'inner_file_entries': len(inner.infolist())})
        self.context = None

    def scan(self):
        if zipfile.is_zipfile(self.archive):
            with zipfile.ZipFile(self.archive) as z:
                if self.selection: self.scan_nested(z)
                else: self.scan_zip(z)
        else:
            if self.selection: raise ValueError('Nested ZIP selection requires an outer ZIP')
            current, files = None, {}
            with tarfile.open(self.archive, 'r|*') as archive:
                for info in archive:
                    if info.issym() or info.islnk(): raise ValueError(f'link in archive: {info.name}')
                    if not info.isfile(): continue
                    loc = group_path(info.name)
                    identity = loc[0] if loc else None
                    if identity is not None and identity != current:
                        if current is not None: self.finish_paper(current, files)
                        current, files = identity, {}
                    with archive.extractfile(info) as handle:
                        self.consume(files if loc else {}, info.name, info.size, handle)
                if current is not None: self.finish_paper(current, files)
        for identity in sorted(self.expected - self.seen_papers):
            self.paper_rows.append({'arxiv_id': identity, 'status': 'missing_paper_dir',
                                    'observed': False, 'validation_status': 'paper_missing'})
        for name, rows in [('publications.jsonl', self.paper_rows), ('versions.jsonl', self.version_rows), ('edge_cases.jsonl', self.edge_rows)]:
            with (self.building / name).open('x', encoding='utf-8') as handle:
                for row in rows: handle.write(encode(row) + '\n')
        self.edge_zip.close();self.event_log.close();self.source_reports.close()
        write_json(self.building / 'samples.json', self.samples)
        write_json(self.building / 'inventory.json', {'entries': dict(self.inventory), 'audit_counters': dict(self.audit),
                    'distinct_serialized_audit_records': len(self.audit_seen), 'control_report_inventory': self.control_reports,
                    'nested_zip_selection': self.nested_inventory,
                    'note': 'Audit counts are observed records, not inferred request attempts. No audit files means unknown, not zero failures.'})
        write_json(self.building / 'm2.json', P.summarize(self.paper_rows, self.version_rows))
        return self.paper_rows, self.version_rows


def rebuild(output, registry):
    papers, versions, edge_cases, per_member = [], [], [], {}
    for entry in registry['sources']:
        base = output / entry['snapshot']
        read = lambda name: [json.loads(line) for line in (base / name).read_text(encoding='utf-8').splitlines() if line]
        ps, vs, es = read('publications.jsonl'), read('versions.jsonl'), read('edge_cases.jsonl')
        for rows in (ps, vs, es):
            for row in rows: row.update(member=entry['member'], source_sha256=entry['sha256'])
        observed = [p for p in ps if p.get('observed')]
        inventory = json.loads((base / 'inventory.json').read_text(encoding='utf-8'))
        path_example = next((a['archive_member'].split('/versions/')[0] for v in vs
                             for a in v['source_artifacts'].values()), None)
        version_groups = defaultdict(list)
        for v in vs: version_groups[v['arxiv_id']].append(v)
        available = [p['arxiv_id'] for p in observed if p.get('metadata_identity_matches')
                     and not p.get('version_mismatch') and version_groups[p['arxiv_id']]
                     and all(v['category'] in {'has_tex', 'no_tex'} and not v['listed_files_missing']
                             and v.get('manifest_identity_matches') is True for v in version_groups[p['arxiv_id']])]
        empty_conflicts = []
        for paper in observed:
            errors = {json.loads(key)[1] for key in paper.get('validation_issue_counts', {})
                      if key.startswith('[') and json.loads(key)[0] == 'error'}
            rows = version_groups[paper['arxiv_id']]
            if paper['validation_status'] == 'failed' and errors == {'MISSING_SOURCE'} and rows and all(v['category'] == 'no_tex' for v in rows):
                empty_conflicts.append({'arxiv_id': paper['arxiv_id'], 'stored_validation': 'failed',
                    'current_manifest_categories': ['no_tex'] * len(rows),
                    'validation_errors': paper['validation_error_examples'],
                    'assessment': 'Stored missing-source error conflicts with current explicit no-TeX manifests; not proof of lost TeX or confirmed stale validation.'})
                for edge in es:
                    if edge['arxiv_id'] == paper['arxiv_id']:
                        edge['tags'] = sorted(set(edge['tags']) | {'stored_validation_no_tex_conflict'})
        summary = {'expected_publications': entry['expected_count'], 'observed_publications': len(observed),
                   'missing_expected_ids': sorted(set(entry['expected_ids']) - {p['arxiv_id'] for p in observed}),
                   'unexpected_ids': sorted({p['arxiv_id'] for p in observed} - set(entry['expected_ids'])),
                   'recorded_validation_status': dict(Counter(p['validation_status'] for p in ps)),
                   'metadata_status': dict(Counter(str(p.get('metadata_status')) for p in observed)),
                   'references_status': dict(Counter(str(p.get('references_status')) for p in observed)),
                   'references_total': sum(p.get('reference_count') or 0 for p in observed),
                   'references_with_date': sum(p.get('reference_dates_present', 0) for p in observed),
                   'references_with_arxiv_id': sum(p.get('reference_arxiv_ids_present', 0) for p in observed),
                   'references_nonempty_ok_publications': sum(p.get('references_status') == 'ok' and (p.get('reference_count') or 0) > 0 for p in observed),
                   'references_empty_ok_publications': sum(p.get('references_status') == 'ok' and p.get('reference_count') == 0 for p in observed),
                   'reference_counts_unknown_publications': sum(p.get('reference_count') is None for p in observed),
                   'metadata_error_codes': dict(Counter(str(p['metadata_error_code']) for p in observed if p.get('metadata_error_code'))),
                   'references_error_codes': dict(Counter(str(p['references_error_code']) for p in observed if p.get('references_error_code'))),
                   'validation_issue_counts': dict(sum((Counter(p['validation_issue_counts']) for p in observed), Counter())),
                   'recorded_failed_publication_ids': sorted(p['arxiv_id'] for p in observed if p['validation_status'] == 'failed'),
                   'version_download_status': dict(Counter(str(v['download_status']) for v in vs)),
                   'version_extract_status': dict(Counter(str(v['extract_status']) for v in vs)),
                   'version_error_codes': dict(Counter(str(v['error_code']) for v in vs if v['error_code'])),
                   'versions': len(vs), 'source': entry}
        audit_files = inventory['entries'].get('audit_jsonl_files', 0)
        summary.update(
            recorded_no_tex_validation_conflicts=empty_conflicts,
            artifact_available_for_m2_publications=len(available),
            artifact_availability_definition='Metadata/versions match; manifests identify the publication; declared TeX artifacts present or explicit no_tex. Not parsing success or M1 validation.',
            input_layout={'publication_path_example': path_example,
                          'papers_container': '/papers/' in path_example if path_example else None,
                          'note': ('No source-file path available for layout inference.' if path_example is None else
                                   'Flat corpus/<paper_key> layout needs mapping into canonical papers/ before using the M2 adapter.' if '/papers/' not in path_example else 'Canonical papers/<paper_key> container observed.')},
            audit={'availability': 'present' if audit_files else 'absent', 'jsonl_files': audit_files,
                   'observed_record_counts': inventory['audit_counters'] if audit_files else None,
                   'retained_records': entry['snapshot'] + '/audit_records.jsonl.gz',
                   'definition': 'Observed serialized records, not network attempt counts. Absent logs mean unknown.'})
        control = base / 'source_reports.zip'
        comparison = []
        if control.exists():
            with zipfile.ZipFile(control) as z:
                for name in z.namelist():
                    if not name.endswith('M1_FINAL_SUMMARY.json'): continue
                    claimed = json.loads(z.read(name))
                    values = {'total_papers': len(observed), 'ok_papers': summary['recorded_validation_status'].get('ok', 0),
                        'failed_papers': summary['recorded_validation_status'].get('failed', 0), 'total_versions': len(vs),
                        'download_ok_versions': summary['version_download_status'].get('ok', 0),
                        'download_failed_versions': summary['version_download_status'].get('failed', 0),
                        'extract_ok_versions': summary['version_extract_status'].get('ok', 0),
                        'extract_empty_versions': summary['version_extract_status'].get('empty', 0),
                        'extract_failed_versions': summary['version_extract_status'].get('failed', 0)}
                    declared_failures = {row['arxiv_id'] for row in claimed.get('failed_paper_details', [])}
                    actual_failures = set(summary['recorded_failed_publication_ids'])
                    comparison.append({'archive_member': name, 'declared_counts': {k: claimed[k] for k in values if k in claimed},
                        'observed_counts': values, 'count_mismatches': {k: {'claimed': claimed[k], 'observed': value} for k, value in values.items() if k in claimed and claimed[k] != value},
                        'recorded_failed_not_in_claimed_details': sorted(actual_failures - declared_failures) if 'failed_paper_details' in claimed else None,
                        'claimed_failed_not_recorded_failed': sorted(declared_failures - actual_failures) if 'failed_paper_details' in claimed else None})
        summary['source_report_comparisons'] = comparison
        evidence_dir = output / 'quality_evidence' / (entry['member'] + '_' + entry['sha256'][:16])
        recheck_path = evidence_dir / 'recheck.json'
        if recheck_path.exists():
            evidence = json.loads(recheck_path.read_text(encoding='utf-8'))
            evidence_zip = evidence_dir / evidence['evidence_zip']
            if evidence['source_sha256'] != entry['sha256'] or sha_file(evidence_zip) != evidence['evidence_sha256']:
                raise ValueError('Targeted validation evidence does not match registered source')
            summary['targeted_validation_recheck'] = {
                'scope': evidence['scope'], 'validator_sha256': evidence['validator_sha256'],
                'evidence_zip': evidence_zip.relative_to(output).as_posix(),
                'report': recheck_path.relative_to(output).as_posix(),
                'results': [{'arxiv_id': r['arxiv_id'], 'status': r['status'],
                             'errors': [i for i in r['issues'] if i['severity'] == 'error']} for r in evidence['results']],
                'note': 'Read-only validation on file copies. Empty source directory conventions affect M1 validation; not a full-run revalidation or automatic override.'}
        per_member[entry['member']] = summary
        papers.extend(ps);versions.extend(vs);edge_cases.extend(es)
    owners = defaultdict(set)
    for p in papers:
        if p.get('observed'): owners[p['arxiv_id']].add(p['member'])
    collisions = {i: sorted(m) for i, m in owners.items() if len(m) > 1}
    common = {'schema_version': SCHEMA, 'members_loaded': sorted(per_member),
              'members_pending': sorted({'24120027', '24120046', '24120058', '24120085'} - set(per_member)),
              'publication_overlap': collisions, 'sources': registry['sources'],
              'report_generator_sha256': sha_file(__file__),
              'retention': 'Reports rebuild from immutable snapshots without raw inputs. Snapshots are not a complete M2/M3 corpus; retain original data elsewhere.'}
    write_json(output / 'M1_REPORT.json', {**common, 'by_member': per_member,
        'observed_unique_publications': len(owners),
        'expected_unique_publications': len({i for e in registry['sources'] for i in e['expected_ids']}),
        'combined_observed_validation_status': dict(Counter(p['validation_status'] for p in papers if p.get('observed'))) if not collisions else None,
        'references_total': sum(s['references_total'] for s in per_member.values()) if not collisions else None,
        'references_with_date': sum(s['references_with_date'] for s in per_member.values()) if not collisions else None,
        'definitions': {'recorded_validation_status': 'Stored M1 validation.json; unknown is not failed or successful.',
                        'references_with_date': 'Nonempty ref_submission_date, as supplied. No new S2 lookup.',
                        'retry_statistics': 'Separate immutable snapshot inventory and audit_records.jsonl.gz; missing logs imply unknown.',
                        'time_and_resources': 'Not aggregated in this intake.'}})
    write_json(output / 'M2_PROFILING.json', {**common,
        'by_member': {e['member']: json.loads((output / e['snapshot'] / 'm2.json').read_text(encoding='utf-8')) for e in registry['sources']},
        'combined': P.summarize(papers, versions) if not collisions else None,
        'limits': ['Regex-based input profiling, not actual gathering/parsing success.',
                   'Up to 8 MB per source file and 32 MiB total prefixes per publication; truncation recorded.',
                   'Combined profile is withheld on cross-member publication overlaps.']})
    write_json(output / 'M2_EDGE_CASES.json', {**common, 'counts_by_tag': dict(Counter(t for e in edge_cases for t in e['tags'])),
        'counts_unit': 'Versions; tags overlap. multi_version counts versions belonging to a multi-version publication, not publication count.',
        'versions': edge_cases, 'samples_by_member': {e['member']: json.loads((output / e['snapshot'] / 'samples.json').read_text(encoding='utf-8')) for e in registry['sources']},
        'note': 'Findings are preliminary candidates. Complete bounded representative sources are in each snapshot edge_samples.zip; large/truncated samples are explicitly not self-contained.'})
    write_json(output / 'M2_EDGE_CASES_SUMMARY.json', {
        'schema_version': SCHEMA, 'members_loaded': common['members_loaded'], 'members_pending': common['members_pending'],
        'counts_unit': 'Version candidates; tags overlap. Not confirmed parsing failures.',
        'counts_by_tag': dict(Counter(t for e in edge_cases for t in e['tags'])),
        'candidate_versions': len(edge_cases),
        'representatives': {entry['member']: {
            'catalogued': len(samples := json.loads((output / entry['snapshot'] / 'samples.json').read_text(encoding='utf-8'))),
            'complete_retained_version_artifacts': sum(s['complete_retained_artifacts'] for s in samples),
            'archive': entry['snapshot'] + '/edge_samples.zip'} for entry in registry['sources']},
        'details': 'M2_EDGE_CASES.json', 'note': 'Keep snapshots. Representative ZIPs contain selected versions, not the full corpus.'})
    write_json(output / 'progress.json', {'state': 'complete', 'members_loaded': sorted(per_member),
                                         'observed_unique_publications': len(owners)})
    (output / 'KEEP_THIS_DIRECTORY.txt').write_text(
        'KEEP this accumulated directory and snapshots/. Do not delete them with m1-inputs.\n'
        'Raw downloads can be removed only after verifying intake and retaining originals on Drive/other storage.\n'
        'JSON and bounded edge samples do not replace full sources/references needed for M2/M3.\n'
        'Add later members with the same --output; identical source bytes are not counted again.\n'
        'Rebuild without raw archives: python -B -m m2.src.parsing.archive_reports --rebuild --output "' + str(output) + '"\n',
        encoding='utf-8')


def verify_snapshots(output, registry):
    for entry in registry['sources']:
        for name, checksum in entry['artifact_sha256'].items():
            if sha_file(output / entry['snapshot'] / name) != checksum:
                raise ValueError(f'Snapshot verification failed: {entry["member"]} {name}')


def _execute(archive, member, ids_file, output, selection=None):
    archive, output = Path(archive).resolve(strict=True), Path(output).resolve()
    if not archive.is_file(): raise ValueError('input must be one ZIP/TAR archive')
    output.mkdir(parents=True, exist_ok=True)
    registry_path = output / 'registry.json'
    registry = json.loads(registry_path.read_text(encoding='utf-8')) if registry_path.exists() else {'schema_version': SCHEMA, 'sources': []}
    verify_snapshots(output, registry)
    expected = P.read_ids(Path(ids_file))
    print(f'HASH {member}: {archive}', flush=True)
    initial = archive.stat()
    def hashing_progress(count):
        write_json(output / 'progress.json', {'state': 'hashing', 'member': member, 'bytes_hashed': count,
                                              'total_bytes': initial.st_size, 'members_loaded': [e['member'] for e in registry['sources']]})
        print(f'HASH {member}: {count / initial.st_size:.1%}', flush=True)
    hashing_progress(0)
    digest = sha_file(archive, on_progress=hashing_progress)
    for entry in registry['sources']:
        if entry['member'] == member:
            if entry['sha256'] != digest: raise ValueError('Member already has a different snapshot; review retry/consolidation precedence first.')
            if entry['expected_ids'] != expected: raise ValueError('Assigned ID list differs from registered snapshot.')
            if entry.get('selection') != selection: raise ValueError('Nested batch selection differs from registered source.')
            rebuild(output, registry)
            print(f'ALREADY REGISTERED {member}; rebuilt reports without adding counts', flush=True)
            return
        if entry['sha256'] == digest: raise ValueError('Same archive already attributed to another member')
    snapshots = output / 'snapshots';snapshots.mkdir(exist_ok=True)
    building = snapshots / ('.building-' + uuid.uuid4().hex);building.mkdir()
    intake = Intake(archive, member, expected, building, output / 'progress.json', selection=selection)
    try:
        intake.update_progress()
        intake.scan()
        final = archive.stat()
        if (initial.st_size, initial.st_mtime_ns) != (final.st_size, final.st_mtime_ns): raise ValueError('Input changed during scan')
        entry = {'member': member, 'archive_name': archive.name, 'archive_path_at_intake': str(archive),
                 'sha256': digest, 'bytes': initial.st_size, 'expected_ids': expected,
                 'expected_ids_sha256': sha_file(ids_file), 'expected_count': len(expected),
                 'snapshot': 'snapshots/' + member + '_' + digest[:16],
                 'scanner_sha256': sha_file(__file__), 'profiler_sha256': sha_file(P.__file__), 'status': 'complete'}
        if selection: entry['selection'] = selection
        entry['artifact_sha256'] = {p.name: sha_file(p) for p in building.iterdir() if p.is_file()}
        write_json(building / 'source.json', entry)
        target = output / entry['snapshot']
        if target.exists(): raise FileExistsError('Unregistered snapshot exists; manual review required')
        building.rename(target)
        registry['sources'].append(entry)
        write_json(registry_path, registry)
        rebuild(output, registry)
        # Read back every persistent artifact before declaring retention complete.
        verify_snapshots(output, registry)
        print(f'COMPLETE {member}: {target}', flush=True)
    except BaseException as error:
        intake.edge_zip.close();intake.event_log.close();intake.source_reports.close()
        write_json(output / 'progress.json', {'state': 'failed', 'member': member, 'error': str(error)})
        raise


def execute(archive, member, ids_file, output, *, rebuild_only=False, selection=None):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    lock = output / '.intake.lock'
    # Exclusive creation: never remove another process's/user's existing lock.
    with lock.open('x', encoding='utf-8') as handle:
        handle.write(str(os.getpid()));handle.flush()
        try:
            if rebuild_only:
                registry = json.loads((output / 'registry.json').read_text(encoding='utf-8'))
                verify_snapshots(output, registry)
                rebuild(output, registry)
                return
            return _execute(archive, member, ids_file, output, selection=selection)
        finally:
            handle.close()
            lock.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--member', choices=['24120027', '24120046', '24120058', '24120085'])
    parser.add_argument('--ids-file', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rebuild', action='store_true', help='Rebuild only from retained snapshots; raw archives are unnecessary')
    parser.add_argument('--nested-zip-dir', help='Explicit outer directory of current batch ZIPs; backups elsewhere are excluded')
    parser.add_argument('--nested-zip-pattern', default='*.zip')
    args = parser.parse_args(argv)
    if not args.rebuild and not (args.archive and args.member and args.ids_file):
        parser.error('--archive, --member and --ids-file are required unless --rebuild is used')
    selection = {'directory': args.nested_zip_dir, 'pattern': args.nested_zip_pattern} if args.nested_zip_dir else None
    execute(args.archive, args.member, args.ids_file, args.output, rebuild_only=args.rebuild, selection=selection)


if __name__ == '__main__':
    main()
