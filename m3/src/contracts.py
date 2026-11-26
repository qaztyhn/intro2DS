"""Contract dùng chung của Milestone 3, bản 6 (nguồn chân lý; thay v4 và bản 2).

Chạy `python m3_contracts.py` để tự kiểm thử: schema, khóa, sổ đăng ký manual/auto, nhãn, quota,
thiết kế chia tập, MRR@5 (có tách manual/auto) và chuẩn hóa ID.
Quyết định bám theo phản hồi của thầy (QA Replies) và M3 Contract v6 (tài liệu đi kèm).
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Sequence

import pyarrow as pa
import pyarrow.compute as pc

# --------------------------------------------------------------------------------------
# 1. Schema trao đổi (thứ tự cột là thứ tự của schema)
# --------------------------------------------------------------------------------------
STR, LIST_STR = pa.string(), pa.list_(pa.string())

BIBTEX_SCHEMA = pa.schema([  # schema bảng bibtex của M2 (nguồn: m2/src/parsing/export.py)
    pa.field("arxiv_id", STR, nullable=False), pa.field("citation_key", STR, nullable=False),
    pa.field("entry_type", pa.dictionary(pa.int32(), STR)), pa.field("title", STR),
    pa.field("authors", LIST_STR), pa.field("year", pa.int32()), pa.field("doi", STR),
    pa.field("journal", STR), pa.field("url", STR)])

SCHEMAS: dict[str, pa.Schema] = {
    "papers": pa.schema([
        pa.field("arxiv_id", STR, nullable=False), pa.field("title", STR), pa.field("authors", LIST_STR),
        pa.field("submission_date", STR), pa.field("revised_dates", LIST_STR), pa.field("venue", STR)]),
    "references": pa.schema([
        pa.field("arxiv_id", STR, nullable=False), pa.field("ref_arxiv_id", STR, nullable=False),
        pa.field("ref_title", STR), pa.field("ref_authors", LIST_STR),
        pa.field("ref_submission_date", STR), pa.field("ref_s2_id", STR)]),
    "crawl_status": pa.schema([
        pa.field("arxiv_id", STR, nullable=False), pa.field("status", STR, nullable=False),
        pa.field("source_reference_count", pa.int64()), pa.field("candidate_count", pa.int64(), nullable=False)]),
    # assignments: ai SỞ HỮU ID range nào (một paper thuộc đúng một thành viên)
    "assignments": pa.schema([
        pa.field("arxiv_id", STR, nullable=False), pa.field("member_id", STR, nullable=False)]),
    # sổ đăng ký: bài được CHỌN cho manual / auto (kể cả bài manual không tìm được match nào)
    "manual_publications": pa.schema([
        pa.field("arxiv_id", STR, nullable=False), pa.field("member_id", STR, nullable=False)]),
    "auto_publications": pa.schema([
        pa.field("arxiv_id", STR, nullable=False), pa.field("member_id", STR, nullable=False)]),
    "queries": pa.schema([
        pa.field("arxiv_id", STR, nullable=False), pa.field("citation_key", STR, nullable=False)]),
    # pairs: 3 cột khóa + CÁC CỘT ĐẶC TRƯNG (không giới hạn ở 4 cột baseline). Xem _schema_ok.
    "pairs": pa.schema([
        pa.field("arxiv_id", STR, nullable=False), pa.field("citation_key", STR, nullable=False),
        pa.field("ref_arxiv_id", STR, nullable=False),
        pa.field("title_similarity", pa.float64()), pa.field("first_author_match", pa.bool_()),
        pa.field("year_diff", pa.int32()), pa.field("arxiv_id_match", pa.bool_())]),
    # labels: nhãn là CẶP (entry, reference) đã match. Không có no_match; groundtruth luôn có giá trị.
    "labels": pa.schema([
        pa.field("arxiv_id", STR, nullable=False), pa.field("citation_key", STR, nullable=False),
        pa.field("groundtruth_arxiv_id", STR, nullable=False), pa.field("label_source", STR, nullable=False),
        pa.field("member_id", STR, nullable=False)]),
    # splits: MỘT split chung toàn nhóm. pool (manual/auto) lấy từ sổ đăng ký, không lặp lại ở đây.
    "splits": pa.schema([
        pa.field("arxiv_id", STR, nullable=False), pa.field("partition", STR, nullable=False)]),
    "predictions": pa.schema([
        pa.field("arxiv_id", STR, nullable=False), pa.field("partition", pa.dictionary(pa.int32(), STR), nullable=False),
        pa.field("citation_key", STR, nullable=False), pa.field("groundtruth_arxiv_id", STR),
        *[pa.field(f"cand_{i}", STR) for i in range(1, 6)]]),
}
SCHEMAS["bibtex"] = BIBTEX_SCHEMA
KEYS: dict[str, tuple[str, ...]] = {
    "papers": ("arxiv_id",), "references": ("arxiv_id", "ref_arxiv_id"), "crawl_status": ("arxiv_id",),
    "assignments": ("arxiv_id",), "manual_publications": ("arxiv_id",), "auto_publications": ("arxiv_id",),
    "queries": ("arxiv_id", "citation_key"), "pairs": ("arxiv_id", "citation_key", "ref_arxiv_id"),
    "labels": ("arxiv_id", "citation_key"), "splits": ("arxiv_id",), "predictions": ("arxiv_id", "citation_key"),
    "bibtex": ("arxiv_id", "citation_key")}

# Sáu bảng của dataset Hugging Face theo matching.md (bảng nội bộ như pairs, labels, splits KHÔNG được xuất)
HF_TABLES = ("papers", "references", "elements", "hierarchy", "bibtex", "predictions")

CRAWL_STATUSES = {"ok", "empty", "failed", "missing", "incomplete"}
LABEL_SOURCES = {"manual", "auto"}
PARTITIONS = {"train", "valid", "test"}
USABLE_CRAWL = {"ok", "empty"}   # crawl hợp lệ; failed/missing/incomplete KHÔNG sinh nhãn


# --------------------------------------------------------------------------------------
# 2. Chuẩn hóa ID arXiv (khóa dùng để join). paper_key() của M1 chỉ dùng để tìm THƯ MỤC.
# --------------------------------------------------------------------------------------
_NEW = re.compile(r"(\d{4}\.\d{4,5})(?:v\d+)?$")
_OLD = re.compile(r"([a-z]+(?:-[a-z]+)*(?:\.[A-Za-z]{2})?/\d{7})(?:v\d+)?$")


def normalize_arxiv_id(raw: str | None) -> str | None:
    """'arXiv:2301.00001v2' / 'https://arxiv.org/abs/2301.00001v2' -> '2301.00001'; ID kiểu cũ giữ nguyên dấu '/'.
    Trả None nếu không nhận diện được (không đoán)."""
    if not raw:
        return None
    s = raw.strip()
    s = re.sub(r"^(?:https?://)?(?:www\.)?arxiv\.org/(?:abs|pdf)/", "", s, flags=re.I)
    s = re.sub(r"^arxiv:\s*", "", s, flags=re.I)
    s = re.sub(r"\.pdf$", "", s, flags=re.I)
    m = _NEW.fullmatch(s) or _OLD.fullmatch(s)
    return m.group(1) if m else None


# --------------------------------------------------------------------------------------
# 3. Kiểm tra bảng. Lỗi luôn là ValueError kèm tên bảng và khóa liên quan.
# --------------------------------------------------------------------------------------
def make_table(name: str, rows: Sequence[dict]) -> pa.Table:
    """Dựng bảng từ danh sách dict theo schema (trường vắng mặt thành null); bảng rỗng vẫn đủ schema."""
    schema = SCHEMAS[name]
    return pa.Table.from_pylist([dict(r) for r in rows], schema=schema) if rows else schema.empty_table()


def _schema_ok(name: str, table: pa.Table) -> None:
    expected = SCHEMAS[name]
    names = table.schema.names
    if name == "pairs":   # cho phép thêm cột đặc trưng SAU 3 cột khóa; 3 khóa phải đúng kiểu và thứ tự
        if names[:3] != list(expected.names[:3]) or any(table.schema.field(n).type != STR for n in names[:3]):
            raise ValueError(f"[pairs] 3 cột khóa đầu phải là {list(expected.names[:3])} kiểu string, nhận {names[:3]}")
        return
    if names != list(expected.names) or any(table.schema.field(n).type != expected.field(n).type for n in names):
        raise ValueError(f"[{name}] sai schema.\n  mong đợi: {expected}\n  nhận được: {table.schema}")


def _enum(name: str, table: pa.Table, col: str, allowed: set[str]) -> None:
    for v in {x.as_py() for x in table.column(col)}:
        if v not in allowed:
            raise ValueError(f"[{name}] giá trị '{v}' của cột {col} không thuộc {sorted(allowed)}")


def validate_table(name: str, table: pa.Table) -> None:
    """Schema, cột bắt buộc không null, khóa không rỗng/không trùng, giá trị enum hợp lệ."""
    _schema_ok(name, table)
    schema, keys = SCHEMAS[name], KEYS[name]
    for f in schema:
        if f.name in table.schema.names and not f.nullable and table.column(f.name).null_count:
            raise ValueError(f"[{name}] cột bắt buộc '{f.name}' có {table.column(f.name).null_count} giá trị null")
    seen: set[tuple] = set()
    for r in table.select(list(keys)).to_pylist():
        k = tuple(r[c] for c in keys)
        if any(v is None or str(v).strip() == "" for v in k):
            raise ValueError(f"[{name}] khóa rỗng: {k}")
        if k in seen:
            raise ValueError(f"[{name}] khóa trùng: {k}")
        seen.add(k)
    for tname, col, allowed in (("crawl_status", "status", CRAWL_STATUSES), ("labels", "label_source", LABEL_SOURCES),
                                ("splits", "partition", PARTITIONS), ("predictions", "partition", PARTITIONS)):
        if name == tname:
            _enum(name, table, col, allowed)


def _owner(assignments: pa.Table) -> dict[str, str]:
    validate_table("assignments", assignments)
    return {r["arxiv_id"]: r["member_id"] for r in assignments.to_pylist()}


def validate_registry(assignments: pa.Table, manual: pa.Table, auto: pa.Table) -> None:
    """Sổ đăng ký: bài thuộc đúng chủ ID range; manual và auto LOẠI TRỪ nhau ở cấp publication."""
    owner = _owner(assignments)
    for name, t in (("manual_publications", manual), ("auto_publications", auto)):
        validate_table(name, t)
        for r in t.to_pylist():
            if r["arxiv_id"] not in owner:
                raise ValueError(f"[{name}] bài {r['arxiv_id']} không có trong assignments")
            if owner[r["arxiv_id"]] != r["member_id"]:
                raise ValueError(f"[{name}] bài {r['arxiv_id']} thuộc {owner[r['arxiv_id']]} nhưng đăng ký cho {r['member_id']}")
    both = {r["arxiv_id"] for r in manual.to_pylist()} & {r["arxiv_id"] for r in auto.to_pylist()}
    if both:
        raise ValueError(f"[auto_publications] bài gán tay không được qua gán tự động: {sorted(both)}")


def validate_pairs(queries: pa.Table, references: pa.Table, pairs: pa.Table) -> None:
    """Mỗi bài có m entry và n ứng viên thì có đúng m*n dòng pairs; mọi cặp thuộc đúng bài của nó."""
    for n, t in (("queries", queries), ("references", references), ("pairs", pairs)):
        validate_table(n, t)
    qs, rs = defaultdict(set), defaultdict(set)
    for r in queries.to_pylist():
        qs[r["arxiv_id"]].add(r["citation_key"])
    for r in references.to_pylist():
        rs[r["arxiv_id"]].add(r["ref_arxiv_id"])
    got = defaultdict(int)
    for r in pairs.select(["arxiv_id", "citation_key", "ref_arxiv_id"]).to_pylist():
        a = r["arxiv_id"]
        if r["citation_key"] not in qs[a]:
            raise ValueError(f"[pairs] entry {(a, r['citation_key'])} không có trong queries của bài đó")
        if r["ref_arxiv_id"] not in rs[a]:
            raise ValueError(f"[pairs] ứng viên {(a, r['ref_arxiv_id'])} không nằm trong references của chính bài {a}")
        got[a] += 1
    for a in qs:
        if got[a] != len(qs[a]) * len(rs[a]):
            raise ValueError(f"[pairs] bài {a}: có {got[a]} dòng, cần {len(qs[a])} x {len(rs[a])}")


def validate_labels(labels: pa.Table, pairs: pa.Table, crawl_status: pa.Table,
                    assignments: pa.Table, manual: pa.Table, auto: pa.Table) -> None:
    """Nhãn là cặp đã match: groundtruth phải là ứng viên của CHÍNH entry đó; bài có nhãn phải có crawl hợp lệ;
    nguồn nhãn phải khớp sổ đăng ký; member_id phải là chủ ID range."""
    validate_table("labels", labels)
    validate_registry(assignments, manual, auto)
    owner = _owner(assignments)
    man = {r["arxiv_id"] for r in manual.to_pylist()}
    aut = {r["arxiv_id"] for r in auto.to_pylist()}
    ok = {r["arxiv_id"] for r in crawl_status.to_pylist() if r["status"] in USABLE_CRAWL}
    cand = {(r["arxiv_id"], r["citation_key"], r["ref_arxiv_id"]) for r in pairs.select(KEYS["pairs"]).to_pylist()}
    for r in labels.to_pylist():
        a, k, src = r["arxiv_id"], r["citation_key"], r["label_source"]
        if a not in ok:
            raise ValueError(f"[labels] bài {a} không có crawl hợp lệ (ok/empty)")
        if (a, k, r["groundtruth_arxiv_id"]) not in cand:
            raise ValueError(f"[labels] {(a, k)}: '{r['groundtruth_arxiv_id']}' không thuộc ứng viên của entry này")
        if src == "manual" and a not in man:
            raise ValueError(f"[labels] {(a, k)} là nhãn manual nhưng bài không có trong manual_publications")
        if src == "auto" and (a in man or a not in aut):
            raise ValueError(f"[labels] {(a, k)} là nhãn auto nhưng bài thuộc pool gán tay hoặc chưa đăng ký auto")
        if r["member_id"] != owner.get(a):
            raise ValueError(f"[labels] {(a, k)}: member_id={r['member_id']} khác chủ ID range {owner.get(a)}")


def validate_splits(splits: pa.Table, labels: pa.Table, manual: pa.Table, auto: pa.Table) -> None:
    """Mỗi bài đúng một partition, thuộc đúng một pool đã đăng ký; bài valid/test phải có ít nhất một nhãn."""
    validate_table("splits", splits)
    man = {r["arxiv_id"] for r in manual.to_pylist()}
    aut = {r["arxiv_id"] for r in auto.to_pylist()}
    n_lab = defaultdict(int)
    for r in labels.to_pylist():
        n_lab[r["arxiv_id"]] += 1
    for r in splits.to_pylist():
        a, p = r["arxiv_id"], r["partition"]
        if a not in man and a not in aut:
            raise ValueError(f"[splits] bài {a} không thuộc pool manual hay auto đã đăng ký")
        if p in {"valid", "test"} and n_lab[a] == 0:
            raise ValueError(f"[splits] bài {a} thuộc {p} nhưng không có nhãn nào")


# --------------------------------------------------------------------------------------
# 4. Quota và thiết kế chia tập (theo phản hồi của thầy)
# --------------------------------------------------------------------------------------
def auto_quota(n_assigned: int, n_manual: int, fraction: float = 0.10) -> int:
    """Số paper tự động của MỘT thành viên = ceil(fraction * (số paper được giao - số paper gán tay)).
    Ví dụ thầy nêu: 5000 giao, 5 gán tay -> 500."""
    return math.ceil(round(fraction * (n_assigned - n_manual), 9))


def check_manual_quota(labels: pa.Table, manual: pa.Table, assignments: pa.Table, *,
                       manual_papers: int = 5, min_pairs: int = 20, min_total: int = 100) -> dict[str, dict]:
    """Mỗi thành viên có ít nhất manual_papers bài gán tay, mỗi bài >= min_pairs CẶP MATCH, tổng >= min_total."""
    validate_registry(assignments, manual, make_table("auto_publications", []))
    per_paper: dict[str, int] = defaultdict(int)
    for r in labels.to_pylist():
        if r["label_source"] == "manual":
            per_paper[r["arxiv_id"]] += 1
    report: dict[str, dict] = {}
    for m in sorted({r["member_id"] for r in assignments.to_pylist()}):
        papers = sorted(r["arxiv_id"] for r in manual.to_pylist() if r["member_id"] == m)
        if len(papers) < manual_papers:
            raise ValueError(f"[quota] thành viên {m}: {len(papers)} bài gán tay, cần {manual_papers} trở lên")
        short = [(a, per_paper[a]) for a in papers if per_paper[a] < min_pairs]
        if short:
            raise ValueError(f"[quota] thành viên {m}: bài thiếu cặp match (cần >= {min_pairs}): {short}")
        total = sum(per_paper[a] for a in papers)
        if total < min_total:
            raise ValueError(f"[quota] thành viên {m}: tổng {total} cặp match < {min_total}")
        report[m] = {"papers": papers, "pairs_per_paper": {a: per_paper[a] for a in papers}, "total_pairs": total}
    return report


def validate_split_design(splits: pa.Table, manual: pa.Table, auto: pa.Table, assignments: pa.Table, *,
                          valid_per_pool_per_member: int = 1, test_per_pool_per_member: int = 1,
                          per_member: bool = True) -> dict:
    """Số bài mỗi pool trong valid/test = (số thành viên) x (hạn mức mỗi thành viên) -- STT 4.3 'căn cứ số lượng
    thành viên'. per_member=True (quyết định N1) còn đòi MỖI thành viên có đúng hạn mức ở mỗi pool/partition,
    để dataset HF của từng người có đủ ba partition."""
    owner = _owner(assignments)
    members = sorted(set(owner.values()))
    pool_of = {r["arxiv_id"]: "manual" for r in manual.to_pylist()}
    pool_of.update({r["arxiv_id"]: "auto" for r in auto.to_pylist()})
    count: dict[tuple, int] = defaultdict(int)
    for r in splits.to_pylist():
        count[(pool_of[r["arxiv_id"]], r["partition"], owner[r["arxiv_id"]])] += 1
    want = {"valid": valid_per_pool_per_member, "test": test_per_pool_per_member}
    for pool in ("manual", "auto"):
        for part, q in want.items():
            total = sum(count[(pool, part, m)] for m in members)
            if total != q * len(members):
                raise ValueError(f"[splits] {part}/{pool}: {total} bài, cần {q * len(members)} ({q} x {len(members)} thành viên)")
            if per_member:
                for m in members:
                    if count[(pool, part, m)] != q:
                        raise ValueError(f"[splits] thành viên {m}: {part}/{pool} có {count[(pool, part, m)]} bài, cần {q}")
    return {f"{part}/{pool}": sum(count[(pool, part, m)] for m in members)
            for pool in ("manual", "auto") for part in ("train", "valid", "test")}


# --------------------------------------------------------------------------------------
# 5. MRR@5 chuẩn (đúng đề): |Q| = entry CÓ NHÃN match trong test; entry không có nhãn không phải sample.
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class MrrResult:
    mrr_at_5: float
    evaluated_entries: int
    manual_mrr_at_5: float | None = None
    manual_entries: int = 0
    auto_mrr_at_5: float | None = None
    auto_entries: int = 0


@dataclass(frozen=True)
class MaskConfig:
    """Che url/DOI/arXiv ID của entry CHỈ khi tính đặc trưng (thí nghiệm D13). Băm theo (arxiv_id, citation_key, seed)."""
    fields: tuple[str, ...] = ("url", "doi", "arxiv_id")
    fraction: float = 0.5
    seed: int = 2026


def evaluate_mrr_at_5(*, predictions: pa.Table, labels: pa.Table, splits: pa.Table,
                      manual: pa.Table | None = None) -> MrrResult:
    validate_table("labels", labels)
    validate_table("splits", splits)
    pred = {(r["arxiv_id"], r["citation_key"]): r for r in predictions.to_pylist()}
    part = {r["arxiv_id"]: r["partition"] for r in splits.to_pylist()}
    man = {r["arxiv_id"] for r in manual.to_pylist()} if manual is not None else None
    tot: dict[str, float] = defaultdict(float)
    cnt: dict[str, int] = defaultdict(int)
    for lab in labels.to_pylist():
        a, k = lab["arxiv_id"], lab["citation_key"]
        if part.get(a) != "test":
            continue
        row = pred.get((a, k))
        if row is None:
            raise ValueError(f"[predictions] entry có nhãn {(a, k)} không có dòng dự đoán")
        cands = [row.get(f"cand_{i}") for i in range(1, 6)]
        score = 1.0 / (cands.index(lab["groundtruth_arxiv_id"]) + 1) if lab["groundtruth_arxiv_id"] in cands else 0.0
        for p in ["all"] + ([("manual" if a in man else "auto")] if man is not None else []):
            tot[p] += score
            cnt[p] += 1
    if cnt["all"] == 0:
        raise ValueError("[predictions] không có entry test nào có nhãn để đánh giá")
    mean = lambda p: (tot[p] / cnt[p]) if cnt[p] else None
    return MrrResult(tot["all"] / cnt["all"], cnt["all"], mean("manual"), cnt["manual"], mean("auto"), cnt["auto"])


# --------------------------------------------------------------------------------------
# 6. Dữ liệu mẫu (không còn no_match)
# --------------------------------------------------------------------------------------
def fixture() -> dict[str, pa.Table]:
    """Bài test 2507.00001: 3 entry (q1, q2 có nhãn; q3 chưa gán nhãn), 2 ứng viên R1=2301.00001, R2=2301.00002.
    Bài 2507.00002: crawl 'empty' (0 ứng viên). Bài 2507.00003: crawl 'missing'."""
    R1, R2, P, P2, P3 = "2301.00001", "2301.00002", "2507.00001", "2507.00002", "2507.00003"
    M = "24120058"
    return {
        "assignments": make_table("assignments", [{"arxiv_id": a, "member_id": M} for a in (P, P2, P3)]),
        "manual_publications": make_table("manual_publications", [{"arxiv_id": P, "member_id": M}]),
        "auto_publications": make_table("auto_publications", []),
        "queries": make_table("queries", [{"arxiv_id": P, "citation_key": k} for k in ("q1", "q2", "q3")]
                              + [{"arxiv_id": P2, "citation_key": "q4"}]),
        "references": make_table("references", [{"arxiv_id": P, "ref_arxiv_id": R1}, {"arxiv_id": P, "ref_arxiv_id": R2}]),
        "crawl_status": make_table("crawl_status", [
            {"arxiv_id": P, "status": "ok", "source_reference_count": 2, "candidate_count": 2},
            {"arxiv_id": P2, "status": "empty", "source_reference_count": 0, "candidate_count": 0},
            {"arxiv_id": P3, "status": "missing", "candidate_count": 0}]),
        "pairs": make_table("pairs", [{"arxiv_id": P, "citation_key": k, "ref_arxiv_id": r}
                                      for k in ("q1", "q2", "q3") for r in (R1, R2)]),
        "labels": make_table("labels", [
            {"arxiv_id": P, "citation_key": "q1", "groundtruth_arxiv_id": R2, "label_source": "manual", "member_id": M},
            {"arxiv_id": P, "citation_key": "q2", "groundtruth_arxiv_id": R1, "label_source": "manual", "member_id": M}]),
        "splits": make_table("splits", [{"arxiv_id": P, "partition": "test"}]),
        "predictions": pa.Table.from_pylist([
            {"arxiv_id": P, "partition": "test", "citation_key": "q1", "groundtruth_arxiv_id": R2, "cand_1": R1, "cand_2": R2},
            {"arxiv_id": P, "partition": "test", "citation_key": "q2", "groundtruth_arxiv_id": R1, "cand_1": R1, "cand_2": R2},
            {"arxiv_id": P, "partition": "test", "citation_key": "q3", "cand_1": R2, "cand_2": R1}],
            schema=SCHEMAS["predictions"]),
    }


def toy_design(n_members: int = 4, n_manual: int = 3, n_auto: int = 4) -> dict[str, pa.Table]:
    """Thiết kế chia tập nhỏ: mỗi thành viên có n_manual bài tay và n_auto bài tự động; mỗi pool lấy
    1 bài vào test, 1 bài vào valid cho MỖI thành viên (quyết định N1), còn lại train. Mỗi bài có 1 nhãn."""
    ass, man, aut, lab, spl, n = [], [], [], [], [], 0
    for i in range(n_members):
        m = f"m{i + 1}"
        for pool, count, reg in (("manual", n_manual, man), ("auto", n_auto, aut)):
            for j in range(count):
                n += 1
                a = f"2507.{n:05d}"
                ass.append({"arxiv_id": a, "member_id": m})
                reg.append({"arxiv_id": a, "member_id": m})
                lab.append({"arxiv_id": a, "citation_key": "k1", "groundtruth_arxiv_id": f"2301.{n:05d}",
                            "label_source": pool, "member_id": m})
                spl.append({"arxiv_id": a, "partition": "test" if j == 0 else "valid" if j == 1 else "train"})
    return {"assignments": make_table("assignments", ass), "manual_publications": make_table("manual_publications", man),
            "auto_publications": make_table("auto_publications", aut), "labels": make_table("labels", lab),
            "splits": make_table("splits", spl)}


def _expect_error(fn, fragment: str) -> None:
    try:
        fn()
    except ValueError as e:
        assert fragment in str(e), f"thông báo lỗi không chứa '{fragment}': {e}"
        return
    raise AssertionError(f"mong đợi ValueError chứa '{fragment}' nhưng không có lỗi")


def self_test() -> None:
    f = fixture()
    for n in ("assignments", "manual_publications", "queries", "references", "crawl_status", "pairs", "labels", "splits", "predictions"):
        validate_table(n, f[n])
    validate_registry(f["assignments"], f["manual_publications"], f["auto_publications"])
    validate_pairs(f["queries"], f["references"], f["pairs"])
    validate_labels(f["labels"], f["pairs"], f["crawl_status"], f["assignments"], f["manual_publications"], f["auto_publications"])
    validate_splits(f["splits"], f["labels"], f["manual_publications"], f["auto_publications"])
    P = f["pairs"]

    # MRR: q1 hạng 2 (0.5), q2 hạng 1 (1.0), q3 chưa gán nhãn -> ngoài mẫu số => (0.5+1)/2 = 0.75
    r = evaluate_mrr_at_5(predictions=f["predictions"], labels=f["labels"], splits=f["splits"], manual=f["manual_publications"])
    assert (r.mrr_at_5, r.evaluated_entries, r.manual_mrr_at_5, r.manual_entries, r.auto_mrr_at_5, r.auto_entries) == (0.75, 2, 0.75, 2, None, 0), r
    assert P.filter(pc.equal(P["arxiv_id"], "2507.00002")).num_rows == 0          # bài 0 ứng viên: 0 dòng pairs

    # lỗi liên kết và nhãn
    bad_pairs = make_table("pairs", P.to_pylist() + [{"arxiv_id": "2507.00001", "citation_key": "q1", "ref_arxiv_id": "9999.99999"}])
    _expect_error(lambda: validate_pairs(f["queries"], f["references"], bad_pairs), "không nằm trong references")
    args = (f["pairs"], f["crawl_status"], f["assignments"], f["manual_publications"], f["auto_publications"])
    base = f["labels"].to_pylist()
    def lab(**kw): return make_table("labels", [{**base[0], **kw}])
    _expect_error(lambda: validate_labels(lab(groundtruth_arxiv_id="9999.99999"), *args), "không thuộc ứng viên")
    _expect_error(lambda: validate_labels(lab(arxiv_id="2507.00003", citation_key="q9"), *args), "không có crawl hợp lệ")
    _expect_error(lambda: validate_labels(lab(member_id="khác"), *args), "khác chủ ID range")
    _expect_error(lambda: validate_labels(lab(label_source="auto"), *args), "thuộc pool gán tay")     # bài tay bị auto-label
    _expect_error(lambda: validate_table("labels", make_table("labels", base * 2)), "khóa trùng")
    _expect_error(lambda: validate_table("splits", make_table("splits", [{"arxiv_id": "a", "partition": "dev"}])), "không thuộc")
    # sổ đăng ký: bài manual (kể cả 0 nhãn) nằm trong sổ nên KHÔNG thể bị chọn làm auto
    both_auto = make_table("auto_publications", [{"arxiv_id": "2507.00001", "member_id": "24120058"}])
    _expect_error(lambda: validate_registry(f["assignments"], f["manual_publications"], both_auto), "không được qua gán tự động")
    wrong_owner = make_table("manual_publications", [{"arxiv_id": "2507.00001", "member_id": "x"}])
    _expect_error(lambda: validate_registry(f["assignments"], wrong_owner, f["auto_publications"]), "đăng ký cho")
    no_lab = make_table("splits", [{"arxiv_id": "2507.00001", "partition": "test"}, {"arxiv_id": "2507.00002", "partition": "valid"}])
    reg2 = make_table("auto_publications", [{"arxiv_id": "2507.00002", "member_id": "24120058"}])
    _expect_error(lambda: validate_splits(no_lab, f["labels"], f["manual_publications"], reg2), "không có nhãn nào")
    _expect_error(lambda: evaluate_mrr_at_5(predictions=f["predictions"].slice(1), labels=f["labels"], splits=f["splits"]), "không có dòng dự đoán")
    only1 = pa.Table.from_pylist(base[:1], schema=SCHEMAS["labels"])
    one = evaluate_mrr_at_5(predictions=f["predictions"], labels=only1, splits=f["splits"])
    assert (one.mrr_at_5, one.evaluated_entries) == (0.5, 1), one
    assert f["predictions"].column("cand_3").null_count == 3                                   # dưới 5 ứng viên: null
    validate_table("pairs", P.append_column(pa.field("token_jaccard", pa.float64()), pa.array([0.1] * P.num_rows)))

    # quota theo thầy: 5000 giao, 5 gán tay -> 500 paper tự động mỗi người; nhóm 4 người -> 2000
    assert auto_quota(5000, 5) == 500 and 4 * auto_quota(5000, 5) == 2000 and auto_quota(100, 5) == 10 and auto_quota(1000, 0) == 100

    # thiết kế chia tập (N1): 4 thành viên -> valid/test mỗi pool 4 bài, mỗi thành viên 1 bài
    d = toy_design()
    A, Mn, Au, S = d["assignments"], d["manual_publications"], d["auto_publications"], d["splits"]
    counts = validate_split_design(S, Mn, Au, A)
    assert counts == {"train/manual": 4, "valid/manual": 4, "test/manual": 4, "train/auto": 8, "valid/auto": 4, "test/auto": 4}, counts
    # quy tắc "1 tay + 1 tự động cho CẢ NHÓM" (bản v4) bị bắt: cần 4 bài mỗi pool theo số thành viên
    owner = {r["arxiv_id"]: r["member_id"] for r in A.to_pylist()}
    whole_group_1 = make_table("splits", [{**r, "partition": "train"} if r["partition"] != "train" and owner[r["arxiv_id"]] != "m1" else r
                                          for r in S.to_pylist()])
    _expect_error(lambda: validate_split_design(whole_group_1, Mn, Au, A, per_member=False), "thành viên)")
    # tổng đúng nhưng dồn vào một thành viên: per_member=False cho qua, per_member=True phải bắt
    man_m1 = sorted(r["arxiv_id"] for r in Mn.to_pylist() if r["member_id"] == "m1")
    man_m2 = sorted(r["arxiv_id"] for r in Mn.to_pylist() if r["member_id"] == "m2")
    part = {r["arxiv_id"]: r["partition"] for r in S.to_pylist()}
    part[man_m1[2]], part[man_m2[0]] = "test", "train"             # m1 có 2 test tay, m2 mất test tay
    skewed = make_table("splits", [{"arxiv_id": a, "partition": p} for a, p in part.items()])
    validate_split_design(skewed, Mn, Au, A, per_member=False)
    _expect_error(lambda: validate_split_design(skewed, Mn, Au, A), "cần 1")
    # nhãn và split của thiết kế mẫu hợp lệ
    pairs_d = make_table("pairs", [{"arxiv_id": r["arxiv_id"], "citation_key": "k1", "ref_arxiv_id": r["groundtruth_arxiv_id"]} for r in d["labels"].to_pylist()])
    crawl_d = make_table("crawl_status", [{"arxiv_id": r["arxiv_id"], "status": "ok", "candidate_count": 1} for r in A.to_pylist()])
    validate_labels(d["labels"], pairs_d, crawl_d, A, Mn, Au)
    validate_splits(S, d["labels"], Mn, Au)

    # quota gán tay (toy: 3 bài tay mỗi thành viên nên dùng tham số nhỏ)
    rep = check_manual_quota(d["labels"], Mn, A, manual_papers=3, min_pairs=1, min_total=3)
    assert len(rep) == 4 and all(v["total_pairs"] == 3 for v in rep.values())
    _expect_error(lambda: check_manual_quota(d["labels"], Mn, A, manual_papers=5), "cần 5")
    _expect_error(lambda: check_manual_quota(d["labels"], Mn, A, manual_papers=3, min_pairs=2, min_total=1), "thiếu cặp match")

    R_T = "2301.09999"
    # chuẩn hóa ID
    cases = {"arXiv:2301.00001v2": "2301.00001", "https://arxiv.org/abs/2301.00001v3": "2301.00001", "2301.00001": "2301.00001",
             "hep-th/9901001v1": "hep-th/9901001", "math.GT/0309136": "math.GT/0309136", "arxiv.org/pdf/2301.00001.pdf": "2301.00001",
             "10.1000/xyz": None, "": None, None: None}
    for raw, want in cases.items():
        assert normalize_arxiv_id(raw) == want, (raw, normalize_arxiv_id(raw), want)
    assert len(HF_TABLES) == 6 and "sources" not in HF_TABLES
    assert MaskConfig().fraction == 0.5 and MaskConfig().fields == ("url", "doi", "arxiv_id")
    # predictions chứa mọi partition: nhãn train thiếu dòng dự đoán KHÔNG làm hỏng MRR trên test
    lab_train = make_table("labels", f["labels"].to_pylist() + [
        {"arxiv_id": "2507.00009", "citation_key": "t1", "groundtruth_arxiv_id": R_T, "label_source": "manual", "member_id": "24120058"}])
    spl_train = make_table("splits", f["splits"].to_pylist() + [{"arxiv_id": "2507.00009", "partition": "train"}])
    assert evaluate_mrr_at_5(predictions=f["predictions"], labels=lab_train, splits=spl_train).mrr_at_5 == 0.75
    # quota: nhiều hơn 5 bài gán tay vẫn đạt
    check_manual_quota(d["labels"], Mn, A, manual_papers=2, min_pairs=1, min_total=2)
    print("m3_contracts v6: tất cả kiểm thử đạt (schema, khóa, sổ đăng ký, nhãn, quota=500, chia tập, MRR=0.75, ID).")


if __name__ == "__main__":
    self_test()

