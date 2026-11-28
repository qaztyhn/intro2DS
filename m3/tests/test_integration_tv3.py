"""Tích hợp: fixture chung -> fit_ranker -> predict_top5 -> evaluate_mrr_at_5 = 0.75."""
import pyarrow as pa
import pytest

from m3.src.contracts import MrrResult, fixture, make_table, validate_table
from m3.src.evaluate import evaluate_mrr_at_5
from m3.src.model import fit_ranker, predict_top5

M = "24120058"
TRAIN = ["2507.00010", "2507.00011"]


def augmented_fixture():
    """contracts.fixture() làm nền (bài test 2507.00001 có đặc trưng null) + hai bài train dựng bằng pa.table."""
    f = fixture()
    refs = {a: [f"2301.{int(a[-2:]) * 10 + j:05d}" for j in range(1, 4)] for a in TRAIN}
    q, pr, lab = [], [], []
    for a in TRAIN:
        for k, g in (("t1", 0), ("t2", 1)):
            q.append({"arxiv_id": a, "citation_key": k})
            lab.append({"arxiv_id": a, "citation_key": k, "groundtruth_arxiv_id": refs[a][g], "label_source": "manual", "member_id": M})
            for j, r in enumerate(refs[a]):
                pos = j == g
                pr.append({"arxiv_id": a, "citation_key": k, "ref_arxiv_id": r,
                           "title_similarity": 0.9 if pos else 0.2, "first_author_match": pos,
                           "year_diff": 0 if pos else 3})                # arxiv_id_match để null: cột bị bỏ
    return {
        **f,
        "queries": pa.concat_tables([f["queries"], make_table("queries", q)]),
        "pairs": pa.concat_tables([f["pairs"], make_table("pairs", pr)]),
        "labels": pa.concat_tables([f["labels"], make_table("labels", lab)]),
        "splits": pa.concat_tables([f["splits"], make_table("splits", [{"arxiv_id": a, "partition": "train"} for a in TRAIN])]),
    }


def test_fixture_chain_gives_mrr_075():
    d = augmented_fixture()
    for name in ("queries", "pairs", "labels", "splits"):
        validate_table(name, d[name])
    model = fit_ranker(pairs=d["pairs"], labels=d["labels"], splits=d["splits"], seed=2026)
    assert model.features_used_ == ["title_similarity", "first_author_match", "year_diff"]
    assert model.features_dropped_ == ["arxiv_id_match"]
    # bài 2507.00002 có query nhưng không có trong splits -> bị bỏ kèm warning
    with pytest.warns(UserWarning, match="2507.00002"):
        preds = predict_top5(model, queries=d["queries"], pairs=d["pairs"], labels=d["labels"], splits=d["splits"])
    rows = {(r["arxiv_id"], r["citation_key"]): r for r in preds.to_pylist()}
    assert ("2507.00002", "q4") not in rows
    assert {k for k in rows if k[0] == "2507.00001"} == {("2507.00001", q) for q in ("q1", "q2", "q3")}
    # đặc trưng test đều null -> cùng điểm -> ref_arxiv_id nhỏ đứng trước
    assert (rows[("2507.00001", "q1")]["cand_1"], rows[("2507.00001", "q1")]["cand_2"]) == ("2301.00001", "2301.00002")
    result = evaluate_mrr_at_5(predictions=preds, labels=d["labels"], splits=d["splits"], manual=d["manual_publications"])
    assert result.mrr_at_5 == 0.75 and result.evaluated_entries == 2
    assert result == MrrResult(0.75, 2, 0.75, 2, None, 0)
    # bài train được dự đoán đúng: predictions chứa mọi partition và không làm hỏng MRR test
    assert {r["partition"] for r in preds.to_pylist()} == {"test", "train"}
