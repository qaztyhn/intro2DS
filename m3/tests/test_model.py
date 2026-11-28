"""T3-3, T3-4, T3-5, T3-7 và các kiểm tra phụ của `model.py`."""
import random
import warnings

import numpy as np
import pyarrow as pa
import pytest

from m3.src.contracts import SCHEMAS, make_table, validate_pairs, validate_table
from m3.src.evaluate import evaluate_mrr_at_5
from m3.src.model import FORBIDDEN_FEATURES, fit_ranker, predict_top5

M = "24120058"


def make_synth(seed: int = 7, n_train: int = 8, n_valid: int = 2, n_test: int = 2) -> dict[str, pa.Table]:
    """Dữ liệu tổng hợp nhỏ, tất định theo seed. Đặc trưng có tín hiệu: cặp đúng có tiêu đề giống hơn."""
    rng = random.Random(seed)
    parts = ["train"] * n_train + ["valid"] * n_valid + ["test"] * n_test
    queries, pairs, labels, splits, refs = [], [], [], [], []
    for p, part in enumerate(parts):
        a = f"2507.{p + 1:05d}"
        splits.append({"arxiv_id": a, "partition": part})
        cands = [f"2301.{p * 10 + j + 1:05d}" for j in range(rng.randint(3, 6))]
        refs += [{"arxiv_id": a, "ref_arxiv_id": c} for c in cands]
        for e in range(rng.randint(2, 4)):
            k = f"k{e}"
            queries.append({"arxiv_id": a, "citation_key": k})
            gt = rng.choice(cands)
            if rng.random() < 0.85:
                labels.append({"arxiv_id": a, "citation_key": k, "groundtruth_arxiv_id": gt,
                               "label_source": "manual" if p % 2 == 0 else "auto", "member_id": M})
            for c in cands:
                pos = c == gt
                pairs.append({
                    "arxiv_id": a, "citation_key": k, "ref_arxiv_id": c,
                    "title_similarity": min(1.0, max(0.0, rng.gauss(0.85 if pos else 0.4, 0.12))),
                    "first_author_match": (rng.random() < (0.9 if pos else 0.2)) if rng.random() < 0.9 else None,
                    "year_diff": rng.choice([0, 0, 1]) if pos else rng.randint(0, 5),
                    "arxiv_id_match": True if (pos and rng.random() < 0.5) else None})
    return {"queries": make_table("queries", queries), "references": make_table("references", refs),
            "pairs": make_table("pairs", pairs), "labels": make_table("labels", labels),
            "splits": make_table("splits", splits)}


def fit(d, **kw):
    return fit_ranker(pairs=d["pairs"], labels=d["labels"], splits=d["splits"], seed=2026, **kw)


def predict(model, d, **kw):
    return predict_top5(model, queries=d["queries"], pairs=d["pairs"], labels=d["labels"], splits=d["splits"], **kw)


def coef(model):
    return model.named_steps["clf"].coef_.copy(), model.named_steps["clf"].intercept_.copy()


def test_synth_is_valid_contract_data():
    d = make_synth()
    validate_pairs(d["queries"], d["references"], d["pairs"])


# ------------------------------------------------------------------ T3-3
def tiny_model():
    return fit(make_synth())


def one_paper(rows_per_entry: dict[str, list[tuple[str, float | None]]], partition="test"):
    """Một bài, nhiều entry; mỗi entry là danh sách (ref, title_similarity). Các đặc trưng khác để null."""
    a = "2507.90001"
    queries = [{"arxiv_id": a, "citation_key": k} for k in rows_per_entry]
    pairs = [{"arxiv_id": a, "citation_key": k, "ref_arxiv_id": r, "title_similarity": s}
             for k, cs in rows_per_entry.items() for r, s in cs]
    return dict(queries=make_table("queries", queries), pairs=make_table("pairs", pairs),
                labels=SCHEMAS["labels"].empty_table(), splits=make_table("splits", [{"arxiv_id": a, "partition": partition}]))


def test_t3_3_fewer_than_five_candidates_gives_null_cands():
    model = tiny_model()
    d = one_paper({"q": [("2301.00005", 0.9), ("2301.00006", 0.1)]})
    out = predict(model, d)
    row = out.to_pylist()[0]
    assert row["cand_1"] == "2301.00005" and row["cand_2"] == "2301.00006"
    assert [row[f"cand_{i}"] for i in (3, 4, 5)] == [None, None, None]


def test_t3_3_zero_candidates_gives_five_null_columns():
    model = tiny_model()
    d = one_paper({"q": []})
    d["queries"] = make_table("queries", [{"arxiv_id": "2507.90001", "citation_key": "q"}])
    out = predict(model, d)
    assert out.num_rows == 1
    assert [out.column(f"cand_{i}")[0].as_py() for i in range(1, 6)] == [None] * 5


def test_t3_3_ties_broken_by_ref_arxiv_id_ascending():
    model = tiny_model()
    refs = ["2301.00009", "2301.00003", "2301.00007", "2301.00001", "2301.00005", "2301.00002", "2301.00004"]
    d = one_paper({"q": [(r, 0.5) for r in refs]})                      # đồng điểm hoàn toàn, thứ tự đầu vào lộn xộn
    row = predict(model, d).to_pylist()[0]
    assert [row[f"cand_{i}"] for i in range(1, 6)] == sorted(refs)[:5]  # tối đa 5, ref nhỏ đứng trước


def test_ranks_by_score_descending():
    model = tiny_model()
    d = one_paper({"q": [("2301.00001", 0.2), ("2301.00002", 0.95), ("2301.00003", 0.6)]})
    row = predict(model, d).to_pylist()[0]
    assert [row["cand_1"], row["cand_2"], row["cand_3"]] == ["2301.00002", "2301.00003", "2301.00001"]


def test_output_schema_dictionary_partition_sorted_keys_and_validated():
    d = make_synth()
    out = predict(fit(d), d)
    assert out.schema == SCHEMAS["predictions"] and pa.types.is_dictionary(out.schema.field("partition").type)
    validate_table("predictions", out)
    keys = list(zip(out.column("arxiv_id").to_pylist(), out.column("citation_key").to_pylist()))
    assert keys == sorted(keys)
    assert out.num_rows == d["queries"].num_rows                         # all_entries: mọi entry trong splits


def test_empty_queries_gives_empty_table_with_schema():
    d = make_synth()
    model = fit(d)
    d["queries"] = SCHEMAS["queries"].empty_table()
    d["pairs"] = d["pairs"].slice(0, 0)
    out = predict(model, d)
    assert out.num_rows == 0 and out.schema == SCHEMAS["predictions"]


def test_scope_labeled_only_vs_all_entries():
    d = make_synth()
    model = fit(d)
    allp = predict(model, d, scope="all_entries")
    lab = predict(model, d, scope="labeled_only")
    labeled = set(zip(d["labels"].column("arxiv_id").to_pylist(), d["labels"].column("citation_key").to_pylist()))
    got = set(zip(lab.column("arxiv_id").to_pylist(), lab.column("citation_key").to_pylist()))
    assert got == labeled and lab.num_rows < allp.num_rows
    unl = [r for r in allp.to_pylist() if (r["arxiv_id"], r["citation_key"]) not in labeled]
    assert unl and all(r["groundtruth_arxiv_id"] is None for r in unl)
    with pytest.raises(ValueError, match="scope"):
        predict(model, d, scope="everything")


def test_paper_missing_from_splits_is_dropped_with_warning():
    d = make_synth()
    model = fit(d)
    extra_q = make_table("queries", d["queries"].to_pylist() + [{"arxiv_id": "2507.99999", "citation_key": "z"}])
    d["queries"] = extra_q
    with pytest.warns(UserWarning, match="2507.99999"):
        out = predict(model, d)
    assert "2507.99999" not in out.column("arxiv_id").to_pylist()


def test_groundtruth_and_partition_joined_after_ranking_not_used_as_features():
    d = make_synth()
    model = fit(d)
    out = predict(model, d)
    truth = {(r["arxiv_id"], r["citation_key"]): r["groundtruth_arxiv_id"] for r in d["labels"].to_pylist()}
    for r in out.to_pylist():
        assert r["groundtruth_arxiv_id"] == truth.get((r["arxiv_id"], r["citation_key"]))
    # đổi nhãn (kể cả của bài test) không làm đổi thứ hạng: ranking không nhìn thấy nhãn
    flipped = make_table("labels", [{**r, "groundtruth_arxiv_id": "9999.99999"} if r["arxiv_id"].endswith(("11", "12", "13", "14")) else r
                                    for r in d["labels"].to_pylist()])
    out2 = predict_top5(model, queries=d["queries"], pairs=d["pairs"], labels=flipped, splits=d["splits"])
    cols = ["arxiv_id", "citation_key", *[f"cand_{i}" for i in range(1, 6)]]
    assert out.select(cols).equals(out2.select(cols))


def test_inputs_not_mutated():
    d = make_synth()
    before = {k: v.to_pylist() for k, v in d.items()}
    model = fit(d)
    predict(model, d)
    assert before == {k: v.to_pylist() for k, v in d.items()}


# ------------------------------------------------------------------ T3-4
def test_t3_4_fit_ignores_valid_and_test_labels():
    d = make_synth()
    part = {r["arxiv_id"]: r["partition"] for r in d["splits"].to_pylist()}
    train_only = make_table("labels", [r for r in d["labels"].to_pylist() if part[r["arxiv_id"]] == "train"])
    assert train_only.num_rows < d["labels"].num_rows
    full = fit(d)
    reduced = fit_ranker(pairs=d["pairs"], labels=train_only, splits=d["splits"], seed=2026)
    assert np.array_equal(coef(full)[0], coef(reduced)[0]) and np.array_equal(coef(full)[1], coef(reduced)[1])
    # nhãn valid/test thay đổi hoàn toàn, thậm chí trỏ tới ứng viên không tồn tại: vẫn không ảnh hưởng, không lỗi
    scrambled = make_table("labels", [{**r, "groundtruth_arxiv_id": "9999.99999"} if part[r["arxiv_id"]] != "train" else r
                                      for r in d["labels"].to_pylist()])
    again = fit_ranker(pairs=d["pairs"], labels=scrambled, splits=d["splits"], seed=2026)
    assert np.array_equal(coef(full)[0], coef(again)[0])


def test_t3_4_candidates_belong_to_the_paper_own_references():
    d = make_synth()
    out = predict(fit(d), d)
    refs: dict[str, set] = {}
    for r in d["references"].to_pylist():
        refs.setdefault(r["arxiv_id"], set()).add(r["ref_arxiv_id"])
    n = 0
    for r in out.to_pylist():
        for i in range(1, 6):
            if r[f"cand_{i}"] is not None:
                assert r[f"cand_{i}"] in refs[r["arxiv_id"]]
                n += 1
        cands = [r[f"cand_{i}"] for i in range(1, 6) if r[f"cand_{i}"] is not None]
        assert len(cands) == len(set(cands))
    assert n > 0


def test_t3_4_labeled_train_entry_with_groundtruth_not_in_pairs_raises_with_key():
    d = make_synth()
    first = d["labels"].to_pylist()[0]
    assert {r["arxiv_id"]: r["partition"] for r in d["splits"].to_pylist()}[first["arxiv_id"]] == "train"
    bad = make_table("labels", [{**first, "groundtruth_arxiv_id": "9999.99999"}] + d["labels"].to_pylist()[1:])
    with pytest.raises(ValueError, match=r"\[labels\].*" + first["arxiv_id"].replace(".", r"\.")) as e:
        fit_ranker(pairs=d["pairs"], labels=bad, splits=d["splits"], seed=1)
    assert first["citation_key"] in str(e.value) and "9999.99999" in str(e.value)


def test_unlabeled_entries_pairs_are_excluded_from_training():
    d = make_synth()
    model = fit(d)
    part = {r["arxiv_id"]: r["partition"] for r in d["splits"].to_pylist()}
    labeled = {(r["arxiv_id"], r["citation_key"]) for r in d["labels"].to_pylist() if part[r["arxiv_id"]] == "train"}
    n_rows = sum(1 for r in d["pairs"].to_pylist() if (r["arxiv_id"], r["citation_key"]) in labeled)
    c = model.train_counts_
    assert c["positive"] + c["negative"] == n_rows and c["positive"] == c["entries"] == len(labeled)


# ------------------------------------------------------------------ T3-5
def test_t3_5_one_class_raises_with_counts():
    d = make_synth()
    only_gt = [r for r in d["pairs"].to_pylist()
               if any(r["arxiv_id"] == l["arxiv_id"] and r["citation_key"] == l["citation_key"]
                      and r["ref_arxiv_id"] == l["groundtruth_arxiv_id"] for l in d["labels"].to_pylist())]
    pairs = make_table("pairs", only_gt)                                 # mỗi entry chỉ còn đúng cặp dương
    with pytest.raises(ValueError, match=r"\[train\].*\d+ mẫu dương, 0 mẫu âm"):
        fit_ranker(pairs=pairs, labels=d["labels"], splits=d["splits"], seed=1)


def test_t3_5_no_train_papers_raises_with_zero_counts():
    d = make_synth()
    splits = make_table("splits", [{**r, "partition": "valid"} for r in d["splits"].to_pylist()])
    with pytest.raises(ValueError, match=r"0 mẫu dương, 0 mẫu âm"):
        fit_ranker(pairs=d["pairs"], labels=d["labels"], splits=splits, seed=1)


# ------------------------------------------------------------------ T3-7
def test_t3_7_same_seed_same_coefficients_and_predictions():
    d = make_synth()
    m1, m2 = fit(d), fit(d)
    assert np.array_equal(coef(m1)[0], coef(m2)[0]) and np.array_equal(coef(m1)[1], coef(m2)[1])
    assert predict(m1, d).equals(predict(m2, d))


def test_t3_7_row_order_of_pairs_does_not_matter():
    d = make_synth()
    idx = list(range(d["pairs"].num_rows))
    random.Random(3).shuffle(idx)
    shuffled = dict(d, pairs=d["pairs"].take(pa.array(idx)))
    assert np.array_equal(coef(fit(d))[0], coef(fit(shuffled))[0])
    assert predict(fit(d), d).equals(predict(fit(shuffled), shuffled))


def test_t3_7_lgbm_ranker_deterministic():
    d = make_synth(n_train=14)
    params = {"n_estimators": 20, "num_leaves": 4, "min_child_samples": 2}
    m1, m2 = fit(d, estimator="lgbm_ranker", params=params), fit(d, estimator="lgbm_ranker", params=params)
    assert predict(m1, d).equals(predict(m2, d))
    assert m1.named_steps["ranker"].get_params()["n_jobs"] == 1


# ------------------------------------------------------------------ đặc trưng
def test_bool_to_float_null_kept_and_all_null_columns_dropped_and_recorded():
    d = make_synth()
    n = d["pairs"].num_rows
    pairs = d["pairs"].append_column(pa.field("dead_col", pa.float64()), pa.array([None] * n, pa.float64()))
    pairs = pairs.append_column(pa.field("note", pa.string()), pa.array(["x"] * n))
    model = fit_ranker(pairs=pairs, labels=d["labels"], splits=d["splits"], seed=1)
    assert model.features_used_ == ["title_similarity", "first_author_match", "year_diff", "arxiv_id_match"]
    assert model.features_dropped_ == ["dead_col"] and model.features_skipped_nonnumeric_ == ["note"]
    assert list(model.feature_names_in_) == model.features_used_
    # predict chỉ dùng đúng cột đã dùng: bỏ cột dead_col/note khỏi pairs khi dự đoán vẫn chạy
    out = predict_top5(model, queries=d["queries"], pairs=d["pairs"], labels=d["labels"], splits=d["splits"])
    assert out.num_rows == d["queries"].num_rows


def test_explicit_features_subset_used_in_given_order():
    d = make_synth()
    model = fit(d, features=["year_diff", "title_similarity"])
    assert model.features_used_ == ["year_diff", "title_similarity"]
    assert model.named_steps["clf"].coef_.shape == (1, 2)


@pytest.mark.parametrize("bad", sorted(FORBIDDEN_FEATURES))
def test_forbidden_column_in_pairs_raises_when_features_none(bad):
    d = make_synth()
    n = d["pairs"].num_rows
    pairs = d["pairs"].append_column(pa.field(bad, pa.float64()), pa.array([0.5] * n))
    with pytest.raises(ValueError, match=bad):
        fit_ranker(pairs=pairs, labels=d["labels"], splits=d["splits"], seed=1)


@pytest.mark.parametrize("bad", sorted(FORBIDDEN_FEATURES) + ["ref_arxiv_id"])
def test_forbidden_or_key_in_explicit_features_raises(bad):
    with pytest.raises(ValueError, match=bad):
        fit(make_synth(), features=["title_similarity", bad])


def test_unknown_or_nonnumeric_explicit_feature_raises():
    d = make_synth()
    with pytest.raises(ValueError, match="thiếu cột"):
        fit(d, features=["nope"])
    pairs = d["pairs"].append_column(pa.field("note", pa.string()), pa.array(["x"] * d["pairs"].num_rows))
    with pytest.raises(ValueError, match="không phải số"):
        fit_ranker(pairs=pairs, labels=d["labels"], splits=d["splits"], seed=1, features=["note"])


def test_no_feature_left_after_dropping_all_null_raises_with_column_list():
    d = make_synth()
    nulls = {"title_similarity": None, "first_author_match": None, "year_diff": None, "arxiv_id_match": None}
    pairs = make_table("pairs", [{**r, **nulls} for r in d["pairs"].to_pylist()])
    with pytest.raises(ValueError, match=r"không còn đặc trưng.*arxiv_id_match"):
        fit_ranker(pairs=pairs, labels=d["labels"], splits=d["splits"], seed=1)


def test_unknown_estimator_raises():
    with pytest.raises(ValueError, match="estimator"):
        fit(make_synth(), estimator="svm")


def test_model_without_feature_columns_in_pairs_raises_on_predict():
    d = make_synth()
    model = fit(d)
    slim = d["pairs"].select(["arxiv_id", "citation_key", "ref_arxiv_id", "title_similarity"])
    with pytest.raises(ValueError, match="thiếu cột đặc trưng"):
        predict_top5(model, queries=d["queries"], pairs=slim, labels=d["labels"], splits=d["splits"])


def test_pairs_entry_not_in_queries_raises():
    d = make_synth()
    model = fit(d)
    d["queries"] = d["queries"].slice(1)
    with pytest.raises(ValueError, match=r"\[pairs\].*queries"):
        predict(model, d)


def test_end_to_end_beats_chance_on_synthetic_signal():
    d = make_synth(n_train=30, n_valid=2, n_test=8)
    out = predict(fit(d), d)
    labeled_test = evaluate_mrr_at_5(predictions=out, labels=d["labels"], splits=d["splits"])
    assert labeled_test.mrr_at_5 > 0.5
