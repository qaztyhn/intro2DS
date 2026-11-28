"""Recover source assets into a NEW run; originals and derivations stay separate.

Usage: python -m m1.src.tools.recover_sources --plan plan.json --output NEW_DIR
Plan: [{"arxiv_id": "2507.01659", "version": 1, "archive": "...",
        "member_id": "24120058", "paper_dir": "..."}]
With --download, missing archives are fetched from the versioned arXiv source
endpoint, sequentially at >=3 second intervals. No existing corpus is changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import re
import shutil
import time
import urllib.request
import urllib.error
import http.client
from pathlib import Path, PurePosixPath

from m1.src.downloader.http import make_manifest
from m1.src.compilation import MAX_COMPILATION_BYTES, compilation_record, parse_compilation_readme
from m1.src.utils.http import Throttle, get_retry_delay
from m1.src.schema import require_arxiv_id, require_version
from m1.src.stages.extract import extract_source, _is_tex
from m1.src.stages.validation import validate_manifest
from m1.src.utils.common import paper_key
from m2.src.parsing.biblatex_bbl import biblatex_bbl_to_bibtex, is_biblatex_bbl, is_empty_bibliography
from m2.src.parsing.amsrefs_bbl import amsrefs_bbl_to_bibtex, is_amsrefs_bbl
from m2.src.parsing.gathering import events, strip_comments, root_count, _top_level_roots

RECOVERY_SUFFIXES = {'.tex', '.bib', '.bbl', '.bst', '.ltx', '.latex', '.sty', '.cls',
                     '.def', '.inc', '.tikz', '.pgf', '.pdf_tex', '.eps_tex', '.txt'}
DOWNLOAD_THROTTLE = Throttle(3.1)


def download_original_archive(url, destination, *, attempts=3):
    """Sequential, capped downloads. Failed payloads never become source input."""
    destination = Path(destination)
    if type(attempts) is not int or attempts < 1:
        raise ValueError('attempts must be a positive integer')
    if destination.exists():
        raise FileExistsError(destination)
    request = urllib.request.Request(url, headers={'User-Agent':
        'intro2DS-source-recovery/1.0 (academic project; https://github.com/qaztyhn/intro2DS)'})
    for attempt in range(attempts):
        partial = destination.with_name(destination.name + f'.partial-{attempt+1}')
        DOWNLOAD_THROTTLE.wait()
        try:
            size = 0
            with urllib.request.urlopen(request, timeout=90) as response, partial.open('xb') as dest:
                declared = response.headers.get('Content-Length')
                expected = int(declared) if declared and declared.isdecimal() else None
                if expected is not None and expected > 256 * 1024 * 1024:
                    raise ValueError('download exceeds 256 MiB')
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 256 * 1024 * 1024:
                        raise ValueError('download exceeds 256 MiB')
                    dest.write(chunk)
            if expected is not None and size != expected:
                raise urllib.error.URLError(f'truncated source response: expected {expected}, got {size}')
            if size == 0:
                raise ValueError('empty source response')
            if destination.exists():
                raise FileExistsError(destination)
            partial.replace(destination)
            return dict(attempts=attempt+1, bytes=size, url=url)
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as exc:
            status = getattr(exc, 'code', None)
            transient = status is None or status in {408, 429, 500, 502, 503, 504}
            if not transient or attempt + 1 == attempts:
                raise
            # Retry-After is capped by the shared crawler helper. Failed
            # .partial-N files stay separate from source.archive for audit.
            time.sleep(get_retry_delay(exc if isinstance(exc, urllib.error.HTTPError) else None,
                                       attempt, base_delay=3.1))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def text_bytes(data):
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16'), 'utf-16'
    if b'\0' in data:
        raise ValueError('binary/NUL content')
    try:
        return data.decode('utf-8-sig'), 'utf-8'
    except UnicodeDecodeError:
        return data.decode('latin-1'), 'latin-1'


def referenced_text_assets(original):
    """Find literal TXT inputs reachable from declared/unique compilation roots.

    No package execution or unknown-branch guessing. Unreferenced logs and
    download instructions do not become TeX merely because they are textual.
    """
    names={p.relative_to(original).as_posix() for p in original.rglob('*') if p.is_file()}
    cache={}
    def clean(name):
        if name not in cache:cache[name]=strip_comments(text_bytes((original/name).read_bytes())[0])[0]
        return cache[name]
    def resolve(origin,raw):
        raw=raw.strip()
        if not raw or any(c in raw for c in '\\#$\x00{}') or ':' in raw or raw.startswith('/'):
            return None
        candidates={posixpath.normpath(posixpath.join(base,raw)) for base in (posixpath.dirname(origin),'')}
        candidates={p for p in candidates if p!='..' and not p.startswith('../')}
        found=candidates&names
        if not found:found={p+'.tex' for p in candidates}&names
        return next(iter(found)) if len(found)==1 else None
    roots=[]
    readme=original/'00README.json'
    if readme.is_file():
        try:
            roots=[n for n in parse_compilation_readme(readme.read_bytes())['toplevels'] if n in names]
        except (ValueError,UnicodeError):pass
    if not roots:
        for n in sorted(names):
            if Path(n).suffix.lower() not in {'.tex','.ltx','.latex'}:continue
            try:
                if root_count(clean(n)):roots.append(n)
            except ValueError:continue
        if len(roots)>1:
            from types import SimpleNamespace
            roots=_top_level_roots(SimpleNamespace(names=names,clean=clean),roots)
        if len(roots)!=1:return set()
    referenced=set();ordinary=set();flags=set();values={};scopes=[];calls=0
    def visit(name,stack):
        nonlocal calls
        calls+=1
        if calls>10000 or len(stack)>=128 or name in stack:return
        try:walk(clean(name),name,stack+(name,))
        except (ValueError,UnicodeError):return
    def walk(text,origin,stack,depth=0):
        if depth>=128:return
        for _,_,kind,payload in events(text,ordinary,flags,values,scopes):
            if kind=='conditional' and payload is not None:walk(payload,origin,stack,depth+1)
            elif kind in {'input','include','subfile','subfileinclude'}:
                target=resolve(origin,payload)
                if target:
                    if target.lower().endswith('.txt'):referenced.add(target)
                    if Path(target).suffix.lower() in {'.tex','.ltx','.latex','.txt','.inc'}:
                        visit(target,stack)
    for name in roots:
        ordinary.clear();flags.clear();values.clear();scopes.clear();visit(name,())
    return referenced


def convert_assets(original, source):
    """Convert bibliographies/text assets without inventing missing metadata."""
    source.mkdir()
    records, warnings, mapping = [], [], {}
    active_txt = referenced_text_assets(original)
    has_document_source = any(p.is_file() and p.suffix.lower() in {'.tex', '.ltx', '.latex'}
                              for p in original.rglob('*'))
    for f in sorted(original.rglob('*')):
        if not f.is_file():
            continue
        name = f.relative_to(original).as_posix()
        data = f.read_bytes()
        suffix = f.suffix.lower()
        record = dict(original=name, original_sha256=sha(data), bytes=len(data))
        if name == '00README.json' or suffix == '.bst':
            record['conversion'] = ('compilation_metadata_sidecar_only' if name == '00README.json'
                                    else 'bibliography_style_source_only')
            records.append(record)
            continue
        try:
            text, encoding = text_bytes(data)
            record['encoding'] = encoding
            if suffix in {'.tex', '.bib'}:
                target, converted, kind = name, data, 'unchanged'
            elif suffix == '.bbl':
                if is_biblatex_bbl(text):
                    bib, issues = biblatex_bbl_to_bibtex(text)
                    warnings.extend(f'{name}: {w}' for w in issues)
                    if not bib.strip():
                        raise ValueError('no convertible Biber entries')
                    target, converted, kind = name + '.bib', bib.encode('utf-8'), 'biber_to_bib'
                elif is_amsrefs_bbl(text):
                    bib, issues = amsrefs_bbl_to_bibtex(text)
                    warnings.extend(f'{name}: {w}' for w in issues)
                    if not bib.strip():
                        raise ValueError('no convertible AMSRefs entries')
                    target, converted, kind = name + '.bib', bib.encode('utf-8'), 'amsrefs_to_bib'
                else:
                    clean, _ = strip_comments(text)
                    if not re.search(r'\\bibitem(?![A-Za-z@])', clean) and not is_empty_bibliography(text):
                        raise ValueError('unsupported BBL syntax; original retained')
                    # Preserve all citation text, including metadata the heuristic
                    # BibTeX converter cannot recover confidently.
                    target, converted, kind = name + '.tex', data, (
                        'empty_bibliography_to_tex' if is_empty_bibliography(text) else 'bibitems_to_tex')
            else:
                if suffix == '.txt' and re.search(r'(?m)^\s*@[A-Za-z]+\s*[{(]', text):
                    from m2.src.parsing.bibliography import parse_bib
                    entries, issues = parse_bib(text)
                    if not entries or issues:
                        raise ValueError('text bibliography is malformed; original retained')
                    target, converted, kind = name + '.bib', data, 'text_bibliography_to_bib'
                elif suffix == '.txt' and name not in active_txt and not _is_tex(data[:65536]):
                    raise ValueError('plain txt not identified as TeX source')
                else:
                    target, converted, kind = name + '.tex', data, 'text_asset_to_tex'
                if re.search(r'<\s*(?:!doctype\s+html|html)\b', text, re.I):
                    raise ValueError('HTML is not TeX source')
            if target.endswith('.tex') and not has_document_source:
                raise ValueError('support asset without document TeX; retained only in original_assets')
            if target != name and (original / target).exists():
                raise ValueError(f'conversion target collides with original: {target}')
            dest = source / target
            if dest.exists():
                raise ValueError(f'conversion target collision: {target}')
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(converted)
            record.update(derived=target, conversion=kind, derived_sha256=sha(converted))
            if suffix=='.txt' and name in active_txt:
                record['referenced_on_compilation_path']=True
            mapping[name] = target
            if kind == 'text_bibliography_to_bib' and name.lower().endswith('.bib.txt'):
                alias = name[:-4]
                if not (original/alias).exists():
                    mapping[alias] = target
                    record['resource_alias'] = alias
        except ValueError as exc:
            record['error'] = str(exc)
            warnings.append(f'{name}: {exc}')
        records.append(record)

    def resolve(origin, raw, suffix):
        raw = raw.strip()
        if not raw or any(c in raw for c in '\\#$\x00{}') or ':' in raw or raw.startswith('/'):
            return None
        exact = set()
        appended = set()
        for base in (posixpath.dirname(origin), ''):
            path = posixpath.normpath(posixpath.join(base, raw))
            if path.startswith('../') or path == '..':
                continue
            if path in mapping:
                exact.add(path)
            if path + suffix in mapping:
                appended.add(path + suffix)
        candidates = exact or appended
        if len(candidates) > 1 and all(n.lower().endswith('.bbl') for n in candidates):
            if len({(original/n).read_bytes() for n in candidates}) == 1:
                preferred = posixpath.normpath(posixpath.join(posixpath.dirname(origin), raw))
                return preferred if preferred in candidates else sorted(candidates)[0]
        return next(iter(candidates)) if len(candidates) == 1 else None

    # Rewrite only directives on paths understood by the gatherer. Unknown
    # conditions remain untouched and explicitly unresolved downstream.
    for record in records:
        target = record.get('derived', '')
        if not target.lower().endswith('.tex'):
            continue
        path = source / target
        raw = path.read_bytes()
        text, encoding = text_bytes(raw)
        clean, _ = strip_comments(text)
        patches = []
        for start, end, kind, payload in events(clean):
            if kind in {'input', 'include'}:
                old = resolve(record['original'], payload, '.tex')
                if old and mapping[old] != old:
                    name = posixpath.relpath(mapping[old], posixpath.dirname(target) or '.')
                    replacement = (r'\bibliography{' + name + '}' if name.endswith('.bib')
                                   else '\\' + kind + '{' + name + '}')
                    patches.append((start, end, replacement))
            elif kind in {'bibliography', 'addbibresource'}:
                names = payload.split(',')
                resolved = [resolve(record['original'], name, '.bib') for name in names]
                bbl_candidates = {str(PurePosixPath(record['original']).with_suffix('.bbl')),
                                  PurePosixPath(record['original']).stem + '.bbl'}
                bbl_found = sorted(n for n in bbl_candidates if n in mapping)
                if len(bbl_found) > 1:
                    if len({(original/n).read_bytes() for n in bbl_found}) == 1:
                        record['equivalent_bbl_resources'] = bbl_found
                        preferred = str(PurePosixPath(record['original']).with_suffix('.bbl'))
                        bbl_found = [preferred if preferred in bbl_found else bbl_found[0]]
                    else:
                        warnings.append(f'{record["original"]}: ambiguous root BBL: {bbl_found}')
                bbl = bbl_found[0] if len(bbl_found) == 1 else None
                supplement = None
                if bbl:
                    name = posixpath.relpath(mapping[bbl], posixpath.dirname(target) or '.')
                    supplement = (r'\bibliography{' + name + '}' if name.endswith('.bib')
                                  else r'\input{' + name + '}')
                if any(resolved):
                    replacements = [posixpath.relpath(mapping[old], posixpath.dirname(target) or '.')
                                    if old else name.strip() for name, old in zip(names, resolved)]
                    replacement = clean[start:end]
                    if any(old and mapping[old] != old for old in resolved):
                        if kind == 'addbibresource':
                            # Preserve options, e.g. location/label, exactly.
                            command_text = clean[start:end]
                            before = command_text.rfind('{')
                            replacement = command_text[:before+1] + ','.join(replacements) + '}'
                        else:
                            replacement = r'\bibliography{' + ','.join(replacements) + '}'
                    # Both resources may contain complementary metadata. The
                    # bibliography layer keeps .bib precedence and reports
                    # real conflicts instead of dropping either source here.
                    if supplement:
                        replacement += '\n' + supplement
                    if replacement != clean[start:end]:
                        patches.append((start, end, replacement))
                    continue
                # Root/jobname BBL fallback only when all declared bibliography
                # resources are absent. Never override an available .bib.
                if not supplement:
                    continue
                patches.append((start, end, supplement))
        if patches:
            for start, end, replacement in reversed(patches):
                clean = clean[:start] + replacement + clean[end:]
            output = clean.encode('utf-8')
            path.write_bytes(output)
            record.update(directives_rewritten=len(patches), derived_sha256=sha(output),
                          derived_encoding='utf-8')
    return records, warnings


def recover_version(item, output, *, download=False):
    identity = require_arxiv_id(item['arxiv_id'])
    version = require_version(item['version'])
    paper = output / 'papers' / paper_key(identity)
    directory = paper / 'versions' / f'v{version}'
    directory.mkdir(parents=True, exist_ok=False)
    original_archive = item.get('archive')
    archive = Path(original_archive) if original_archive else directory / 'source.archive'
    download_info = None
    if not archive.is_file():
        if not download:
            return dict(**item, status='archive_missing')
        url = f'https://export.arxiv.org/src/{identity}v{version}'
        # The loop enforces pacing; stream and limit bytes instead of loading
        # an arbitrarily large response into memory.
        archive = directory / 'source.archive'
        download_info = download_original_archive(url, archive)
    original = directory / 'original_assets'
    result = extract_source(archive, original, source_suffixes=RECOVERY_SUFFIXES,
                            source_names={'00README.json'})
    if result['extract_status'] == 'failed':
        return dict(arxiv_id=identity, version=version, member_id=item.get('member_id'),
                    status='extract_failed', extraction=result)
    records, warnings = convert_assets(original, directory / 'source')
    readme = original / '00README.json'
    if readme.is_file():
        try:
            with readme.open('rb') as handle:
                raw_readme = handle.read(MAX_COMPILATION_BYTES + 1)
            compilation = compilation_record(raw_readme, identity, version)
            write_json(directory/'recovery_compilation.json', compilation)
        except (UnicodeError, ValueError) as exc:
            warnings.append(f'00README.json: unusable compilation metadata: {exc}')
    files = sorted(p.relative_to(directory/'source').as_posix()
                   for p in (directory/'source').rglob('*') if p.is_file())
    manifest = make_manifest(identity, version)
    has_tex = any(n.lower().endswith('.tex') for n in files)
    manifest.update(download_status='ok', extract_status='ok' if has_tex else 'empty',
                    source_format=result['source_format'], source_files=files,
                    archive_bytes=archive.stat().st_size, unpacked_total_bytes=None,
                    kept_source_bytes=sum((directory/'source'/n).stat().st_size for n in files),
                    error_code=None if has_tex else 'NO_TEX_SOURCE')
    manifest['recovery_bibliography_warnings'] = [w for w in warnings
        if '.bbl:' in w.lower() or 'ambiguous root BBL' in w]
    # The original archive remains in place; absolute location is provenance,
    # not an unsafe absolute archive_path in the canonical manifest schema.
    write_json(directory/'manifest.json', manifest)
    issues = validate_manifest(manifest, arxiv_id=identity, version=version,
                               source_dir=directory/'source')
    provenance = dict(arxiv_id=identity, version=version, member_id=item.get('member_id'),
                      archive=str(archive.resolve()), archive_sha256=sha(archive.read_bytes()),
                      download=download_info, extraction=result, records=records, warnings=warnings, validation=issues)
    write_json(directory/'recovery_provenance.json', provenance)
    old_paper = Path(item['paper_dir']) if item.get('paper_dir') else None
    metadata = paper/'metadata.json'
    if not metadata.exists():
        if old_paper and (old_paper/'metadata.json').is_file():
            old_meta = json.loads((old_paper/'metadata.json').read_text(encoding='utf-8'))
            old_meta['versions'] = [version]
            write_json(metadata, old_meta)
        else:
            write_json(metadata, {'arxiv_id': identity, 'versions': [version]})
        if old_paper and (old_paper/'references.json').is_file():
            shutil.copyfile(old_paper/'references.json', paper/'references.json')
    else:
        meta = json.loads(metadata.read_text(encoding='utf-8'))
        meta['versions'] = sorted(set(meta['versions']) | {version})
        write_json(metadata, meta)
    return dict(arxiv_id=identity, version=version, member_id=item.get('member_id'),
                status=('no_tex' if not has_tex else 'recovered' if not warnings and not issues
                        else 'recovered_with_diagnostics'),
                files=len(files), conversions=[r for r in records if r.get('conversion')!='unchanged'],
                warnings=warnings, validation=issues)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--download', action='store_true')
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding='utf-8'))
    keys = [(require_arxiv_id(x['arxiv_id']), require_version(x['version'])) for x in plan]
    if len(keys) != len(set(keys)):
        raise ValueError('duplicate paper/version in recovery plan')
    args.output.mkdir(parents=True, exist_ok=False)
    rows = []
    for item in plan:
        started = time.monotonic()
        try:
            row = recover_version(item, args.output, download=args.download)
        except Exception as exc:
            row = dict(arxiv_id=item['arxiv_id'], version=item['version'],
                       member_id=item.get('member_id'), status='failed', error=str(exc))
        rows.append(row)
        write_json(args.output/'recovery_report.json', rows)
        print(json.dumps({k:v for k,v in row.items() if k not in {'conversions','validation'}},ensure_ascii=True),flush=True)
        if args.download:
            time.sleep(max(0, 3.1 - (time.monotonic()-started)))


if __name__ == '__main__':
    main()
