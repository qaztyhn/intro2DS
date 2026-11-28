"""Xuất dataset Hugging Face (6 bảng Parquet) cho MỘT lát cắt publication_ids."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import re
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from m3.src.contracts import HF_TABLES, SCHEMAS, validate_table

STR = pa.string()

# Schema theo parsing.md (contract chưa có hai bảng này)
EXTRA_SCHEMAS = {
    "elements": pa.schema([
        pa.field("element_id", STR, nullable=False), pa.field("arxiv_id", STR, nullable=False),
        pa.field("kind", pa.dictionary(pa.int32(), STR)), pa.field("content", STR)]),
    "hierarchy": pa.schema([
        pa.field("arxiv_id", STR, nullable=False), pa.field("version", pa.int32()),
        pa.field("element_id", STR, nullable=False), pa.field("parent_id", STR, nullable=False)]),
}
HF_SCHEMAS = {t: EXTRA_SCHEMAS.get(t) or SCHEMAS[t] for t in HF_TABLES}

SORT_KEYS = {
    "papers": ["arxiv_id"], "references": ["arxiv_id", "ref_arxiv_id"],
    "elements": ["arxiv_id", "element_id"], "hierarchy": ["arxiv_id", "version", "element_id"],
    "bibtex": ["arxiv_id", "citation_key"], "predictions": ["arxiv_id", "citation_key"],
}


@dataclass
class DatasetInputs:
    """Các bảng đầu vào đã nạp sẵn dưới dạng pa.Table."""
    papers: pa.Table
    references: pa.Table
    elements: pa.Table
    hierarchy: pa.Table
    bibtex: pa.Table
    splits: pa.Table                     # split chung toàn nhóm (bảng nội bộ, không xuất)
    labels: pa.Table                     # chỉ dùng cho predictions_scope="labeled_only"
    predictions: pa.Table | None = None  # None -> xuất predictions rỗng đúng schema


@dataclass
class ExportResult:
    output_dir: Path
    row_counts: dict[str, int]
    shard_counts: dict[str, int]


def _filter_by_pub(t: pa.Table, ids: Iterable[str]) -> pa.Table:
    mask = pc.is_in(t["arxiv_id"], value_set=pa.array(sorted(set(ids)), STR))
    return t.filter(mask)


def _finalize(name: str, t: pa.Table) -> pa.Table:
    schema = HF_SCHEMAS[name]
    t = t.select(schema.names).cast(schema)
    t = t.sort_by([(c, "ascending") for c in SORT_KEYS[name]])
    if name in SCHEMAS:
        validate_table(name, t)
    return t


def _predictions(inputs: DatasetInputs, ids, scope: str) -> pa.Table:
    if scope not in {"all_entries", "labeled_only"}:
        raise ValueError(f"[predictions] predictions_scope không hợp lệ: {scope}")
    t = inputs.predictions if inputs.predictions is not None else SCHEMAS["predictions"].empty_table()
    t = _filter_by_pub(t, ids)
    # partition lấy từ splits chung; bài không có trong splits thì không có dòng predictions
    part = {r["arxiv_id"]: r["partition"] for r in inputs.splits.to_pylist()}
    arx = t["arxiv_id"].to_pylist()
    keep = pa.array([a in part for a in arx], pa.bool_())
    t = t.filter(keep)
    arx = t["arxiv_id"].to_pylist()
    idx = t.schema.get_field_index("partition")
    t = t.set_column(idx, "partition", pa.array([part[a] for a in arx], STR))
    if scope == "labeled_only":
        lab = {(r["arxiv_id"], r["citation_key"]) for r in inputs.labels.to_pylist()}
        keys = zip(t["arxiv_id"].to_pylist(), t["citation_key"].to_pylist())
        t = t.filter(pa.array([k in lab for k in keys], pa.bool_()))
    return t


def _write_shards(table: pa.Table, schema: pa.Schema, out: Path, rows_per_shard: int) -> int:
    out.mkdir(parents=True, exist_ok=True)
    if table.num_rows == 0:
        pq.write_table(schema.empty_table(), out / "part-00000.parquet")
        return 1
    n = 0
    for start in range(0, table.num_rows, rows_per_shard):
        pq.write_table(table.slice(start, rows_per_shard), out / f"part-{n:05d}.parquet")
        n += 1
    return n


def _dataset_card() -> str:
    cfgs = "\n".join(
        f"- config_name: {t}\n  data_files:\n  - split: train\n    path: data/{t}/*.parquet" for t in HF_TABLES)
    return f"---\nconfigs:\n{cfgs}\n---\n\n# arXiv reference matching dataset\n\n" \
           "Sharded Parquet tables: " + ", ".join(HF_TABLES) + ".\n"


def export_dataset(inputs: DatasetInputs, *, output_dir, publication_ids,
                   rows_per_shard: int = 100_000, predictions_scope: str = "all_entries") -> ExportResult:
    out = Path(output_dir)
    if out.exists():
        raise FileExistsError(f"Thư mục đích phải mới, đã tồn tại: {out}")
    ids = sorted(set(publication_ids))
    tables = {
        "papers": inputs.papers, "references": inputs.references, "elements": inputs.elements,
        "hierarchy": inputs.hierarchy, "bibtex": inputs.bibtex,
        "predictions": _predictions(inputs, ids, predictions_scope),
    }
    final = {}
    for name in HF_TABLES:
        t = tables[name] if name == "predictions" else _filter_by_pub(tables[name], ids)
        final[name] = _finalize(name, t)
    shards = {name: _write_shards(final[name], HF_SCHEMAS[name], out / "data" / name, rows_per_shard)
              for name in HF_TABLES}
    (out / "README.md").write_text(_dataset_card(), encoding="utf-8")
    return ExportResult(out, {n: t.num_rows for n, t in final.items()}, shards)


def _read_table(root: Path, name: str) -> tuple[pa.Table, int]:
    """Đọc mọi shard của một bảng; trả về (bảng, tổng dòng theo metadata từng shard)."""
    files = sorted((root / "data" / name).glob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"[{name}] không có shard parquet nào trong {root / 'data' / name}")
    meta_rows = sum(pq.ParquetFile(f).metadata.num_rows for f in files)
    return pa.concat_tables([pq.read_table(f) for f in files]), meta_rows


def check_dataset(output_dir, *, publication_ids) -> dict:
    """Kiểm tra dataset đã xuất. Trả về {'ok': bool, 'errors': [...], 'row_counts': {...}}."""
    root, ids = Path(output_dir), set(publication_ids)
    errors: list[str] = []
    T: dict[str, pa.Table] = {}
    counts: dict[str, int] = {}

    # 1. đọc 6 bảng, schema, tổng dòng shard
    for name in HF_TABLES:
        t, meta_rows = _read_table(root, name)
        T[name], counts[name] = t, t.num_rows
        if t.schema.names != HF_SCHEMAS[name].names or any(
                t.schema.field(n).type != HF_SCHEMAS[name].field(n).type for n in t.schema.names):
            errors.append(f"[{name}] sai schema: {t.schema}")
        if meta_rows != t.num_rows:
            errors.append(f"[{name}] tổng dòng shard {meta_rows} khác số dòng bảng {t.num_rows}")
        if not set(t["arxiv_id"].to_pylist()) <= ids:
            extra = sorted(set(t["arxiv_id"].to_pylist()) - ids)[:5]
            errors.append(f"[{name}] có arxiv_id ngoài publication_ids: {extra}")

    # 2. không có bảng nội bộ, không có script nạp, README đúng 6 config
    present = {p.name for p in (root / "data").iterdir()} if (root / "data").exists() else set()
    if present != set(HF_TABLES):
        errors.append(f"thư mục data/ phải có đúng {sorted(HF_TABLES)}, nhận {sorted(present)}")
    if list(root.rglob("*.py")):
        errors.append("dataset không được chứa script nạp .py")
    readme = (root / "README.md").read_text(encoding="utf-8") if (root / "README.md").exists() else ""
    cfg = re.findall(r"config_name:\s*(\S+)", readme)
    if sorted(cfg) != sorted(HF_TABLES):
        errors.append(f"README phải có đúng 6 config_name {sorted(HF_TABLES)}, nhận {sorted(cfg)}")

    # 3. papers bao phủ mọi bài xuất hiện ở bảng khác
    papers = set(T["papers"]["arxiv_id"].to_pylist())
    for name in HF_TABLES:
        missing = set(T[name]["arxiv_id"].to_pylist()) - papers
        if missing:
            errors.append(f"[papers] thiếu {len(missing)} bài có ở bảng {name}: {sorted(missing)[:5]}")

    # 4. predictions nối được với bibtex theo (arxiv_id, citation_key)
    bib_keys = {(r["arxiv_id"], r["citation_key"]) for r in T["bibtex"].select(["arxiv_id", "citation_key"]).to_pylist()}
    refs: dict[str, set[str]] = {}
    for r in T["references"].select(["arxiv_id", "ref_arxiv_id"]).to_pylist():
        refs.setdefault(r["arxiv_id"], set()).add(r["ref_arxiv_id"])
    cols = ["groundtruth_arxiv_id"] + [f"cand_{i}" for i in range(1, 6)]
    for r in T["predictions"].to_pylist():
        key = (r["arxiv_id"], r["citation_key"])
        if key not in bib_keys:
            errors.append(f"[predictions] {key} không có trong bibtex")
        # 5. mọi ID ứng viên / groundtruth khác null phải thuộc references của CHÍNH bài
        for c in cols:
            v = r[c]
            if v is not None and v not in refs.get(r["arxiv_id"], set()):
                errors.append(f"[predictions] {key}: {c}='{v}' không thuộc references của bài {r['arxiv_id']}")

    # 6. hierarchy theo quy ước M2: element_id và parent_id đều là element của chính bài
    elem = {(r["arxiv_id"], r["element_id"]) for r in T["elements"].select(["arxiv_id", "element_id"]).to_pylist()}
    for r in T["hierarchy"].to_pylist():
        a = r["arxiv_id"]
        for c in ("element_id", "parent_id"):
            if (a, r[c]) not in elem:
                errors.append(f"[hierarchy] {a} v{r['version']}: {c}='{r[c]}' không có trong elements")
    for a, e in elem:
        if "/" not in a and not e.startswith(a.replace(".", "-") + "#"):
            errors.append(f"[elements] element_id '{e}' không bắt đầu bằng '{a.replace('.', '-')}#'")

    return {"ok": not errors, "errors": errors[:50], "n_errors": len(errors), "row_counts": counts}