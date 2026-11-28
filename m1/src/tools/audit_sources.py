"""Summarize M1 source decisions and write a versioned archive-retry plan."""
import argparse
import json
from collections import Counter
from pathlib import Path

from m1.src.schema import no_symlinks,require_arxiv_id,require_version,safe_path
from m1.src.stages.source_assets import POLICY


def audit_run(run_dir, *, member_id=None):
    run=no_symlinks(run_dir);rows=[];errors=[];retry=[];suffixes=Counter()
    for paper in sorted((run/'papers').iterdir()):
        if not paper.is_dir():
            continue
        try:
            metadata=json.loads(safe_path(paper,'metadata.json').read_text(encoding='utf-8'))
            identity=require_arxiv_id(metadata['arxiv_id'])
            versions=metadata['versions']
            if not isinstance(versions,list) or not versions or len(versions)!=len(set(versions)):
                raise ValueError('metadata has no valid unique version list')
            for v in versions:require_version(v)
        except (OSError,ValueError,KeyError,TypeError) as exc:
            errors.append(dict(paper_dir=str(paper),error=str(exc)))
            continue
        for version in versions:
            reasons=[];audit={};manifest={}
            try:
                directory=safe_path(paper,f'versions/v{version}')
                record=json.loads(safe_path(directory,'manifest.json').read_text(encoding='utf-8'))
                if not isinstance(record,dict):raise ValueError('manifest must be an object')
                manifest=record
                if manifest['arxiv_id']!=identity or manifest['version']!=version:
                    raise ValueError('manifest identity mismatch')
                if manifest.get('download_status')!='ok':reasons.append('download_not_ok')
                if manifest.get('extract_status') not in {'ok','empty'}:reasons.append('extract_not_complete')
                if manifest.get('source_asset_policy')!=POLICY:
                    reasons.append('source_policy_not_applied')
                else:
                    record=json.loads(safe_path(directory,'source_assets_audit.json').read_text(encoding='utf-8'))
                    if not isinstance(record,dict):raise ValueError('source audit must be an object')
                    audit=record
                    if audit.get('arxiv_id')!=identity or audit.get('version')!=version or audit.get('policy')!=POLICY:
                        raise ValueError('source audit identity/policy mismatch')
                    if not audit.get('archive_scan_complete'):reasons.append('archive_scan_incomplete')
                    if audit.get('filtered_source_candidates'):reasons.append('filtered_source_candidates')
                    for record in audit.get('records',[]):
                        if record.get('error') and (record['error']!='plain txt not identified as TeX source'
                                                  or record['original'].lower().endswith('.bib.txt')):
                            reasons.append('unconverted_source_assets');break
            except (OSError,ValueError,KeyError,TypeError) as exc:
                reasons.append('missing_or_invalid_source_audit')
                audit_error=str(exc)
            else:
                audit_error=None
            counts=audit.get('filtered_suffix_counts',{})
            if not counts:
                counts=dict(Counter(r['suffix'] for r in audit.get('inventory',[]) if r['disposition']=='filtered'))
            suffixes.update(counts)
            archive=None
            try:
                if manifest.get('archive_path'):
                    candidate=safe_path(run,manifest['archive_path'])
                    if candidate.is_file():archive=str(candidate.resolve())
            except ValueError:
                reasons.append('unsafe_archive_path')
            reasons=sorted(set(reasons))
            row=dict(arxiv_id=identity,version=version,extract_status=manifest.get('extract_status'),
                policy=manifest.get('source_asset_policy'),archive_available=archive is not None,
                filtered_suffix_counts=counts,filtered_source_candidates=audit.get('filtered_source_candidates',[]),
                unconverted_assets=[r for r in audit.get('records',[]) if r.get('error')],
                warnings=audit.get('warnings',[]),review_reasons=reasons)
            if audit_error:row['audit_error']=audit_error
            rows.append(row)
            if reasons:
                item=dict(arxiv_id=identity,version=version,paper_dir=str(paper.resolve()),review_reasons=reasons)
                if archive:item['archive']=archive
                if member_id:item['member_id']=member_id
                retry.append(item)
    return dict(summary=dict(publications=len({r['arxiv_id'] for r in rows}),versions=len(rows),
        extract_status=dict(Counter(r['extract_status'] for r in rows)),
        filtered_suffix_counts=dict(suffixes),review_versions=len(retry),
        retry_archive_available=sum('archive' in r for r in retry),publication_errors=len(errors),
        note='Review candidates, not proof that another extraction can repair every case; PDF-only source is not a parser failure.'),
        versions=rows,publication_errors=errors,retry_plan=retry)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--member-id')
    args=parser.parse_args()
    result=audit_run(args.run_dir,member_id=args.member_id)
    args.output.mkdir(parents=True,exist_ok=False)
    (args.output/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (args.output/'retry_plan.json').write_text(json.dumps(result['retry_plan'],indent=2),encoding='utf-8')
    print(json.dumps(result['summary'],ensure_ascii=True))


if __name__=='__main__':main()
