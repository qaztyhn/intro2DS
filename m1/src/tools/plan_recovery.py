"""Plan a deterministic, balanced recovery wave from existing M2 reports.

No downloads or input edits. Default per member: 4 BBL, 4 missing bibliography,
4 other text dependencies, 2 root/structure cases and 2 clean controls.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from m1.src.schema import require_arxiv_id, require_version
from m1.src.utils.common import paper_key

QUOTAS={'other_text_dependency':4,'explicit_bbl_missing':4,'bibliography_missing':4,
        'root_or_structure':2,'clean_control':2}


def categories(publication, version):
    a=publication['arxiv_id'];number=version['version']
    prefix=f'{a}v{number} '
    warnings=[w for w in publication.get('warnings',[]) if w.startswith(prefix)]
    result=set()
    for w in warnings:
        if 'MISSING_DEPENDENCY:' in w and re.search(r'\.bbl(?:\s|$)',w,re.I):
            result.add('explicit_bbl_missing')
        if 'MISSING_BIB_RESOURCE:' in w:result.add('bibliography_missing')
        if 'MISSING_DEPENDENCY:' in w and re.search(r'\.(?:sty|cls|def|inc|ltx|latex|txt|tikz|pgf|pdf_tex|eps_tex)(?:\s|$)',w,re.I):
            result.add('other_text_dependency')
        if any(' '+code+':' in w for code in ['NO_ROOT','AMBIGUOUS_ROOT','MULTIPLE_ROOT_DECLARATIONS',
                      'UNSUPPORTED_SUBFILE','UNRESOLVED_CONDITIONAL','DEFERRED_MACRO_DIRECTIVE']):
            result.add('root_or_structure')
    if version.get('status')=='ok' and not warnings:result.add('clean_control')
    if version.get('status') in {'no_tex','skipped'}:return set()
    return result


def select_wave(records, roots, *, seed=2026, exclude_publications=(), quotas=None):
    quotas=QUOTAS if quotas is None else quotas
    if any(type(n) is not int or n < 1 for n in quotas.values()):
        raise ValueError('stratum quotas must be positive integers')
    excluded=set(exclude_publications);plan=[];summary={}
    if set(records)!=set(roots):raise ValueError('report members and M1 roots must match')
    for member in sorted(records):
        candidates=[]
        for publication in records[member]:
            a=require_arxiv_id(publication['arxiv_id'])
            if a in excluded:continue
            for version in publication.get('version_status',[]):
                number=require_version(version['version'])
                tags=categories(publication,version)
                if tags:candidates.append((a,number,tags,version))
        used=set();chosen=[]
        for tag,count in quotas.items():
            pool=[c for c in candidates if tag in c[2] and c[0] not in used]
            pool.sort(key=lambda c:hashlib.sha256(f'{seed}:{member}:{tag}:{c[0]}v{c[1]}'.encode()).hexdigest())
            picks=[]
            for c in pool:
                if c[0] in used:continue
                picks.append(c);used.add(c[0])
                if len(picks)==count:break
            if len(picks)!=count:
                raise ValueError(f'{member}: {tag} needs {count}, only {len(picks)} distinct papers available')
            for a,number,tags,v in picks:
                paper=Path(roots[member])/'papers'/paper_key(a)
                chosen.append(dict(arxiv_id=a,version=number,member_id=member,paper_dir=str(paper),
                    stratum=tag,all_observed_strata=sorted(tags),prior_m2_status=v['status'],
                    prior_m2_reason=v.get('reason')))
        plan.extend(chosen)
        summary[member]=dict(versions=len(chosen),papers=len(used),strata=dict(Counter(x['stratum'] for x in chosen)))
    if len(plan)!=len({x['arxiv_id'] for x in plan}):
        raise ValueError('selected publication belongs to multiple member reports')
    return plan,summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reports-root',required=True,type=Path)
    parser.add_argument('--m1-roots',required=True,type=Path,help='JSON mapping member_id -> M1 run directory')
    parser.add_argument('--exclude-plan',action='append',default=[],type=Path)
    parser.add_argument('--seed',type=int,default=2026)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    roots=json.loads(args.m1_roots.read_text(encoding='utf-8'))
    records={};sources={str(args.m1_roots):hashlib.sha256(args.m1_roots.read_bytes()).hexdigest()}
    for path in sorted(args.reports_root.glob('full_*/processing_report.json')):
        match=re.fullmatch(r'full_(\d{8})_.+',path.parent.name)
        if match and match[1] in roots:
            member=match[1]
            if member in records:raise ValueError(f'duplicate report for {member}')
            raw=path.read_bytes();records[member]=json.loads(raw)['publications']
            sources[str(path)]=hashlib.sha256(raw).hexdigest()
    excluded=set()
    for path in args.exclude_plan:
        data=json.loads(path.read_text(encoding='utf-8'))
        excluded.update(r['arxiv_id'] for r in data)
        sources[str(path)]=hashlib.sha256(path.read_bytes()).hexdigest()
    plan,summary=select_wave(records,roots,seed=args.seed,exclude_publications=excluded)
    # Require real metadata before spending network/time on this plan.
    for item in plan:
        paper=Path(item['paper_dir'])
        for needed in [paper/'metadata.json',paper/'versions'/f'v{item["version"]}'/'manifest.json']:
            if not needed.is_file():raise FileNotFoundError(needed)
    args.output.mkdir(parents=True,exist_ok=False)
    (args.output/'plan.json').write_text(json.dumps(plan,indent=2),encoding='utf-8')
    (args.output/'manifest.json').write_text(json.dumps(dict(seed=args.seed,quotas=QUOTAS,
        input_sha256=sources,excluded_publications=len(excluded),members=summary,
        scope='stratified version pilot; one version per publication; not an M3 selection'),indent=2),encoding='utf-8')
    print(json.dumps(summary,indent=2))


if __name__=='__main__':main()
