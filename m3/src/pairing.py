"""CLI for TV1: python -m m3.src.pairing --help."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from pathlib import Path

from .contracts import MaskConfig, normalize_arxiv_id
from .features import FEATURE_NAMES, prepare_pairs


def sha256(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def bibtex_checksums(path):
    path = Path(path)
    if path.is_file():
        return {path.name: sha256(path)}
    files = sorted(path.rglob('*.parquet'))
    if not files:
        raise FileNotFoundError(f'No Parquet under {path}')
    return {p.relative_to(path).as_posix(): sha256(p) for p in files}


def read_ids(path):
    ids = []
    for raw in Path(path).read_text(encoding='utf-8-sig').splitlines():
        if not raw.strip():
            continue
        value = normalize_arxiv_id(raw.strip())
        if value is None:
            raise ValueError(f'{path}: invalid ID {raw!r}')
        ids.append(value)
    if not ids or len(ids) != len(set(ids)):
        raise ValueError(f'{path}: empty or duplicate IDs')
    return ids


def git_info():
    try:
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
        dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], text=True).strip())
        return {'commit': head, 'git_dirty': dirty}
    except (OSError, subprocess.CalledProcessError):
        return {'commit': None, 'git_dirty': None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog-dir', required=True, type=Path)
    parser.add_argument('--bibtex-path', required=True, type=Path)
    parser.add_argument('--ids-file', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--member-id', help='Optional owner for this run manifest')
    parser.add_argument('--seed', type=int, default=2026)
    parser.add_argument('--mask-fraction', type=float, default=0.0)
    expected = parser.add_mutually_exclusive_group(required=True)
    expected.add_argument('--expected-bibtex-sha256', help='TV2 checksum for a single Parquet file')
    expected.add_argument('--expected-bibtex-checksums', type=Path, help='JSON mapping shard-relative filename to TV2 SHA256')
    expected.add_argument('--allow-unverified-snapshot', action='store_true', help='Provisional run only; checksum recorded but not verified against TV2')
    args = parser.parse_args(argv)
    ids = read_ids(args.ids_file)
    checksums = bibtex_checksums(args.bibtex_path)
    if args.expected_bibtex_sha256:
        if not args.bibtex_path.is_file() or next(iter(checksums.values())) != args.expected_bibtex_sha256.strip().lower():
            raise ValueError('BibTeX SHA256 mismatch; stop and confirm M2 snapshot with TV2')
    if args.expected_bibtex_checksums:
        expected_map = json.loads(args.expected_bibtex_checksums.read_text(encoding='utf-8'))
        if not isinstance(expected_map, dict) or checksums != {k: str(v).lower() for k,v in expected_map.items()}:
            raise ValueError('BibTeX shard checksums mismatch; stop and confirm M2 snapshot with TV2')
    if args.allow_unverified_snapshot:
        print('WARNING: provisional run; original TV2 M2 snapshot is not verified.')
    catalog_hashes = {f'{n}.parquet': sha256(args.catalog_dir / f'{n}.parquet') for n in ('papers', 'references', 'crawl_status', 'queries')}
    ids_hash = sha256(args.ids_file)
    mask = MaskConfig(fraction=args.mask_fraction, seed=args.seed)
    output = prepare_pairs(catalog_dir=args.catalog_dir, bibtex_path=args.bibtex_path,
                           publication_ids=ids, output_dir=args.output_dir, mask=mask)
    with (output / 'pairing_report.csv').open(encoding='utf-8', newline='') as f:
        rows = list(csv.DictReader(f))
    eligible = sum(r['eligible'] == 'True' for r in rows)
    total_pairs = sum(int(r['pair_count']) for r in rows)
    unchanged = (checksums == bibtex_checksums(args.bibtex_path)
                 and catalog_hashes == {k: sha256(args.catalog_dir/k) for k in catalog_hashes}
                 and ids_hash == sha256(args.ids_file))
    manifest = {'run_id': output.name, 'member_id': args.member_id, 'seed': args.seed,
                **git_info(), 'mask_fraction': mask.fraction, 'mask_fields': list(mask.fields),
                'snapshot_verified_against_tv2': not args.allow_unverified_snapshot and unchanged,
                'inputs_unchanged_during_run': unchanged,
                'bibtex_path': str(args.bibtex_path.resolve()), 'bibtex_sha256': checksums,
                'catalog_dir': str(args.catalog_dir.resolve()), 'catalog_sha256': catalog_hashes,
                'ids_file': str(args.ids_file.resolve()), 'ids_sha256': ids_hash,
                'features': FEATURE_NAMES, 'selected_papers': len(ids),
                'eligible_papers': eligible, 'ineligible_papers': len(ids)-eligible,
                'total_pairs': total_pairs,
                'output_sha256': {n: sha256(output/n) for n in ('pairs.parquet', 'pairing_report.csv')}}
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    if not unchanged:
        raise RuntimeError('Input changed during pairing; output is INVALID, do not hand off')
    print(json.dumps({k: manifest[k] for k in ('selected_papers','eligible_papers','ineligible_papers','total_pairs','snapshot_verified_against_tv2')}, indent=2))
    print(f'Output: {output}')
    if eligible != len(ids):
        print('WARNING: some selected papers cannot be labeled; review pairing_report.csv with TV2.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
