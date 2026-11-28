"""Lệnh tích hợp của pipeline M3.

Ví dụ:
  python -m m3.src.pipeline --stage export --member 24120058 --run-id r1 --config m3/configs/members.yaml
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from m3.src.contracts import SCHEMAS
from m3.src.export import DatasetInputs, check_dataset, export_dataset

STAGES = ("catalog", "pairs", "labels", "splits", "train", "predict", "export")


# ---------------------------------------------------------------- tiện ích chung
def load_config(path) -> dict:
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    for key in ("seed", "paths", "members"):
        if key not in cfg:
            raise ValueError(f"[config] thiếu khóa '{key}' trong {path}")
    for key in ("m2_dir", "work_dir", "output_dir"):
        if key not in cfg["paths"]:
            raise ValueError(f"[config] thiếu paths.{key} trong {path}")
    return cfg


def read_table(path) -> pa.Table:
    """Đọc một file .parquet hoặc một thư mục shard."""
    p = Path(path)
    if p.is_dir() and not any(p.glob("*.parquet")):
        raise FileNotFoundError(f"Thư mục không có shard parquet: {p}")
    if not p.exists():
        raise FileNotFoundError(f"Thiếu file đầu vào: {p}")
    return pq.read_table(p)


def find_table(base: Path, name: str) -> Path:
    """Tìm <base>/<name>.parquet hoặc thư mục <base>/<name>/."""
    for cand in (base / f"{name}.parquet", base / name):
        if cand.exists():
            return cand
    raise FileNotFoundError(f"Thiếu bảng '{name}' trong {base} (file {name}.parquet hoặc thư mục {name}/)")


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:
        return "unknown"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_manifest(path: Path, *, stage: str, seed: int, run_id: str, files_root: Path, extra: dict) -> dict:
    """Ghi manifest.json: stage, seed, commit, danh sách file parquet kèm số dòng và sha256."""
    files = []
    for f in sorted(files_root.rglob("*.parquet")):
        files.append({"path": str(f.relative_to(files_root)),
                      "rows": pq.ParquetFile(f).metadata.num_rows,
                      "sha256": sha256_file(f)})
    manifest = {"stage": stage, "seed": seed, "run_id": run_id, "commit": git_commit(),
                "files": files, **extra}
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


# ---------------------------------------------------------------- stage export
def member_publication_ids(assignments: pa.Table, member_id: str) -> list[str]:
    rows = assignments.to_pylist()
    ids = sorted(r["arxiv_id"] for r in rows if r["member_id"] == member_id)
    if not ids:
        raise ValueError(f"[assignments] thành viên {member_id} không có bài nào được giao")
    return ids


def stage_export(cfg: dict, args) -> Path:
    if not args.member:
        raise ValueError("stage export cần --member <member_id>")
    if args.member not in cfg["members"]:
        raise ValueError(f"[config] thành viên {args.member} không có trong danh sách members")
    m2, work = Path(cfg["paths"]["m2_dir"]), Path(cfg["paths"]["work_dir"])
    ids = member_publication_ids(read_table(find_table(work, "assignments")), args.member)

    pred_path = next((p for p in (work / "predictions.parquet", work / "predictions") if p.exists()), None)
    try:
        labels = read_table(find_table(work, "labels"))
    except FileNotFoundError:
        if args.predictions_scope == "labeled_only":
            raise
        labels = SCHEMAS["labels"].empty_table()

    inputs = DatasetInputs(
        papers=read_table(find_table(work, "papers")),
        references=read_table(find_table(work, "references")),
        elements=read_table(find_table(m2, "elements")),
        hierarchy=read_table(find_table(m2, "hierarchy")),
        bibtex=read_table(find_table(m2, "bibtex")),
        splits=read_table(find_table(work, "splits")),
        labels=labels,
        predictions=read_table(pred_path) if pred_path else None,
    )

    stage_dir = Path(cfg["paths"]["output_dir"]) / args.run_id / f"export_{args.member}"
    dataset_dir = stage_dir / "dataset"
    result = export_dataset(inputs, output_dir=dataset_dir, publication_ids=ids,
                            rows_per_shard=args.rows_per_shard, predictions_scope=args.predictions_scope)
    report = check_dataset(dataset_dir, publication_ids=ids)
    if not report["ok"]:
        raise ValueError(f"[check_dataset] {report['n_errors']} lỗi, ví dụ: {report['errors'][:5]}")
    write_manifest(stage_dir / "manifest.json", stage="export", seed=cfg["seed"], run_id=args.run_id,
                   files_root=dataset_dir,
                   extra={"member": args.member, "n_publications": len(ids),
                          "predictions_scope": args.predictions_scope, "row_counts": result.row_counts})
    print(f"Đã xuất {dataset_dir}: {result.row_counts}")
    return dataset_dir


STAGE_FUNCS = {"export": stage_export}


# ---------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Pipeline M3")
    p.add_argument("--stage", required=True, choices=STAGES)
    p.add_argument("--config", required=True)
    p.add_argument("--run-id", required=True, help="tên lần chạy, mỗi lần xuất cần một run-id mới")
    p.add_argument("--member", help="member_id của người cần xuất lát cắt (stage export)")
    p.add_argument("--predictions-scope", default="all_entries", choices=("all_entries", "labeled_only"))
    p.add_argument("--rows-per-shard", type=int, default=100_000)
    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    func = STAGE_FUNCS.get(args.stage)
    if func is None:
        raise NotImplementedError(f"stage '{args.stage}' chưa được nối vào pipeline")
    func(cfg, args)


if __name__ == "__main__":
    main()