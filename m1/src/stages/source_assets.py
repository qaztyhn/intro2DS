"""Preserve original text assets, derive M2 inputs and audit archive filtering.

Called by the normal M1 extract hook. Conversion reuses the tested recovery
implementation; it never fetches metadata or executes TeX. The raw downloaded
archive remains at manifest.archive_path for future retries of unknown formats.
"""
import hashlib
import json
import os
import tempfile
from collections import Counter
from pathlib import Path

from ..compilation import MAX_COMPILATION_BYTES, compilation_record
from ..schema import NO_TEX_SOURCE, no_symlinks
from ..utils.common import EMPTY, FAILED, OK, SKIPPED, read_json, write_json_atomic
from ..utils.errors import CrawlError, INVALID_RESPONSE
from .extract import _same_output, extract_source

POLICY='preserve-convert-v1'


def _publish_identical(stage, destination):
    no_symlinks(destination)
    files=sorted(p.relative_to(stage).as_posix() for p in stage.rglob('*') if p.is_file())
    if destination.exists() and any(destination.iterdir()):
        if not _same_output(destination,stage,files):
            raise CrawlError('existing source/assets differ from derivation; use a new run or recovery output',INVALID_RESPONSE)
    else:
        os.replace(stage,destination)


def extract_preserved_source(archive, directory, arxiv_id, version, *, limits):
    # Lazy import avoids a module initialization cycle: the standalone recovery
    # tool also uses the low-level extractor. Both entry points share conversion.
    from ..tools.recover_sources import RECOVERY_SUFFIXES, convert_assets
    directory=Path(directory)
    with tempfile.TemporaryDirectory(prefix='.preserve-',dir=directory) as temp:
        temp=Path(temp);original=temp/'original_assets';source=temp/'source'
        result=extract_source(archive,original,limits=limits,
            source_suffixes=RECOVERY_SUFFIXES,source_names={'00README.json'})
        audit=dict(arxiv_id=arxiv_id,version=version,policy=POLICY,
            archive_scan_complete=result['archive_scan_complete'],
            inventory=result['asset_inventory'],extraction=dict(result),warnings=[])
        result.update(source_asset_policy=POLICY,source_asset_audit='source_assets_audit.json')
        if result['extract_status']==FAILED:
            write_json_atomic(directory/'source_assets_audit.json',audit)
            return result
        records,warnings=convert_assets(original,source)
        readme=original/'00README.json'
        compilation=None
        if readme.is_file():
            try:
                with readme.open('rb') as handle:
                    compilation=compilation_record(handle.read(MAX_COMPILATION_BYTES+1),arxiv_id,version)
            except (ValueError,UnicodeError) as exc:
                warnings.append(f'00README.json: unusable compilation metadata: {exc}')
        files=sorted(p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file())
        has_tex=any(name.lower().endswith('.tex') for name in files)
        result.update(extract_status=OK if has_tex else EMPTY,source_files=files,
            # Original extraction bytes remain in audit; derivation can enlarge
            # text, so do not mix original and derived byte denominators.
            unpacked_total_bytes=None,
            kept_source_bytes=sum((source/name).stat().st_size for name in files),
            error_code=None if has_tex else NO_TEX_SOURCE,
            recovery_bibliography_warnings=[w for w in warnings if '.bbl:' in w.lower() or 'ambiguous root BBL' in w])
        with Path(archive).open('rb') as handle:
            archive_sha=hashlib.file_digest(handle,'sha256').hexdigest()
        audit.update(archive_sha256=archive_sha,records=records,warnings=warnings,
            filtered_suffix_counts=dict(Counter(r['suffix'] for r in audit['inventory'] if r['disposition']=='filtered')),
            filtered_source_candidates=[r['path'] for r in audit['inventory'] if r['disposition']=='filtered' and r['source_like']],
            derived_files=files)
        write_json_atomic(directory/'source_assets_audit.json',audit)
        _publish_identical(original,directory/'original_assets')
        if compilation is not None:
            write_json_atomic(directory/'recovery_compilation.json',compilation)
        write_json_atomic(directory/'recovery_provenance.json',dict(arxiv_id=arxiv_id,version=version,
            archive=str(Path(archive).resolve()),archive_sha256=archive_sha,policy=POLICY,
            extraction=audit['extraction'],records=records,warnings=warnings))
        _publish_identical(source,directory/'source')
        return result


def log_asset_summary(result, *, directory, arxiv_id, version, logger, cached):
    if result.get('source_asset_policy')!=POLICY:
        message='legacy cached extraction has no preserve/convert audit; use a new run or recovery tool'
        logger.log('source_assets',SKIPPED,arxiv_id=arxiv_id,version=version,
                   error_code='SOURCE_POLICY_NOT_APPLIED',message=message)
        print(f'[{arxiv_id}v{version}] SOURCE_POLICY_NOT_APPLIED: {message}',flush=True)
        return
    audit=read_json(Path(directory)/'source_assets_audit.json')
    if not audit:
        logger.log('source_assets',FAILED,arxiv_id=arxiv_id,version=version,
                   error_code='SOURCE_AUDIT_MISSING',message='source_assets_audit.json is missing')
        print(f'[{arxiv_id}v{version}] SOURCE_AUDIT_MISSING',flush=True)
        return
    counts=audit.get('filtered_suffix_counts',dict(Counter(
        r['suffix'] for r in audit.get('inventory',[]) if r['disposition']=='filtered')))
    candidates=audit.get('filtered_source_candidates',[])
    unconverted=[r['original'] for r in audit.get('records',[]) if r.get('error')]
    message=json.dumps(dict(filtered_suffixes=counts,filtered_source_candidates=candidates,
        unconverted_source_assets=unconverted,warnings=len(audit.get('warnings',[])),
        audit='source_assets_audit.json',cached=cached),ensure_ascii=True)
    logger.log('source_assets',FAILED if result['extract_status']==FAILED else OK,
        arxiv_id=arxiv_id,version=version,error_code='SOURCE_ASSETS_REVIEW' if candidates or unconverted else None,
        message=message)
    print(f'[{arxiv_id}v{version}] source_assets: {message}',flush=True)
