"""Build a separate M1 snapshot with recovered versions and all siblings intact.

Each plan row supplies arxiv_id, version, paper_dir and recovered_run. Originals
are read only; the destination must not already exist. Reparse whole publications
from this snapshot because M2 deduplicates elements and bibliography across versions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from collections import defaultdict
from pathlib import Path

from m1.src.schema import no_symlinks, require_arxiv_id, require_version
from m1.src.utils.common import paper_key
from m1.src.stages.validation import validate_manifest


def _read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def _hashes(directory):
    result={}
    for path in sorted(directory.rglob('*')):
        no_symlinks(path)
        if path.is_file():
            result[path.relative_to(directory).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def build_overlay(plan, output):
    output=no_symlinks(output)
    if output.exists():
        raise FileExistsError(output)
    groups=defaultdict(dict)
    originals={}
    # Validate the complete plan before copying any inputs.
    for item in plan:
        identity=require_arxiv_id(item['arxiv_id']);number=require_version(item['version'])
        original=no_symlinks(item['paper_dir'])
        recovered=no_symlinks(Path(item['recovered_run'])/'papers'/paper_key(identity)/'versions'/f'v{number}')
        if identity in originals and originals[identity]!=original:
            raise ValueError(f'inconsistent original paper directory: {identity}')
        if number in groups[identity]:
            raise ValueError(f'duplicate replacement: {identity}v{number}')
        for source in [original,recovered]:
            if not source.is_dir():
                raise FileNotFoundError(source)
            if output.is_relative_to(source) or source.is_relative_to(output):
                raise ValueError('output and input directories must not overlap')
        metadata=_read(original/'metadata.json')
        if metadata.get('arxiv_id')!=identity or number not in metadata.get('versions',[]):
            raise ValueError(f'replacement not declared in original metadata: {identity}v{number}')
        manifest=_read(recovered/'manifest.json')
        issues=validate_manifest(manifest,arxiv_id=identity,version=number,source_dir=recovered/'source')
        if issues:
            raise ValueError(f'invalid recovered manifest: {identity}v{number}: {issues}')
        _hashes(original);_hashes(recovered)
        originals[identity]=original;groups[identity][number]=recovered
    if not groups:
        raise ValueError('empty overlay plan')
    output.mkdir(parents=True)
    provenance=[]
    for identity,replacements in sorted(groups.items()):
        original=originals[identity];paper=output/'papers'/paper_key(identity)
        paper.mkdir(parents=True)
        for child in original.iterdir():
            if child.name=='versions':
                continue
            if child.is_dir():
                shutil.copytree(child,paper/child.name)
            else:
                shutil.copyfile(child,paper/child.name)
        versions=paper/'versions';versions.mkdir()
        for child in sorted((original/'versions').iterdir()):
            if child.name in {f'v{v}' for v in replacements}:
                continue
            if child.is_dir():
                shutil.copytree(child,versions/child.name)
            else:
                shutil.copyfile(child,versions/child.name)
        for number,recovered in sorted(replacements.items()):
            shutil.copytree(recovered,versions/f'v{number}')
        expected=_hashes(original)
        for number in replacements:
            prefix=f'versions/v{number}/'
            expected={k:v for k,v in expected.items() if not k.startswith(prefix)}
            expected.update({prefix+k:v for k,v in _hashes(versions/f'v{number}').items()})
        actual=_hashes(paper)
        if actual!=expected:
            raise RuntimeError(f'overlay byte verification failed: {identity}')
        provenance.append(dict(arxiv_id=identity,original_paper=str(original),
            replacements={str(v):str(p) for v,p in replacements.items()},
            versions=_read(paper/'metadata.json')['versions'],file_sha256=actual))
    (output/'overlay_manifest.json').write_text(json.dumps(provenance,indent=2),encoding='utf-8')
    return provenance


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=build_overlay(_read(args.plan),args.output)
    print(json.dumps(dict(publications=len(result),versions=sum(len(r['versions']) for r in result))))


if __name__=='__main__':
    main()
