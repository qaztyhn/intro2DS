"""Model xếp hạng và dự đoán top-5 cho reference matching (TV3).

- `fit_ranker`: chỉ học từ bài partition `train`; mặc định Pipeline(SimpleImputer, StandardScaler,
  LogisticRegression(class_weight="balanced")). `estimator="lgbm_ranker"` chọn LightGBM lambdarank.
- `predict_top5`: chấm điểm mọi cặp, CHỈ trong ứng viên của chính bài đó (D8), xếp (score giảm, ref_arxiv_id tăng),
  chỉ nối `groundtruth_arxiv_id` và `partition` SAU khi đã xếp hạng.

Chạy offline (D9): chỉ dùng pyarrow, numpy, pandas, scikit-learn, lightgbm. Không sửa dữ liệu đầu vào.
Lỗi dữ liệu là ValueError kèm tên bảng và khóa.
"""
from __future__ import annotations

import warnings
from collections import defaultdict

import numpy as np
import pandas as pd
import pyarrow as pa
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from .contracts import SCHEMAS, validate_table

KEY_COLUMNS = ("arxiv_id", "citation_key", "ref_arxiv_id")
# Cột chứa thông tin nhãn/phân chia: tuyệt đối không được làm đặc trưng.
FORBIDDEN_FEATURES = frozenset({"groundtruth_arxiv_id", "label_source", "member_id", "partition"})
ESTIMATORS = ("logreg", "lgbm_ranker")
SCOPES = ("all_entries", "labeled_only")
TOP_K = 5


# ---------------------------------------------------------------------------------------------
# Đặc trưng
# ---------------------------------------------------------------------------------------------
def _is_numeric(t: pa.DataType) -> bool:
    return pa.types.is_boolean(t) or pa.types.is_integer(t) or pa.types.is_floating(t)


def _as_float(col: pa.ChunkedArray) -> np.ndarray:
    """bool/int/float -> float64, null -> NaN."""
    return col.cast(pa.float64()).to_numpy()


def _feature_frame(pairs: pa.Table, names: list[str]) -> pd.DataFrame:
    return pd.DataFrame({n: _as_float(pairs.column(n)) for n in names})


def _candidate_features(pairs: pa.Table, features: list[str] | None) -> tuple[list[str], list[str]]:
    """(cột ứng viên, cột bị bỏ vì không phải số). Kiểm tra cột cấm và cột không tồn tại."""
    names = pairs.schema.names
    if features is None:
        banned = [n for n in names if n in FORBIDDEN_FEATURES]
        if banned:
            raise ValueError(f"[pairs] chứa cột cấm làm đặc trưng: {banned}")
        rest = [n for n in names if n not in KEY_COLUMNS]
        numeric = [n for n in rest if _is_numeric(pairs.schema.field(n).type)]
        return numeric, [n for n in rest if n not in numeric]
    features = list(features)
    banned = [n for n in features if n in FORBIDDEN_FEATURES or n in KEY_COLUMNS]
    if banned:
        raise ValueError(f"[features] cột cấm hoặc cột khóa không được làm đặc trưng: {banned}")
    if len(set(features)) != len(features):
        raise ValueError(f"[features] có cột lặp: {features}")
    missing = [n for n in features if n not in names]
    if missing:
        raise ValueError(f"[pairs] thiếu cột đặc trưng yêu cầu: {missing}")
    bad = [n for n in features if not _is_numeric(pairs.schema.field(n).type)]
    if bad:
        raise ValueError(f"[features] cột không phải số/bool: {bad}")
    return features, []


# ---------------------------------------------------------------------------------------------
# Huấn luyện
# ---------------------------------------------------------------------------------------------
def _training_rows(pairs: pa.Table, labels: pa.Table, splits: pa.Table) -> tuple[pa.Table, np.ndarray, list[int]]:
    """Bảng pairs đã sắp theo khóa, y (0/1) và kích thước nhóm theo entry. Chỉ bài train, chỉ entry có nhãn."""
    train = {r["arxiv_id"] for r in splits.to_pylist() if r["partition"] == "train"}
    gt = {(r["arxiv_id"], r["citation_key"]): r["groundtruth_arxiv_id"]
          for r in labels.to_pylist() if r["arxiv_id"] in train}
    keys = list(zip(*(pairs.column(c).to_pylist() for c in KEY_COLUMNS)))
    chosen = sorted((i for i, k in enumerate(keys) if (k[0], k[1]) in gt), key=lambda i: keys[i])
    refs_of: dict[tuple, set] = defaultdict(set)
    for i in chosen:
        refs_of[keys[i][:2]].add(keys[i][2])
    for entry in sorted(gt):                                    # nhãn phải trỏ tới ứng viên có thật của entry
        if gt[entry] not in refs_of[entry]:
            raise ValueError(f"[labels] entry {entry}: groundtruth '{gt[entry]}' không có trong pairs")
    y = np.array([int(keys[i][2] == gt[keys[i][:2]]) for i in chosen], dtype=int)
    groups: list[int] = []
    prev = None
    for i in chosen:
        if keys[i][:2] == prev:
            groups[-1] += 1
        else:
            groups.append(1)
            prev = keys[i][:2]
    return pairs.take(pa.array(chosen, type=pa.int64())), y, groups


def fit_ranker(*, pairs: pa.Table, labels: pa.Table, splits: pa.Table, seed: int,
               features: list[str] | None = None, estimator: str = "logreg",
               params: dict | None = None) -> Pipeline:
    """Huấn luyện chỉ trên bài `train`. `params` truyền cho estimator (vd. {"C": 1.0} hoặc tham số LGBMRanker).

    Model trả về là Pipeline đã fit, kèm thuộc tính: `features_used_`, `features_dropped_` (toàn null trên tập train),
    `features_skipped_nonnumeric_`, `estimator_name_`, `seed_`, `train_counts_`.
    """
    if estimator not in ESTIMATORS:
        raise ValueError(f"[model] estimator '{estimator}' không thuộc {list(ESTIMATORS)}")
    for name, t in (("pairs", pairs), ("labels", labels), ("splits", splits)):
        validate_table(name, t)
    candidates, skipped = _candidate_features(pairs, features)
    table, y, groups = _training_rows(pairs, labels, splits)
    n_pos, n_neg = int(y.sum()), int(len(y) - y.sum())
    if n_pos == 0 or n_neg == 0:
        raise ValueError(f"[train] thiếu một lớp: {n_pos} mẫu dương, {n_neg} mẫu âm "
                         f"({len(groups)} entry có nhãn trong bài train)")
    dropped = [n for n in candidates if table.column(n).null_count == table.num_rows]
    used = [n for n in candidates if n not in dropped]
    if not used:
        raise ValueError(f"[pairs] không còn đặc trưng nào để huấn luyện; cột ứng viên={candidates}, "
                         f"bị bỏ vì toàn null={dropped}")
    X = _feature_frame(table, used)
    params = dict(params or {})
    if estimator == "logreg":
        model = Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            ("clf", LogisticRegression(**{"class_weight": "balanced", "max_iter": 1000,
                                          "random_state": seed, **params}))])
        model.fit(X, y)
    else:
        from lightgbm import LGBMRanker                      # import muộn: logreg không cần lightgbm
        model = Pipeline([("ranker", LGBMRanker(**{"objective": "lambdarank", "random_state": seed, "n_jobs": 1,
                                                   "deterministic": True, "force_row_wise": True,
                                                   "verbose": -1, **params}))])
        model.fit(X, y, ranker__group=groups)
    model.features_used_ = list(used)
    model.features_dropped_ = list(dropped)
    model.features_skipped_nonnumeric_ = list(skipped)
    model.estimator_name_ = estimator
    model.seed_ = seed
    model.train_counts_ = {"positive": n_pos, "negative": n_neg, "entries": len(groups)}
    return model


def feature_names(model: Pipeline) -> list[str]:
    names = getattr(model, "features_used_", None)
    if names is None:
        names = getattr(model, "feature_names_in_", None)
    if names is None:
        raise ValueError("[model] không có danh sách đặc trưng đã dùng (features_used_ / feature_names_in_)")
    return list(names)


def score_pairs(model: Pipeline, pairs: pa.Table) -> np.ndarray:
    """Điểm cho từng dòng pairs, chỉ từ các cột đặc trưng của model. Điểm càng cao càng có khả năng là cặp đúng."""
    names = feature_names(model)
    missing = [n for n in names if n not in pairs.schema.names]
    if missing:
        raise ValueError(f"[pairs] thiếu cột đặc trưng mà model đã dùng: {missing}")
    if pairs.num_rows == 0:
        return np.empty(0)
    X = _feature_frame(pairs, names)
    scores = model.decision_function(X) if hasattr(model, "decision_function") else model.predict(X)
    return np.asarray(scores, dtype=float)


# ---------------------------------------------------------------------------------------------
# Dự đoán
# ---------------------------------------------------------------------------------------------
def rank_candidates(pairs: pa.Table, scores: np.ndarray, k: int = TOP_K) -> dict[tuple[str, str], list[str]]:
    """Top-k ref_arxiv_id của từng entry: (score giảm, ref_arxiv_id tăng), bỏ ứng viên trùng. NaN xếp cuối."""
    per_entry: dict[tuple[str, str], list[tuple[float, str]]] = defaultdict(list)
    cols = [pairs.column(c).to_pylist() for c in KEY_COLUMNS]
    for a, key, ref, s in zip(*cols, scores.tolist()):
        per_entry[(a, key)].append((-np.inf if s != s else -s, ref))
    ranked: dict[tuple[str, str], list[str]] = {}
    for entry, items in per_entry.items():
        out: list[str] = []
        for _, ref in sorted(items):
            if ref not in out:
                out.append(ref)
            if len(out) == k:
                break
        ranked[entry] = out
    return ranked


def predict_top5(model: Pipeline, *, queries: pa.Table, pairs: pa.Table, labels: pa.Table,
                 splits: pa.Table, scope: str = "all_entries") -> pa.Table:
    """Bảng predictions theo `SCHEMAS["predictions"]`, sắp theo (arxiv_id, citation_key).

    scope="all_entries": mọi entry của bài có trong `splits`; "labeled_only": chỉ entry có trong `labels`.
    Bài có trong `queries` nhưng không có trong `splits` bị bỏ và ghi warning (contract chưa quy định).
    """
    if scope not in SCOPES:
        raise ValueError(f"[predictions] scope '{scope}' không thuộc {list(SCOPES)}")
    for name, t in (("queries", queries), ("pairs", pairs), ("labels", labels), ("splits", splits)):
        validate_table(name, t)
    query_keys = set(zip(queries.column("arxiv_id").to_pylist(), queries.column("citation_key").to_pylist()))
    for a, k, r in zip(*(pairs.column(c).to_pylist() for c in KEY_COLUMNS)):
        if (a, k) not in query_keys:
            raise ValueError(f"[pairs] entry {(a, k)} (ứng viên '{r}') không có trong queries")
    ranked = rank_candidates(pairs, score_pairs(model, pairs))     # chỉ dùng đặc trưng; chưa chạm nhãn/partition
    # Từ đây mới nối partition và groundtruth.
    partition = {r["arxiv_id"]: r["partition"] for r in splits.to_pylist()}
    truth = {(r["arxiv_id"], r["citation_key"]): r["groundtruth_arxiv_id"] for r in labels.to_pylist()}
    dropped = sorted({a for a, _ in query_keys if a not in partition})
    if dropped:
        warnings.warn(f"[predictions] bỏ {len(dropped)} bài không có trong splits: {dropped}", stacklevel=2)
    rows = []
    for a, k in sorted(query_keys):
        if a not in partition or (scope == "labeled_only" and (a, k) not in truth):
            continue
        cands = ranked.get((a, k), [])
        rows.append({"arxiv_id": a, "partition": partition[a], "citation_key": k,
                     "groundtruth_arxiv_id": truth.get((a, k)),
                     **{f"cand_{i + 1}": (cands[i] if i < len(cands) else None) for i in range(TOP_K)}})
    out = (pa.Table.from_pylist(rows, schema=SCHEMAS["predictions"]) if rows
           else SCHEMAS["predictions"].empty_table())
    validate_table("predictions", out)
    return out
