"""MRR@5 cho bài toán reference matching (TV3).

Quy ước (khớp `contracts.evaluate_mrr_at_5`, nguồn chân lý):
- Q = các entry CÓ NHÃN thuộc bài có partition `test`. Entry chưa gán nhãn không nằm trong Q.
- Bộ lọc test được áp dụng TRƯỚC khi tra dòng dự đoán; `predictions` có thể chứa mọi partition.
- Entry test có nhãn mà thiếu dòng dự đoán -> ValueError kèm khóa. Entry train/valid thiếu dòng -> bỏ qua.
- Điểm entry = 1/rank nếu groundtruth nằm trong cand_1..cand_5, ngược lại 0.
- `manual=None` -> các trường manual/auto của MrrResult là None và 0 (không suy ra từ label_source).

Module chỉ dùng pyarrow; không đọc/ghi file, không dùng mạng. Không sửa dữ liệu đầu vào.
"""
from __future__ import annotations

import pyarrow as pa

from .contracts import MrrResult, validate_table

CAND_COLUMNS = tuple(f"cand_{i}" for i in range(1, 6))

PER_ENTRY_SCHEMA = pa.schema([
    pa.field("arxiv_id", pa.string(), nullable=False),
    pa.field("citation_key", pa.string(), nullable=False),
    pa.field("rank", pa.int32()),                                   # null nếu groundtruth không nằm trong top 5
    pa.field("reciprocal_rank", pa.float64(), nullable=False),
])


def per_entry_reciprocal_rank(*, predictions: pa.Table, labels: pa.Table, splits: pa.Table) -> pa.Table:
    """Điểm 1/rank của từng entry thuộc Q (entry có nhãn, bài partition test).

    Thứ tự dòng kết quả = thứ tự dòng của `labels` (đã lọc test), để tổng cộng dồn cho ra đúng từng bit
    như `contracts.evaluate_mrr_at_5`. Q rỗng cho bảng rỗng (đủ schema); lỗi chặn Q rỗng nằm ở
    `evaluate_mrr_at_5`. Dùng cho bootstrap theo entry ở experiments.
    """
    validate_table("labels", labels)
    validate_table("splits", splits)
    validate_table("predictions", predictions)
    pred = {(r["arxiv_id"], r["citation_key"]): r for r in predictions.to_pylist()}
    part = {r["arxiv_id"]: r["partition"] for r in splits.to_pylist()}
    rows = []
    for lab in labels.to_pylist():
        a, k = lab["arxiv_id"], lab["citation_key"]
        if part.get(a) != "test":                                   # lọc test TRƯỚC khi tra dự đoán
            continue
        row = pred.get((a, k))
        if row is None:
            raise ValueError(f"[predictions] entry có nhãn {(a, k)} không có dòng dự đoán")
        cands = [row.get(c) for c in CAND_COLUMNS]
        gt = lab["groundtruth_arxiv_id"]
        rank = cands.index(gt) + 1 if gt in cands else None
        rows.append({"arxiv_id": a, "citation_key": k, "rank": rank,
                     "reciprocal_rank": 1.0 / rank if rank is not None else 0.0})
    if not rows:
        return PER_ENTRY_SCHEMA.empty_table()
    return pa.Table.from_pylist(rows, schema=PER_ENTRY_SCHEMA)


def evaluate_mrr_at_5(*, predictions: pa.Table, labels: pa.Table, splits: pa.Table,
                      manual: pa.Table | None = None) -> MrrResult:
    """MRR@5 chung và tách manual/auto (chỉ khi truyền `manual`: sổ đăng ký manual_publications)."""
    scores = per_entry_reciprocal_rank(predictions=predictions, labels=labels, splits=splits)
    if scores.num_rows == 0:
        raise ValueError("[predictions] không có entry test nào có nhãn để đánh giá")
    man = None
    if manual is not None:
        validate_table("manual_publications", manual)
        man = set(manual.column("arxiv_id").to_pylist())
    tot: dict[str, float] = {"all": 0.0, "manual": 0.0, "auto": 0.0}
    cnt: dict[str, int] = {"all": 0, "manual": 0, "auto": 0}
    for a, s in zip(scores.column("arxiv_id").to_pylist(), scores.column("reciprocal_rank").to_pylist()):
        for p in ("all",) + (() if man is None else ("manual" if a in man else "auto",)):
            tot[p] += s                                             # cộng tuần tự theo thứ tự labels (giống contract)
            cnt[p] += 1

    def mean(p: str) -> float | None:
        return tot[p] / cnt[p] if cnt[p] else None

    return MrrResult(tot["all"] / cnt["all"], cnt["all"], mean("manual"), cnt["manual"], mean("auto"), cnt["auto"])
