"""Offline M2 CLI. One publication and all its versions per unit of work."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from m1.src.schema import no_symlinks, relative_path, require_arxiv_id
from .benchmark import run_benchmark
from .pipeline import process_publication_detailed
from .qa_report import build_qa_report


def load_root_choices(path: Path | None):
    """Version-specific reviewed choices, with reasons; never a filename heuristic."""
    if path is None:
        return {}
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(raw, dict):
        raise ValueError('root choices must be an object keyed by arxiv_id')
    result = {}
    for identity, versions in raw.items():
        require_arxiv_id(identity)
        if not isinstance(versions, dict):
            raise ValueError('root choices must contain version objects')
        result[identity] = {}
        for version, choice in versions.items():
            if not isinstance(version, str) or not version.isascii() or not version.isdecimal() or int(version) < 1 or str(int(version)) != version:
                raise ValueError('root choice version must be a canonical positive integer string')
            if not isinstance(choice, dict) or set(choice) != {'root_file', 'reason'}:
                raise ValueError('each root choice requires root_file and reason')
            if not isinstance(choice['reason'], str) or not choice['reason'].strip():
                raise ValueError('root choice reason must be nonempty')
            result[identity][int(version)] = dict(root_file=relative_path(choice['root_file']), reason=choice['reason'])
    return result


def run(run_dir: Path, ids_file: Path, output_dir: Path, label: str, *, root_choices_path=None):
    choices = load_root_choices(root_choices_path)
    run_dir = no_symlinks(Path(run_dir).absolute())
    output_dir = Path(output_dir).resolve()
    if output_dir == run_dir or run_dir in output_dir.parents:
        raise ValueError('M2 output must be outside the M1 run directory')
    # Refuse reused labels before opening any logs or outputs.
    if (output_dir / label).exists() or (output_dir / f'benchmark_{label}.json').exists():
        raise FileExistsError(f'output label already used: {label}')
    statuses, bib_before, stages = [], {}, {}
    audit = []

    def process(directory, identity):
        try:
            result = process_publication_detailed(directory, identity, root_choices=choices.get(identity))
        except Exception as error:
            logging.getLogger(__name__).exception('%s version=unknown stage=publication', identity)
            audit.append({'arxiv_id': identity, 'error_type': type(error).__name__, 'error': str(error)})
            raise
        statuses.extend(result.version_status)
        bib_before[identity] = result.bib_entries_before
        for stage, seconds in result.stage_seconds.items():
            stages[stage] = stages.get(stage, 0) + seconds
        audit.append({'arxiv_id': identity, 'version_status': result.version_status,
                      'warnings': result.warnings,
                      'bibliography_reconciliation': result.bibliography_reconciliation})
        if result.version_status and all(
                row['status'] in {'failed', 'skipped'} for row in result.version_status):
            raise RuntimeError(f'{identity}: all recorded versions failed or were skipped')
        return result.elements, result.hierarchy, result.bibtex

    benchmark = run_benchmark(run_dir, ids_file, output_dir, label, process=process,
                              config={'root_choices': {identity: {str(v): c for v, c in vs.items()}
                                                      for identity, vs in choices.items()}} if choices else {})
    tables = output_dir / label / 'tables'
    qa = build_qa_report(tables, statuses, output_dir / f'benchmark_{label}.json',
                         bib_entries_before=bib_before, scope=f'ID list: {Path(ids_file).name}')
    report = {'publications': audit, 'stage_seconds_sum': stages,
              'note': 'Per-stage sums include calls returning a detailed result, even when versions failed. '
                      'Publication attempts raising before that result are excluded; benchmark wall time includes them.'}
    (output_dir / label / 'processing_report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return benchmark, qa


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--ids-file', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--root-choices', type=Path,
                        help='Reviewed per-publication/version root JSON with reasons')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    benchmark, qa = run(args.run_dir, args.ids_file, args.output_dir, args.label,
                        root_choices_path=args.root_choices)
    versions = qa['versions']['counts']['by_status']
    print(json.dumps({'counts': benchmark['counts'], 'versions': versions,
                      'validation': qa['validation']['passed']}))
    return 0 if (qa['validation']['passed']
                 and not benchmark['counts']['publications_failed']
                 and not versions['failed'] and not versions['skipped']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
