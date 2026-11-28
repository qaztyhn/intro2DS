"""Bộ thí nghiệm của TV3: ghi `experiments.json` (D9: chạy offline, chỉ dùng pyarrow/numpy/pandas/sklearn/lightgbm).

Các thí nghiệm
  title_baseline       xếp hạng thẳng theo `title_similarity`, không huấn luyện.
  logistic_regression  C ∈ {0.01, 0.1, 1, 10} chọn trên VALID; test đánh giá đúng một lần với C đã chọn.
  lightgbm_ranker      LGBMRanker lambdarank (n_jobs=1, deterministic); lưới nhỏ 2x2 chọn trên VALID.
  ablation             bỏ từng nhóm đặc trưng (title/author/year/id/other), giữ C đã chọn ở logistic_regression.
  mask                 che url/DOI/arXiv ID 0/25/50/100%; cần `masked_pairs` (đặc trưng tính lại bởi TV1), nếu không thì skipped.
  candidate_distribution  số ứng viên mỗi bài test (min/median/max, histogram).
  random_baseline      MRR kỳ vọng của xếp hạng ngẫu nhiên: mỗi entry (1/n)·Σ_{k=1..min(5,n)} 1/k, lấy trung bình trên Q.

Q = entry có nhãn của bài thuộc partition đánh giá. Mọi MRR đi qua `evaluate.evaluate_mrr_at_5`; khoảng tin cậy 95% là
bootstrap theo entry (1000 lần, numpy.random.default_rng(seed), percentile 2.5/97.5). Sổ manual dựng từ labels
(bài có label_source == "manual").
"""
from __future__ import annotations

import datetime as dt
import json
import os
import platform
import subprocess
import tempfile
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import sklearn

from .contracts import MrrResult, make_table, validate_table
from .evaluate import evaluate_mrr_at_5, per_entry_reciprocal_rank
from .model import KEY_COLUMNS, TOP_K, fit_ranker, predict_top5

N_BOOT = 1000
LR_GRID = [{"C": c} for c in (0.01, 0.1, 1, 10)]
LGBM_GRID = [{"n_estimators": n, "num_leaves": leaves, "min_child_samples": 5} for n in (50, 100) for leaves in (4, 8)]
GROUPS = ("title", "author", "year", "id", "other")
HISTOGRAM_BINS = [("0", 0, 0), ("1-5", 1, 5), ("6-10", 6, 10), ("11-20", 11, 20), ("21-50", 21, 50),
                  ("51-100", 51, 100), ("101-200", 101, 200), ("201-500", 201, 500), ("501+", 501, None)]
SELECTION_RULE = "MRR@5 cao nhất trên valid; đồng điểm thì lấy cấu hình đứng trước trong lưới"


# ---------------------------------------------------------------------------------------------
# Tiện ích thuần
# ---------------------------------------------------------------------------------------------
def group_features(columns: list[str]) -> dict[str, str]:
    """Ánh xạ cột đặc trưng -> nhóm theo tên: title, author, year, id (arxiv_id/doi/url), còn lại là other."""
    out = {}
    for c in columns:
        low = c.lower()
        if "title" in low:
            out[c] = "title"
        elif "author" in low:
            out[c] = "author"
        elif "year" in low:
            out[c] = "year"
        elif any(t in low for t in ("arxiv_id", "doi", "url")):
            out[c] = "id"
        else:
            out[c] = "other"
    return out


def expected_random_rr(n: int, k: int = TOP_K) -> float:
    """MRR@k kỳ vọng khi groundtruth đứng đều ngẫu nhiên trong n ứng viên: (1/n)·Σ_{j=1..min(k,n)} 1/j. n=0 -> 0."""
    return sum(1.0 / j for j in range(1, min(k, n) + 1)) / n if n > 0 else 0.0


def bootstrap_ci(scores, seed: int, n_boot: int = N_BOOT) -> dict | None:
    """CI 95% (percentile) của trung bình, bootstrap theo entry. None nếu không có entry."""
    arr = np.asarray(scores, dtype=float)
    if arr.size == 0:
        return None
    rng = np.random.default_rng(seed)
    means = arr[rng.integers(0, arr.size, size=(n_boot, arr.size))].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return {"low": float(lo), "high": float(hi), "n_boot": n_boot, "level": 0.95}


def _git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent, capture_output=True,
                             text=True, timeout=10, check=True)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _versions() -> dict:
    out = {"python": platform.python_version(), "scikit-learn": sklearn.__version__, "pyarrow": pa.__version__,
           "numpy": np.__version__, "pandas": pd.__version__}
    try:
        import lightgbm
        out["lightgbm"] = lightgbm.__version__
    except ImportError:
        out["lightgbm"] = None
    return out


class ColumnScoreModel:
    """'Model' không huấn luyện: điểm = một cột đặc trưng (NaN xếp cuối). Tương thích `predict_top5`."""

    def __init__(self, column: str):
        self.features_used_ = [column]

    def decision_function(self, X: pd.DataFrame) -> np.ndarray:
        v = X.iloc[:, 0].to_numpy(dtype=float)
        return np.where(np.isnan(v), -np.inf, v)


def _subset(table: pa.Table, papers: set[str]) -> pa.Table:
    return table.filter(pc.is_in(table.column("arxiv_id"), value_set=pa.array(sorted(papers), pa.string())))


# ---------------------------------------------------------------------------------------------
# Ngữ cảnh dữ liệu
# ---------------------------------------------------------------------------------------------
class _Context:
    def __init__(self, labels: pa.Table, splits: pa.Table):
        self.labels, self.splits = labels, splits
        self.part_of = {r["arxiv_id"]: r["partition"] for r in splits.to_pylist()}
        self.papers = {p: {a for a, q in self.part_of.items() if q == p} for p in ("train", "valid", "test")}
        rows = labels.to_pylist()
        self.member_of = {(r["arxiv_id"], r["citation_key"]): r["member_id"] for r in rows}
        manual: dict[str, str] = {}
        for r in rows:
            if r["label_source"] == "manual":
                manual.setdefault(r["arxiv_id"], r["member_id"])
        self.manual_papers = set(manual)
        self.manual = make_table("manual_publications", [{"arxiv_id": a, "member_id": m} for a, m in sorted(manual.items())])
        self.labels_in, self.splits_in, self.splits_eval = {}, {}, {}
        for p in ("valid", "test"):
            self.labels_in[p] = _subset(labels, self.papers[p])
            self.splits_in[p] = _subset(splits, self.papers[p])
            # `evaluate_mrr_at_5` chỉ chấm partition test: với valid thì đánh dấu các bài valid là "test"
            self.splits_eval[p] = make_table("splits", [{"arxiv_id": a, "partition": "test"} for a in sorted(self.papers[p])])
            if self.labels_in[p].num_rows == 0:
                raise ValueError(f"[labels] không có entry có nhãn nào trong partition {p}")

    def evaluate(self, model, pairs: pa.Table, part: str) -> tuple[MrrResult, pa.Table]:
        """Dự đoán và chấm điểm CHỈ các entry có nhãn của partition `part`."""
        sub_pairs = _subset(pairs, self.papers[part])
        keys = {(r["arxiv_id"], r["citation_key"]) for r in self.labels_in[part].to_pylist()}
        keys |= set(zip(sub_pairs.column("arxiv_id").to_pylist(), sub_pairs.column("citation_key").to_pylist()))
        queries = make_table("queries", [{"arxiv_id": a, "citation_key": k} for a, k in sorted(keys)])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            preds = predict_top5(model, queries=queries, pairs=sub_pairs, labels=self.labels_in[part],
                                 splits=self.splits_in[part], scope="labeled_only")
        kw = dict(predictions=preds, labels=self.labels_in[part], splits=self.splits_eval[part])
        return (evaluate_mrr_at_5(manual=self.manual, **kw), per_entry_reciprocal_rank(**kw))

    def summarize(self, result: MrrResult, scores: pa.Table, seed: int) -> dict:
        a = scores.column("arxiv_id").to_pylist()
        k = scores.column("citation_key").to_pylist()
        s = np.asarray(scores.column("reciprocal_rank").to_pylist(), dtype=float)
        is_manual = np.array([x in self.manual_papers for x in a], dtype=bool)
        members = np.array([self.member_of[(x, y)] for x, y in zip(a, k)], dtype=object)

        def block(mask: np.ndarray) -> dict:
            sel = s[mask]
            return {"mrr_at_5": float(sel.mean()) if sel.size else None, "entries": int(sel.size),
                    "ci95": bootstrap_ci(sel, seed)}

        out = block(np.ones(s.size, dtype=bool))
        out["manual"], out["auto"] = block(is_manual), block(~is_manual)
        for blk, value, n in ((out, result.mrr_at_5, result.evaluated_entries),     # số chính thức lấy từ evaluate_mrr_at_5
                              (out["manual"], result.manual_mrr_at_5, result.manual_entries),
                              (out["auto"], result.auto_mrr_at_5, result.auto_entries)):
            if blk["entries"] != n or (value is not None and abs(blk["mrr_at_5"] - value) > 1e-9):
                raise RuntimeError(f"điểm từng entry không khớp evaluate_mrr_at_5: {blk} vs {value}, {n}")
            blk["mrr_at_5"] = value
        out["by_member"] = {str(m): block(members == m) for m in sorted(set(members.tolist()))}
        return out


# ---------------------------------------------------------------------------------------------
# Chạy từng thí nghiệm
# ---------------------------------------------------------------------------------------------
def _record(ctx: _Context, *, name, model, features, hyper, valid, test, selection, seed) -> dict:
    return {"model": name, "features": list(features), "hyperparameters": hyper,
            "selection": selection,
            "valid": {"mrr_at_5": valid[0].mrr_at_5, "evaluated_entries": valid[0].evaluated_entries},
            "test": ctx.summarize(*test, seed)}


def _fit_select(ctx: _Context, pairs: pa.Table, *, estimator: str, grid: list[dict], features, seed: int, name: str) -> tuple[dict, object]:
    best = None
    tried = []
    for i, params in enumerate(grid):
        model = fit_ranker(pairs=pairs, labels=ctx.labels, splits=ctx.splits, seed=seed, features=features,
                           estimator=estimator, params=params)
        res, scores = ctx.evaluate(model, pairs, "valid")
        tried.append({"params": params, "valid_mrr_at_5": res.mrr_at_5, "valid_entries": res.evaluated_entries})
        if best is None or res.mrr_at_5 > best[0].mrr_at_5:
            best = (res, scores, model, i)
    valid_res, _, model, idx = best
    test = ctx.evaluate(model, pairs, "test")                 # test chỉ được chấm một lần, với cấu hình đã chọn
    if estimator == "logreg":
        clf = model.named_steps["clf"]
        hyper = {"C": clf.C, "class_weight": clf.class_weight, "max_iter": clf.max_iter, "random_state": seed}
    else:
        p = model.named_steps["ranker"].get_params()
        hyper = {k: p[k] for k in ("objective", "n_estimators", "num_leaves", "min_child_samples", "learning_rate",
                                   "random_state", "n_jobs", "deterministic")}
    rec = _record(ctx, name=name, model=model, features=model.features_used_, hyper=hyper, valid=(valid_res,), test=test,
                  selection={"rule": SELECTION_RULE, "grid": tried, "chosen_index": idx}, seed=seed)
    rec["features_dropped_all_null"] = list(model.features_dropped_)
    rec["train_counts"] = dict(model.train_counts_)
    return rec, model


def _title_baseline(ctx: _Context, pairs: pa.Table, seed: int) -> dict:
    if "title_similarity" not in pairs.schema.names:
        return {"status": "skipped", "reason": "pairs không có cột title_similarity"}
    model = ColumnScoreModel("title_similarity")
    valid, test = ctx.evaluate(model, pairs, "valid"), ctx.evaluate(model, pairs, "test")
    return _record(ctx, name="title_similarity_ranking", model=model, features=model.features_used_,
                   hyper={}, valid=valid, test=test, selection=None, seed=seed)


def _ablation(ctx: _Context, pairs: pa.Table, lr_model, lr_rec: dict, seed: int) -> dict:
    used = lr_model.features_used_
    mapping = group_features(used + lr_model.features_dropped_)
    runs = {}
    C = lr_rec["hyperparameters"]["C"]
    for g in GROUPS:
        cols = [c for c in used if mapping[c] == g]
        if not cols:
            continue
        keep = [c for c in used if mapping[c] != g]
        key = f"drop_{g}"
        if not keep:
            runs[key] = {"status": "skipped", "reason": f"bỏ nhóm {g} thì không còn đặc trưng nào", "dropped_columns": cols}
            continue
        try:
            rec, _ = _fit_select(ctx, pairs, estimator="logreg", grid=[{"C": C}], features=keep, seed=seed,
                                 name=f"logistic_regression_without_{g}")
        except ValueError as e:                                # vd. phần còn lại toàn null hoặc mất một lớp
            runs[key] = {"status": "skipped", "reason": str(e), "dropped_columns": cols}
            continue
        rec["dropped_group"], rec["dropped_columns"] = g, cols
        runs[key] = rec
    return {"feature_groups": mapping, "group_rule": "tên cột chứa title/author/year/(arxiv_id|doi|url); còn lại là other",
            "fixed_C": C, "reference": "logistic_regression (đủ đặc trưng)", "runs": runs}


def _mask(ctx: _Context, masked_pairs, seed: int) -> dict:
    if not masked_pairs:
        return {"status": "skipped",
                "reason": "run_experiments không nhận masked_pairs: che url/DOI/arXiv ID cần tính lại đặc trưng "
                          "(MaskConfig + build_pair_batch của TV1); xem CONTRACT_PROPOSALS_TV3.md"}
    levels = {}
    for frac in sorted(masked_pairs):
        if not isinstance(frac, (int, float)) or isinstance(frac, bool) or not 0.0 <= frac <= 1.0:
            raise ValueError(f"[masked_pairs] tỉ lệ che phải là số trong [0, 1], nhận {frac!r}")
        validate_table("pairs", masked_pairs[frac])
        rec, _ = _fit_select(ctx, masked_pairs[frac], estimator="logreg", grid=LR_GRID, features=None, seed=seed,
                             name=f"logistic_regression_mask_{frac:g}")
        rec["mask_fraction"] = float(frac)
        levels[f"{frac:g}"] = rec
    wanted = (0.0, 0.25, 0.5, 1.0)
    return {"status": "ok", "model": "logistic_regression", "applied_to": "train và đánh giá (cùng bảng pairs đã che)",
            "levels": levels, "missing_levels": [w for w in wanted if f"{w:g}" not in levels]}


def _candidate_distribution(ctx: _Context, pairs: pa.Table) -> dict:
    refs: dict[str, set] = {a: set() for a in ctx.papers["test"]}
    for a, r in zip(pairs.column("arxiv_id").to_pylist(), pairs.column("ref_arxiv_id").to_pylist()):
        if a in refs:
            refs[a].add(r)
    counts = {a: len(v) for a, v in sorted(refs.items())}
    arr = np.array(list(counts.values()), dtype=float)
    hist = {label: sum(1 for c in counts.values() if c >= lo and (hi is None or c <= hi)) for label, lo, hi in HISTOGRAM_BINS}
    return {"scope": "bài có partition test; số ứng viên = số ref_arxiv_id khác nhau trong pairs của bài",
            "papers": len(counts), "min": int(arr.min()) if arr.size else None,
            "median": float(np.median(arr)) if arr.size else None, "max": int(arr.max()) if arr.size else None,
            "histogram": hist, "per_paper": counts}


def _random_baseline(ctx: _Context, pairs: pa.Table) -> dict:
    n_of: dict[tuple, int] = {}
    for a, k in zip(pairs.column("arxiv_id").to_pylist(), pairs.column("citation_key").to_pylist()):
        n_of[(a, k)] = n_of.get((a, k), 0) + 1
    out = {"definition": "mỗi entry: (1/n)·Σ_{k=1..min(5,n)} 1/k với n = số ứng viên của entry; entry 0 ứng viên = 0; "
                         "trung bình trên Q (entry có nhãn của partition), không phải trên bài"}
    for part in ("valid", "test"):
        rows = [(r["arxiv_id"], expected_random_rr(n_of.get((r["arxiv_id"], r["citation_key"]), 0)))
                for r in ctx.labels_in[part].to_pylist()]
        man = [v for a, v in rows if a in ctx.manual_papers]
        aut = [v for a, v in rows if a not in ctx.manual_papers]
        mean = lambda xs: float(np.mean(xs)) if xs else None
        out[part] = {"mrr_at_5": mean([v for _, v in rows]), "entries": len(rows),
                     "manual": {"mrr_at_5": mean(man), "entries": len(man)},
                     "auto": {"mrr_at_5": mean(aut), "entries": len(aut)}}
    return out


def _data_summary(ctx: _Context) -> dict:
    rows = ctx.labels.to_pylist()
    out = {"papers_by_partition": {p: len(v) for p, v in ctx.papers.items()}}
    out["labeled_entries_by_partition"] = {
        p: sum(1 for r in rows if ctx.part_of.get(r["arxiv_id"]) == p) for p in ("train", "valid", "test")}
    out["labeled_entries_by_partition_and_pool"] = {
        p: {s: sum(1 for r in rows if ctx.part_of.get(r["arxiv_id"]) == p and r["label_source"] == s)
            for s in ("manual", "auto")} for p in ("train", "valid", "test")}
    return out


def run_experiments(*, pairs: pa.Table, labels: pa.Table, splits: pa.Table, seed: int, output_dir,
                    masked_pairs: dict[float, pa.Table] | None = None) -> Path:
    """Chạy toàn bộ thí nghiệm và ghi `<output_dir>/experiments.json`; trả đường dẫn file.

    `masked_pairs`: {tỉ lệ che: bảng pairs đã tính lại đặc trưng với che}. Không truyền thì mục `mask` là skipped.
    Không sửa dữ liệu đầu vào.
    """
    for name, t in (("pairs", pairs), ("labels", labels), ("splits", splits)):
        validate_table(name, t)
    ctx = _Context(labels, splits)
    exps: dict = {"title_baseline": _title_baseline(ctx, pairs, seed)}
    lr_rec, lr_model = _fit_select(ctx, pairs, estimator="logreg", grid=LR_GRID, features=None, seed=seed,
                                   name="logistic_regression")
    exps["logistic_regression"] = lr_rec
    try:
        exps["lightgbm_ranker"] = _fit_select(ctx, pairs, estimator="lgbm_ranker", grid=LGBM_GRID, features=None,
                                              seed=seed, name="lightgbm_lambdarank")[0]
    except ImportError as e:
        exps["lightgbm_ranker"] = {"status": "skipped", "reason": f"không import được lightgbm: {e}"}
    exps["ablation"] = _ablation(ctx, pairs, lr_model, lr_rec, seed)
    exps["mask"] = _mask(ctx, masked_pairs, seed)
    result = {
        "metadata": {"seed": seed, "commit": _git_commit(), "versions": _versions(), "n_bootstrap": N_BOOT,
                     "run_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")},
        "data_summary": _data_summary(ctx),
        "random_baseline": _random_baseline(ctx, pairs),
        "candidate_distribution": _candidate_distribution(ctx, pairs),
        **exps,
    }
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "experiments.json"
    text = json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
    fd, tmp = tempfile.mkstemp(dir=out_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path
