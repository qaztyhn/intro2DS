"""T3-1, T3-2 và đối chiếu tuyệt đối với `contracts.evaluate_mrr_at_5`."""
import random

import pyarrow as pa
import pytest

from m3.src import contracts
from m3.src.contracts import SCHEMAS, MrrResult, fixture, make_table
from m3.src.evaluate import evaluate_mrr_at_5, per_entry_reciprocal_rank

P, R1, R2 = "2507.00001", "2301.00001", "2301.00002"
M = "24120058"


def pred_table(rows):
    return pa.Table.from_pylist(rows, schema=SCHEMAS["predictions"])


# ------------------------------------------------------------------ T3-1
def test_t3_1_fixture_mrr_075():
    f = fixture()
    r = evaluate_mrr_at_5(predictions=f["predictions"], labels=f["labels"], splits=f["splits"],
                          manual=f["manual_publications"])
    assert isinstance(r, MrrResult)
    assert r.mrr_at_5 == 0.75 and r.evaluated_entries == 2
    assert (r.manual_mrr_at_5, r.manual_entries, r.auto_mrr_at_5, r.auto_entries) == (0.75, 2, None, 0)


def test_t3_1_q1_alone_is_half():
    f = fixture()
    only_q1 = f["labels"].slice(0, 1)
    assert only_q1.column("citation_key").to_pylist() == ["q1"]
    r = evaluate_mrr_at_5(predictions=f["predictions"], labels=only_q1, splits=f["splits"])
    assert (r.mrr_at_5, r.evaluated_entries) == (0.5, 1)


def test_t3_1_unlabeled_entry_not_in_denominator():
    f = fixture()
    scores = per_entry_reciprocal_rank(predictions=f["predictions"], labels=f["labels"], splits=f["splits"])
    assert scores.column("citation_key").to_pylist() == ["q1", "q2"]          # q3 (chưa nhãn) vắng mặt
    assert scores.column("reciprocal_rank").to_pylist() == [0.5, 1.0]
    assert scores.column("rank").to_pylist() == [2, 1]


def test_manual_none_leaves_pool_fields_empty_even_with_label_source():
    f = fixture()                                   # nhãn có label_source == "manual" nhưng không truyền sổ manual
    r = evaluate_mrr_at_5(predictions=f["predictions"], labels=f["labels"], splits=f["splits"])
    assert r == MrrResult(0.75, 2, None, 0, None, 0)


def test_manual_and_auto_split():
    Q = "2507.00007"
    labels = make_table("labels", [
        {"arxiv_id": P, "citation_key": "q1", "groundtruth_arxiv_id": R2, "label_source": "manual", "member_id": M},
        {"arxiv_id": Q, "citation_key": "a1", "groundtruth_arxiv_id": R1, "label_source": "auto", "member_id": M}])
    splits = make_table("splits", [{"arxiv_id": P, "partition": "test"}, {"arxiv_id": Q, "partition": "test"}])
    preds = pred_table([
        {"arxiv_id": P, "partition": "test", "citation_key": "q1", "groundtruth_arxiv_id": R2, "cand_1": R1, "cand_2": R2},
        {"arxiv_id": Q, "partition": "test", "citation_key": "a1", "groundtruth_arxiv_id": R1, "cand_1": R1}])
    manual = make_table("manual_publications", [{"arxiv_id": P, "member_id": M}])
    r = evaluate_mrr_at_5(predictions=preds, labels=labels, splits=splits, manual=manual)
    assert r == MrrResult(0.75, 2, 0.5, 1, 1.0, 1)


def test_groundtruth_outside_top5_scores_zero():
    labels = make_table("labels", [{"arxiv_id": P, "citation_key": "q1", "groundtruth_arxiv_id": "2301.99999",
                                    "label_source": "manual", "member_id": M}])
    splits = make_table("splits", [{"arxiv_id": P, "partition": "test"}])
    preds = pred_table([{"arxiv_id": P, "partition": "test", "citation_key": "q1",
                         **{f"cand_{i}": f"2301.0000{i}" for i in range(1, 6)}}])
    r = evaluate_mrr_at_5(predictions=preds, labels=labels, splits=splits)
    assert (r.mrr_at_5, r.evaluated_entries) == (0.0, 1)
    scores = per_entry_reciprocal_rank(predictions=preds, labels=labels, splits=splits)
    assert scores.column("rank").to_pylist() == [None]


def test_inputs_not_mutated():
    f = fixture()
    before = {k: f[k].to_pylist() for k in ("predictions", "labels", "splits", "manual_publications")}
    evaluate_mrr_at_5(predictions=f["predictions"], labels=f["labels"], splits=f["splits"],
                      manual=f["manual_publications"])
    assert before == {k: f[k].to_pylist() for k in before}


# ------------------------------------------------------------------ T3-2
def test_t3_2_no_labeled_test_entry_raises():
    f = fixture()
    with pytest.raises(ValueError, match="không có entry test nào có nhãn"):
        evaluate_mrr_at_5(predictions=f["predictions"], labels=SCHEMAS["labels"].empty_table(), splits=f["splits"])
    train_only = make_table("splits", [{"arxiv_id": P, "partition": "train"}])
    with pytest.raises(ValueError, match="không có entry test nào có nhãn"):
        evaluate_mrr_at_5(predictions=f["predictions"], labels=f["labels"], splits=train_only)


def test_t3_2_labeled_test_entry_missing_prediction_raises_with_key():
    f = fixture()
    with pytest.raises(ValueError, match=r"\(.2507.00001., .q1.\)") as e:
        evaluate_mrr_at_5(predictions=f["predictions"].slice(1), labels=f["labels"], splits=f["splits"])
    assert "không có dòng dự đoán" in str(e.value) and "[predictions]" in str(e.value)


def test_t3_2_train_entry_missing_prediction_is_fine():
    f = fixture()
    T = "2507.00009"
    labels = make_table("labels", f["labels"].to_pylist() + [
        {"arxiv_id": T, "citation_key": "t1", "groundtruth_arxiv_id": "2301.09999", "label_source": "manual", "member_id": M}])
    for part in ("train", "valid"):
        splits = make_table("splits", f["splits"].to_pylist() + [{"arxiv_id": T, "partition": part}])
        r = evaluate_mrr_at_5(predictions=f["predictions"], labels=labels, splits=splits)
        assert (r.mrr_at_5, r.evaluated_entries) == (0.75, 2)


def test_predictions_may_contain_all_partitions():
    f = fixture()
    extra = {"arxiv_id": "2507.00009", "partition": "train", "citation_key": "t1", "groundtruth_arxiv_id": "x", "cand_1": "y"}
    preds = pred_table(f["predictions"].to_pylist() + [extra])
    r = evaluate_mrr_at_5(predictions=preds, labels=f["labels"], splits=f["splits"])
    assert r.mrr_at_5 == 0.75


def test_invalid_tables_raise_with_table_name():
    f = fixture()
    dup = pred_table(f["predictions"].to_pylist() + f["predictions"].to_pylist()[:1])
    with pytest.raises(ValueError, match=r"\[predictions\] khóa trùng"):
        evaluate_mrr_at_5(predictions=dup, labels=f["labels"], splits=f["splits"])
    bad_split = make_table("splits", [{"arxiv_id": P, "partition": "dev"}])
    with pytest.raises(ValueError, match=r"\[splits\]"):
        evaluate_mrr_at_5(predictions=f["predictions"], labels=f["labels"], splits=bad_split)


# ------------------------------------------------------------------ đối chiếu với contract
def _random_case(seed: int):
    rng = random.Random(seed)
    refs = [f"2301.{i:05d}" for i in range(1, 9)]
    labels, splits, preds, manual = [], [], [], []
    complete = rng.random() < 0.7                       # 30% bộ dữ liệu cố ý thiếu dòng dự đoán của test
    for p in range(rng.randint(1, 12)):
        a = f"2507.{p + 1:05d}"
        part = rng.choice(["train", "valid", "test", "test"])
        in_splits = rng.random() < 0.9
        pool = rng.choice(["manual", "auto"])
        if in_splits:
            splits.append({"arxiv_id": a, "partition": part})
        if pool == "manual":
            manual.append({"arxiv_id": a, "member_id": "m"})
        for e in range(rng.randint(1, 4)):
            k = f"k{e}"
            gt = rng.choice(refs)
            if rng.random() < 0.75:                     # còn lại: entry chưa gán nhãn
                labels.append({"arxiv_id": a, "citation_key": k, "groundtruth_arxiv_id": gt,
                               "label_source": pool, "member_id": "m"})
            if (part == "test" and complete) or rng.random() < 0.5:
                cands = rng.sample(refs, rng.randint(0, 5))
                if rng.random() < 0.6 and gt not in cands:
                    cands.insert(rng.randint(0, len(cands)), gt)
                    cands = cands[:5]
                row = {"arxiv_id": a, "partition": part if in_splits else "train", "citation_key": k,
                       "groundtruth_arxiv_id": gt if rng.random() < 0.5 else None}
                row.update({f"cand_{i + 1}": c for i, c in enumerate(cands)})
                preds.append(row)
    rng.shuffle(labels)                                 # thứ tự dòng tùy ý: kiểm tra thứ tự cộng dồn
    rng.shuffle(preds)
    return dict(predictions=pred_table(preds), labels=make_table("labels", labels),
                splits=make_table("splits", splits), manual=make_table("manual_publications", manual))


def _outcome(fn, **kw):
    try:
        return ("ok", fn(**kw))
    except ValueError as e:
        return ("error", str(e))


@pytest.mark.parametrize("use_manual", [True, False])
def test_parity_with_contract_on_fixture(use_manual):
    f = fixture()
    kw = dict(predictions=f["predictions"], labels=f["labels"], splits=f["splits"],
              manual=f["manual_publications"] if use_manual else None)
    assert evaluate_mrr_at_5(**kw) == contracts.evaluate_mrr_at_5(**kw)


@pytest.mark.parametrize("use_manual", [True, False])
def test_parity_with_contract_on_seeded_random_data(use_manual):
    kinds = {"ok": 0, "error": 0}
    for seed in range(300):
        case = _random_case(seed)
        kw = dict(predictions=case["predictions"], labels=case["labels"], splits=case["splits"],
                  manual=case["manual"] if use_manual else None)
        mine, ref = _outcome(evaluate_mrr_at_5, **kw), _outcome(contracts.evaluate_mrr_at_5, **kw)
        assert mine == ref, (seed, mine, ref)           # bằng nhau tuyệt đối (so sánh float bằng ==), kể cả thông báo lỗi
        kinds[mine[0]] += 1
    assert kinds["ok"] > 50 and kinds["error"] > 10     # cả hai nhánh đều được kiểm tra thật
