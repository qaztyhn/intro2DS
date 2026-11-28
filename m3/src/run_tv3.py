"""Chạy toàn bộ phần model của TV3 từ thư mục Parquet: chọn model, sinh predictions, ghi metrics và thí nghiệm.

Dùng từ pipeline (stage train/predict của TV4):

    from m3.src.run_tv3 import run
    info = run(input_dir=..., output_dir=..., seed=2026, masked_pairs_dir=None, scope="all_entries")

hoặc từ dòng lệnh (Colab):

    python -m m3.src.run_tv3 --input <thư mục> --output <thư mục mới> [--seed 2026] [--masked-pairs-dir <thư mục>] [--scope all_entries]

Đầu vào: `input_dir/{pairs,labels,splits,queries}.parquet` (hoặc thư mục shard cùng tên); `masked_pairs_dir/mask_<tỉ lệ>/pairs.parquet`.
Đầu ra (thư mục phải chưa tồn tại hoặc rỗng; không bao giờ ghi đè): predictions.parquet, metrics.json, experiments.json, handoff_G4.md.
Chạy offline (D9): chỉ dùng pyarrow, numpy, pandas, scikit-learn, lightgbm.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import warnings
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .contracts import validate_table
from .evaluate import evaluate_mrr_at_5
from .experiments import _Context, _git_commit, _versions, run_experiments
from .model import fit_ranker, predict_top5

INPUT_TABLES = ("pairs", "labels", "splits", "queries")
SCOPES = ("all_entries", "labeled_only")
MASK_DIR = re.compile(r"^mask_(\d+(?:\.\d+)?)$")
ESTIMATOR_OF = {"logistic_regression": "logreg", "lightgbm_ranker": "lgbm_ranker"}
T3_TESTS = (
    ("T3-1", "MRR@5 = 0.75 trên fixture; q1 một mình 0.5; entry chưa nhãn không vào mẫu số", "m3/tests/test_evaluate.py"),
    ("T3-2", "Lỗi khi test không nhãn / test thiếu dòng dự đoán; không lỗi khi train thiếu dòng", "m3/tests/test_evaluate.py"),
    ("T3-3", "Ít hơn 5 ứng viên, 0 ứng viên, đồng điểm theo ref_arxiv_id", "m3/tests/test_model.py"),
    ("T3-4", "Không rò rỉ: fit không phụ thuộc nhãn valid/test; cand thuộc references của bài", "m3/tests/test_model.py"),
    ("T3-5", "Train một lớp thì ValueError kèm số mẫu mỗi lớp", "m3/tests/test_model.py"),
    ("T3-6", "Chạy khi chặn socket; không import module mạng/NLP cấm", "m3/tests/test_offline.py"),
    ("T3-7", "Cùng seed thì cùng hệ số và predictions", "m3/tests/test_model.py"),
    ("T3-8", "experiments.json đầy đủ (baseline, LR, LGBM, ablation, mask, pool/thành viên, CI, phân bố ứng viên)",
     "m3/tests/test_experiments.py"),
    ("Tích hợp", "fixture pairs → labels → splits → fit → predict → evaluate = 0.75", "m3/tests/test_integration_tv3.py"),
    ("run_tv3", "run() end-to-end: 4 file, predictions hợp lệ, tất định, thư mục output không rỗng thì lỗi, chặn socket",
     "m3/tests/test_run_tv3.py"),
)


# ---------------------------------------------------------------------------------------------
# Đọc đầu vào
# ---------------------------------------------------------------------------------------------
def _locate(base: Path, name: str) -> Path | None:
    for cand in (base / f"{name}.parquet", base / name):
        if cand.is_file() or (cand.is_dir() and any(cand.glob("*.parquet"))):
            return cand
    return None


def _read_inputs(input_dir: Path) -> tuple[dict[str, pa.Table], dict[str, Path]]:
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Thiếu thư mục đầu vào: {input_dir}")
    found = {n: _locate(input_dir, n) for n in INPUT_TABLES}
    missing = [f"{n}.parquet" for n, p in found.items() if p is None]
    if missing:
        raise FileNotFoundError(f"Thiếu file đầu vào trong {input_dir}: {missing}")
    tables = {}
    for n, p in found.items():
        tables[n] = pq.read_table(p)
        validate_table(n, tables[n])
    return tables, found


def _read_masked(masked_dir: Path) -> dict[float, pa.Table]:
    if not masked_dir.is_dir():
        raise FileNotFoundError(f"Thiếu thư mục masked_pairs: {masked_dir}")
    out = {}
    for d in sorted(masked_dir.iterdir()):
        m = MASK_DIR.match(d.name)
        if not (d.is_dir() and m):
            continue
        p = _locate(d, "pairs")
        if p is None:
            raise FileNotFoundError(f"Thiếu {d / 'pairs.parquet'}")
        out[float(m.group(1))] = pq.read_table(p)
        validate_table("pairs", out[float(m.group(1))])
    if not out:
        raise FileNotFoundError(f"{masked_dir} không có thư mục mask_<tỉ lệ>/pairs.parquet nào")
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _dump(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------------------------
# Chọn model
# ---------------------------------------------------------------------------------------------
def choose_model(exp: dict) -> tuple[str, str]:
    """(tên model, lý do). LightGBM chỉ thắng khi MRR valid LỚN HƠN HẲN LR; hòa thì chọn LR vì đơn giản hơn."""
    lr, gb = exp["logistic_regression"], exp["lightgbm_ranker"]
    v_lr = lr["valid"]["mrr_at_5"]
    if "valid" not in gb:
        return "logistic_regression", f"LightGBM bị bỏ qua ({gb.get('reason')}); dùng logistic regression (MRR valid {v_lr:.4f})"
    v_gb = gb["valid"]["mrr_at_5"]
    if v_gb > v_lr:
        return "lightgbm_ranker", f"MRR valid: lightgbm_ranker {v_gb:.4f} > logistic_regression {v_lr:.4f}"
    rel = "=" if v_gb == v_lr else "<"
    return "logistic_regression", (f"MRR valid: lightgbm_ranker {v_gb:.4f} {rel} logistic_regression {v_lr:.4f}; "
                                   "hòa hoặc thấp hơn thì chọn logistic regression (đơn giản hơn)")


# ---------------------------------------------------------------------------------------------
# Hàm chính
# ---------------------------------------------------------------------------------------------
def run(*, input_dir, output_dir, seed: int, masked_pairs_dir=None, scope: str = "all_entries") -> dict:
    """Chạy toàn bộ và ghi 4 file vào `output_dir` (mới hoặc rỗng). Trả dict đường dẫn + metrics. Không ghi đè."""
    if scope not in SCOPES:
        raise ValueError(f"[predictions] scope '{scope}' không thuộc {list(SCOPES)}")
    input_dir, out_dir = Path(input_dir), Path(output_dir)
    if out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir())):
        raise FileExistsError(f"Thư mục đầu ra đã tồn tại và không rỗng, không ghi đè: {out_dir}")
    tables, input_paths = _read_inputs(input_dir)
    masked = _read_masked(Path(masked_pairs_dir)) if masked_pairs_dir is not None else None
    pairs, labels, splits, queries = (tables[n] for n in INPUT_TABLES)

    out_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(dir=out_dir.parent, prefix=".run_tv3_"))   # ghi vào thư mục tạm, xong mới đổi tên
    try:
        exp_path = run_experiments(pairs=pairs, labels=labels, splits=splits, seed=seed, output_dir=stage,
                                   masked_pairs=masked)
        exp = json.loads(exp_path.read_text(encoding="utf-8"))
        name, reason = choose_model(exp)
        chosen = exp[name]
        params = chosen["selection"]["grid"][chosen["selection"]["chosen_index"]]["params"]
        model = fit_ranker(pairs=pairs, labels=labels, splits=splits, seed=seed, estimator=ESTIMATOR_OF[name], params=params)
        if model.features_used_ != chosen["features"]:
            raise RuntimeError(f"fit lại cho đặc trưng khác experiments: {model.features_used_} vs {chosen['features']}")

        in_splits = {r["arxiv_id"] for r in splits.to_pylist()}
        dropped = sorted({a for a in queries.column("arxiv_id").to_pylist() if a not in in_splits})
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")                                  # đã ghi danh sách bài bị bỏ vào metrics.json
            preds = predict_top5(model, queries=queries, pairs=pairs, labels=labels, splits=splits, scope=scope)
        validate_table("predictions", preds)
        pred_path = stage / "predictions.parquet"
        pq.write_table(preds, pred_path)
        validate_table("predictions", pq.read_table(pred_path))               # file ghi ra cũng phải hợp lệ

        ctx = _Context(labels, splits)
        final = evaluate_mrr_at_5(predictions=preds, labels=labels, splits=splits, manual=ctx.manual)
        rep = chosen["test"]                                                  # kiểm chứng: predictions cuối = số của experiments
        if final.evaluated_entries != rep["entries"] or abs(final.mrr_at_5 - rep["mrr_at_5"]) > 1e-12:
            raise RuntimeError(f"MRR test từ predictions.parquet ({final.mrr_at_5}) khác experiments ({rep['mrr_at_5']})")

        metrics = {
            "seed": seed, "scope": scope,
            "selected_model": {"name": name, "estimator": ESTIMATOR_OF[name], "reason": reason,
                               "rule": "chọn theo MRR@5 valid; hòa thì logistic_regression",
                               "valid_mrr_at_5": {k: exp[k]["valid"]["mrr_at_5"] for k in ESTIMATOR_OF if "valid" in exp[k]}},
            "hyperparameters": chosen["hyperparameters"],
            "features_used": model.features_used_, "features_dropped_all_null": model.features_dropped_,
            "train_counts": model.train_counts_,
            "valid": chosen["valid"],
            "test": {k: rep[k] for k in ("mrr_at_5", "entries", "ci95", "manual", "auto")},
            "test_note": "chấm đúng một lần với model đã chọn trên valid; CI 95% là bootstrap theo entry (1000 lần)",
            "predictions": {"rows": preds.num_rows, "scope": scope, "papers_in_queries_not_in_splits": dropped},
            "inputs": {n: {"path": str(p), "rows": tables[n].num_rows} for n, p in input_paths.items()},
            "masked_pairs_levels": sorted(masked) if masked else None,
            "commit": _git_commit(), "versions": _versions()}
        _dump(stage / "metrics.json", metrics)

        final_files = {n: out_dir / n for n in ("predictions.parquet", "metrics.json", "experiments.json")}
        rows = {"predictions.parquet": preds.num_rows,
                "metrics.json": len((stage / "metrics.json").read_text(encoding="utf-8").splitlines()),
                "experiments.json": len(exp_path.read_text(encoding="utf-8").splitlines())}
        hashes = {n: _sha256(stage / n) for n in final_files}
        input_hashes = {n: (_sha256(p) if p.is_file() else None) for n, p in input_paths.items()}
        (stage / "handoff_G4.md").write_text(
            _handoff(out_dir, final_files, rows, hashes, input_paths, input_hashes, tables, seed, scope, masked_pairs_dir, input_dir),
            encoding="utf-8")
        os.chmod(stage, 0o755)
        os.replace(stage, out_dir)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return {"output_dir": str(out_dir), "predictions": str(out_dir / "predictions.parquet"),
            "metrics": str(out_dir / "metrics.json"), "experiments": str(out_dir / "experiments.json"),
            "handoff": str(out_dir / "handoff_G4.md"), "selected_model": name, "test_mrr_at_5": final.mrr_at_5,
            "metrics_content": metrics}


def _handoff(out_dir, files, rows, hashes, input_paths, input_hashes, tables, seed, scope, masked_dir, input_dir) -> str:
    cmd = f"python -m m3.src.run_tv3 --input {Path(input_dir).resolve()} --output {out_dir.resolve()} --seed {seed} --scope {scope}"
    if masked_dir is not None:
        cmd += f" --masked-pairs-dir {Path(masked_dir).resolve()}"
    lines = ["# Bàn giao G4 (TV3 → TV4): model, predictions, thí nghiệm", "",
             "File này do `m3/src/run_tv3.py` sinh tự động; không có số liệu nào được nhập tay.", "",
             "## Tham số chạy", "", f"- seed: `{seed}`", f"- predictions_scope: `{scope}`",
             f"- masked_pairs_dir: `{masked_dir if masked_dir is not None else 'không dùng (mục mask = skipped)'}`", "",
             "## File đầu ra", "", "| File | Đường dẫn | Số dòng | sha256 |", "|---|---|---|---|"]
    for n, p in files.items():
        unit = "dòng dữ liệu" if n.endswith(".parquet") else "dòng văn bản"
        lines.append(f"| {n} | `{p.resolve()}` | {rows[n]} ({unit}) | `{hashes[n]}` |")
    lines += ["", "`handoff_G4.md` (file này) không tự băm chính nó.", "", "## File đầu vào", "",
              "| Bảng | Đường dẫn | Số dòng | sha256 |", "|---|---|---|---|"]
    for n, p in input_paths.items():
        lines.append(f"| {n} | `{p.resolve()}` | {tables[n].num_rows} | `{input_hashes[n] or 'thư mục shard (không băm)'}` |")
    lines += ["", "## Lệnh tái tạo", "", "```", cmd, "```", "",
              "Cùng đầu vào, cùng seed, cùng phiên bản thư viện thì `predictions.parquet` giống hệt từng byte "
              "(kiểm tra trong `m3/tests/test_run_tv3.py`). `experiments.json` chỉ khác ở `metadata.run_at`.", "",
              "## Danh sách kiểm thử T3-x", "", "| Mã | Nội dung | File test |", "|---|---|---|"]
    lines += [f"| {c} | {d} | `{f}` |" for c, d, f in T3_TESTS]
    lines += ["", "Kết quả `pytest m3/tests -q` cho lần bàn giao này: TODO (chạy và dán output thật khi bàn giao).", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m m3.src.run_tv3", description=__doc__.split("\n")[0])
    p.add_argument("--input", required=True, help="thư mục chứa pairs, labels, splits, queries (.parquet)")
    p.add_argument("--output", required=True, help="thư mục đầu ra mới (hoặc rỗng); không ghi đè")
    p.add_argument("--seed", type=int, default=2026)
    p.add_argument("--masked-pairs-dir", default=None, help="thư mục chứa mask_<tỉ lệ>/pairs.parquet (tùy chọn)")
    p.add_argument("--scope", default="all_entries", choices=SCOPES)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        info = run(input_dir=args.input, output_dir=args.output, seed=args.seed,
                   masked_pairs_dir=args.masked_pairs_dir, scope=args.scope)
    except (FileExistsError, FileNotFoundError, ValueError) as e:      # lỗi đầu vào/đầu ra: báo gọn, mã thoát 1
        print(f"Lỗi ({type(e).__name__}): {e}", file=sys.stderr)
        return 1
    m = info["metrics_content"]
    print(f"Model: {info['selected_model']} ({m['selected_model']['reason']})")
    print(f"MRR@5 test = {m['test']['mrr_at_5']:.4f} trên {m['test']['entries']} entry "
          f"(CI95 {m['test']['ci95']['low']:.4f}–{m['test']['ci95']['high']:.4f})")
    print(f"Đã ghi: {info['output_dir']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
