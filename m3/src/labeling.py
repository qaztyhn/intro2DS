"""TV2 CLI. Run python -m m3.src.labeling --help from the repository root."""

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq

from .contracts import auto_quota, make_table, validate_labels, validate_registry
from .labels import (
    audit_auto_rule,
    canonical_table,
    check_manual_quota,
    coverage_by_paper,
    label_automatically,
    load_manual_labels,
    normalize_publications,
    write_labeling_packet,
)
from .splits import split_publications


def file_checksums(paths):
    result = {}
    for path in paths:
        with path.open("rb") as stream:
            result[str(path.resolve())] = hashlib.file_digest(
                stream, "sha256"
            ).hexdigest()
    return result


def run_labeling(*, input_dir, manual_csvs, output_dir, seed=2026):
    """Write one atomic, independently inspectable TV2 run from TV1's tables."""
    source, output = Path(input_dir), Path(output_dir)
    if output.exists():
        raise FileExistsError(output)
    names = (
        "assignments",
        "manual_publications",
        "auto_publications",
        "queries",
        "pairs",
        "crawl_status",
    )
    paths = [source / f"{name}.parquet" for name in names] + list(
        map(Path, manual_csvs)
    )
    checksums = file_checksums(paths)
    tables = {
        name: canonical_table(name, pq.read_table(source / f"{name}.parquet"))
        for name in names
    }
    assignments, manual, auto = (tables[n] for n in names[:3])
    validate_registry(assignments, manual, auto)
    context = {n: tables[n] for n in ("queries", "pairs", "crawl_status")}
    manual_labels = load_manual_labels(
        manual_csvs, **context, assignments=assignments, manual_publications=manual
    )
    auto_labels = label_automatically(
        **context,
        publication_ids=auto["arxiv_id"].to_pylist(),
        assignments=assignments,
        manual_publications=manual,
    )
    labels = pa.concat_tables([manual_labels, auto_labels]).sort_by(
        [("arxiv_id", "ascending"), ("citation_key", "ascending")]
    )
    validate_labels(
        labels, tables["pairs"], tables["crawl_status"], assignments, manual, auto
    )
    quota = {}
    for member in sorted(set(assignments["member_id"].to_pylist())):
        own = make_table(
            "assignments",
            [r for r in assignments.to_pylist() if r["member_id"] == member],
        )
        registry = make_table(
            "manual_publications",
            [r for r in manual.to_pylist() if r["member_id"] == member],
        )
        member_labels = make_table(
            "labels", [r for r in manual_labels.to_pylist() if r["member_id"] == member]
        )
        try:
            quota[member] = dict(
                passed=True, **check_manual_quota(member_labels, registry, own)[member]
            )
        except ValueError as exc:
            quota[member] = dict(passed=False, error=str(exc))
        required_auto = max(2, auto_quota(len(own), len(registry)))
        member_auto = {
            r["arxiv_id"] for r in auto.to_pylist() if r["member_id"] == member
        }
        actual_auto = len(member_auto)
        query_papers = set(tables["queries"]["arxiv_id"].to_pylist())
        candidate_papers = set(tables["pairs"]["arxiv_id"].to_pylist())
        ineligible = sorted(member_auto - (query_papers & candidate_papers))
        quota[member].update(
            required_auto_papers=required_auto,
            auto_papers=actual_auto,
            ineligible_auto_papers=ineligible,
        )
        if ineligible:
            quota[member]["passed"] = False
        if actual_auto < required_auto:
            quota[member]["passed"] = False
            quota[member][
                "auto_error"
            ] = f"Need {required_auto} auto papers, got {actual_auto}"
    split_error = None
    splits = None
    if all(r["passed"] for r in quota.values()):
        try:
            splits = split_publications(
                manual_labels=manual_labels,
                auto_labels=auto_labels,
                owners=assignments,
                manual_publications=manual,
                auto_publications=auto,
                seed=seed,
            )
        except ValueError as exc:
            split_error = str(exc)
    else:
        split_error = "Quota or eligibility incomplete. See quota_report.json before final splits."
    report = audit_auto_rule(
        **context, manual_labels=manual_labels, assignments=assignments
    )
    selected = manual["arxiv_id"].to_pylist() + auto["arxiv_id"].to_pylist()
    coverage = coverage_by_paper(
        queries=tables["queries"], labels=labels, publication_ids=selected
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".labeling-", dir=output.parent))
    try:
        pq.write_table(labels, staging / "labels.parquet")
        if splits is not None:
            pq.write_table(splits, staging / "splits.parquet")
        for filename, data in (
            ("auto_rule_report.json", report),
            ("quota_report.json", quota),
        ):
            (staging / filename).write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        with (staging / "coverage_by_paper.csv").open(
            "w", newline="", encoding="utf-8"
        ) as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=[
                    "arxiv_id",
                    "entries",
                    "labeled_entries",
                    "unlabeled_entries",
                    "coverage",
                ],
            )
            writer.writeheader()
            writer.writerows(coverage)
        if checksums != file_checksums(paths):
            raise ValueError("Input files changed during labeling")
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip()
        manifest = dict(
            run_id=output.name,
            seed=seed,
            commit=commit,
            rule="auto_rule_v1",
            labels=len(labels),
            selected_papers=len(selected),
            complete=splits is not None,
            git_dirty=bool(
                subprocess.check_output(
                    ["git", "status", "--porcelain"], text=True
                ).strip()
            ),
            output_sha256={
                Path(k).name: v
                for k, v in file_checksums(sorted(staging.iterdir())).items()
            },
            split_error=split_error,
            input_sha256=checksums,
        )
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        # mkdir reserves the destination against simultaneous writers.
        output.mkdir()
        os.replace(staging, output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    packet = sub.add_parser("packet")
    packet.add_argument("--bibtex", required=True, type=Path)
    packet.add_argument("--references", required=True, type=Path)
    packet.add_argument("--ids-file", required=True, type=Path)
    packet.add_argument("--output-dir", required=True, type=Path)
    run = sub.add_parser("run")
    run.add_argument("--input-dir", required=True, type=Path)
    run.add_argument("--manual-csv", nargs="*", default=[])
    run.add_argument("--output-dir", required=True, type=Path)
    run.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args(argv)
    if args.command == "packet":
        ids = normalize_publications(
            args.ids_file.read_text(encoding="utf-8-sig").split()
        )
        write_labeling_packet(
            bibtex=pq.read_table(args.bibtex),
            references=pq.read_table(args.references),
            publication_ids=ids,
            output_dir=args.output_dir,
        )
        return 0
    result = run_labeling(
        input_dir=args.input_dir,
        manual_csvs=args.manual_csv,
        output_dir=args.output_dir,
        seed=args.seed,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
