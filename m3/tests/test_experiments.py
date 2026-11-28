"""T3-8: `experiments.json` đầy đủ + kiểm tra từng thành phần. Dữ liệu tổng hợp: 2 thành viên, 24 bài, seed cố định."""
import json
import random

import numpy as np
import pyarrow as pa
import pytest

from m3.src import experiments as ex
from m3.src.contracts import (make_table, validate_pairs, validate_split_design, validate_splits, validate_table)
from m3.src.experiments import (bootstrap_ci, expected_random_rr, group_features, run_experiments)

MEMBERS = ("m1", "m2")


def make_design(seed: int = 11, n_manual: int = 6, n_auto: int = 6):
    """Mỗi thành viên: n_manual bài manual + n_auto bài auto; trong mỗi pool bài 0 -> test, bài 1 -> valid, còn lại train
    (đúng validate_split_design). Có cột đặc trưng phụ: author_overlap (float), doi_match (bool), page_ratio (other)."""
    rng = random.Random(seed)
    assignments, manual, auto, queries, refs, pairs, labels, splits = [], [], [], [], [], [], [], []
    n = 0
    for m in MEMBERS:
        for pool, count, reg in (("manual", n_manual, manual), ("auto", n_auto, auto)):
            for j in range(count):
                n += 1
                a = f"2507.{n:05d}"
                part = "test" if j == 0 else "valid" if j == 1 else "train"
                assignments.append({"arxiv_id": a, "member_id": m})
                reg.append({"arxiv_id": a, "member_id": m})
                splits.append({"arxiv_id": a, "partition": part})
                cands = [f"2301.{n * 10 + c:05d}" for c in range(rng.randint(3, 7))]
                refs += [{"arxiv_id": a, "ref_arxiv_id": c} for c in cands]
                for e in range(rng.randint(2, 4)):
                    k = f"k{e}"
                    queries.append({"arxiv_id": a, "citation_key": k})
                    gt = rng.choice(cands)
                    labels.append({"arxiv_id": a, "citation_key": k, "groundtruth_arxiv_id": gt, "label_source": pool, "member_id": m})
                    for c in cands:
                        pos = c == gt
                        pairs.append({
                            "arxiv_id": a, "citation_key": k, "ref_arxiv_id": c,
                            "title_similarity": min(1.0, max(0.0, rng.gauss(0.6 if pos else 0.5, 0.25))),
                            "first_author_match": rng.random() < (0.6 if pos else 0.4),
                            "year_diff": rng.choice([0, 1, 2]) if pos else rng.randint(0, 4),
                            "arxiv_id_match": True if (pos and rng.random() < 0.3) else None,
                            "author_overlap": min(1.0, max(0.0, rng.gauss(0.55 if pos else 0.45, 0.25))),
                            "doi_match": True if (pos and rng.random() < 0.2) else None,
                            "page_ratio": rng.random()})
    extra = [pa.field("author_overlap", pa.float64()), pa.field("doi_match", pa.bool_()), pa.field("page_ratio", pa.float64())]
    schema = pa.schema(list(make_table("pairs", []).schema) + extra)
    d = {"assignments": make_table("assignments", assignments), "manual_publications": make_table("manual_publications", manual),
         "auto_publications": make_table("auto_publications", auto), "queries": make_table("queries", queries),
         "references": make_table("references", refs), "pairs": pa.Table.from_pylist(pairs, schema=schema),
         "labels": make_table("labels", labels), "splits": make_table("splits", splits)}
    # dữ liệu hợp lệ theo contract
    validate_pairs(d["queries"], d["references"], d["pairs"])
    validate_splits(d["splits"], d["labels"], d["manual_publications"], d["auto_publications"])
    validate_split_design(d["splits"], d["manual_publications"], d["auto_publications"], d["assignments"])
    return d


def masked_versions(pairs: pa.Table, fractions=(0.0, 0.25, 0.5, 1.0), seed: int = 5) -> dict[float, pa.Table]:
    """Giả lập che bằng cách null hóa cột id/doi theo entry (KHÔNG dùng features.py): chỉ để thử đường chạy masked_pairs."""
    out = {}
    for f in fractions:
        rng = random.Random(seed)
        hide = {}
        rows = pairs.to_pylist()
        for r in rows:
            key = (r["arxiv_id"], r["citation_key"])
            hide.setdefault(key, rng.random() < f)
        for r in rows:
            if hide[(r["arxiv_id"], r["citation_key"])]:
                r["arxiv_id_match"] = r["doi_match"] = None
        out[f] = pa.Table.from_pylist(rows, schema=pairs.schema)
    return out


@pytest.fixture(scope="module")
def design():
    return make_design()


@pytest.fixture(scope="module")
def result(design, tmp_path_factory):
    out = tmp_path_factory.mktemp("exp_full")
    path = run_experiments(pairs=design["pairs"], labels=design["labels"], splits=design["splits"], seed=2026,
                           output_dir=out, masked_pairs=masked_versions(design["pairs"]))
    return path, json.loads(path.read_text(encoding="utf-8"))


def test_design_has_expected_shape(design):
    assert len(design["splits"]) == 24 and {r["partition"] for r in design["splits"].to_pylist()} == {"train", "valid", "test"}


# ------------------------------------------------------------------ T3-8
def test_t3_8_json_has_all_sections(result, design, tmp_path):
    path, r = result
    assert path.name == "experiments.json"
    for key in ("metadata", "data_summary", "random_baseline", "candidate_distribution", "title_baseline",
                "logistic_regression", "lightgbm_ranker", "ablation", "mask"):
        assert key in r, key
    # metadata
    md = r["metadata"]
    assert md["seed"] == 2026 and md["n_bootstrap"] == 1000 and md["run_at"]
    assert {"python", "scikit-learn", "lightgbm", "pyarrow"} <= set(md["versions"])
    assert md["commit"] is None or len(md["commit"]) >= 7
    # từng thí nghiệm: model, đặc trưng, siêu tham số, MRR valid, MRR test chung/manual/auto, CI
    for name in ("title_baseline", "logistic_regression", "lightgbm_ranker"):
        e = r[name]
        assert e["model"] and e["features"] and "hyperparameters" in e
        assert 0 <= e["valid"]["mrr_at_5"] <= 1 and e["valid"]["evaluated_entries"] > 0
        t = e["test"]
        for blk in (t, t["manual"], t["auto"]):
            assert 0 <= blk["mrr_at_5"] <= 1 and blk["entries"] > 0
            assert 0 <= blk["ci95"]["low"] <= blk["ci95"]["high"] <= 1 and blk["ci95"]["n_boot"] == 1000
        assert set(t["by_member"]) == set(MEMBERS)                        # MRR theo thành viên
        assert t["manual"]["entries"] + t["auto"]["entries"] == t["entries"]
        assert sum(b["entries"] for b in t["by_member"].values()) == t["entries"]
    assert r["logistic_regression"]["selection"]["chosen_index"] in range(4)
    assert len(r["lightgbm_ranker"]["selection"]["grid"]) == 4
    # ablation: ánh xạ cột -> nhóm, mỗi nhóm hiện diện được bỏ một lần
    ab = r["ablation"]
    assert ab["feature_groups"]["title_similarity"] == "title" and ab["feature_groups"]["doi_match"] == "id"
    assert ab["feature_groups"]["page_ratio"] == "other" and ab["feature_groups"]["author_overlap"] == "author"
    assert set(ab["runs"]) == {"drop_title", "drop_author", "drop_year", "drop_id", "drop_other"}
    assert all(run["test"]["entries"] > 0 for run in ab["runs"].values())
    assert "page_ratio" not in ab["runs"]["drop_other"]["features"] and "title_similarity" not in ab["runs"]["drop_title"]["features"]
    # mask 0/25/50/100%
    mk = r["mask"]
    assert mk["status"] == "ok" and set(mk["levels"]) == {"0", "0.25", "0.5", "1"} and mk["missing_levels"] == []
    for lvl in mk["levels"].values():                                     # mỗi mức che chạy cả LR lẫn LGBM
        for name in ("logistic_regression", "lightgbm_ranker"):
            m = lvl[name]
            assert 0 <= m["valid"]["mrr_at_5"] <= 1 and 0 <= m["test"]["mrr_at_5"] <= 1
            assert m["test"]["ci95"]["low"] <= m["test"]["ci95"]["high"] and set(m["test"]["by_member"]) == set(MEMBERS)
            assert m["selection"]["grid"] and m["hyperparameters"]
    assert mk["models"] == ["logistic_regression", "lightgbm_ranker"]
    for name in ("logistic_regression", "lightgbm_ranker"):
        assert "arxiv_id_match" not in mk["levels"]["1"][name]["features"]    # che 100% -> cột toàn null bị bỏ
    # phân bố ứng viên của bài test
    cd = r["candidate_distribution"]
    assert cd["papers"] == 4 and cd["min"] <= cd["median"] <= cd["max"] and sum(cd["histogram"].values()) == 4
    # mốc ngẫu nhiên
    assert 0 < r["random_baseline"]["test"]["mrr_at_5"] < 1
    # file hợp lệ JSON chuẩn (không NaN/Infinity)
    json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda c: pytest.fail(f"hằng số {c} trong JSON"))
    assert [p.name for p in path.parent.iterdir()] == ["experiments.json"]  # không để file rác


def test_mask_skipped_with_reason_when_masked_pairs_missing(design, tmp_path):
    p = run_experiments(pairs=design["pairs"], labels=design["labels"], splits=design["splits"], seed=1, output_dir=tmp_path)
    mk = json.loads(p.read_text())["mask"]
    assert mk["status"] == "skipped" and "masked_pairs" in mk["reason"]


def test_deterministic_except_run_at(design, tmp_path):
    kw = dict(pairs=design["pairs"], labels=design["labels"], splits=design["splits"], seed=3)
    a = json.loads(run_experiments(output_dir=tmp_path / "a", **kw).read_text())
    b = json.loads(run_experiments(output_dir=tmp_path / "b", **kw).read_text())
    a["metadata"].pop("run_at"), b["metadata"].pop("run_at")
    assert a == b


def test_inputs_not_mutated(design, tmp_path):
    names = ("pairs", "labels", "splits")
    before = {n: design[n].to_pylist() for n in names}
    run_experiments(pairs=design["pairs"], labels=design["labels"], splits=design["splits"], seed=1, output_dir=tmp_path)
    assert before == {n: design[n].to_pylist() for n in names}


def test_selection_on_valid_and_test_scored_once(design, tmp_path, monkeypatch):
    calls = []
    orig = ex._Context.evaluate

    def spy(self, model, pairs, part):
        calls.append((type(model).__name__ if not hasattr(model, "named_steps") else model.estimator_name_, part))
        return orig(self, model, pairs, part)

    monkeypatch.setattr(ex._Context, "evaluate", spy)
    p = run_experiments(pairs=design["pairs"], labels=design["labels"], splits=design["splits"], seed=1, output_dir=tmp_path)
    r = json.loads(p.read_text())
    assert calls.count(("logreg", "valid")) >= 4 and calls.count(("lgbm_ranker", "valid")) >= 4
    # test: title (1) + LR (1) + LGBM (1) + mỗi lần ablation (1) — không phụ thuộc kích thước lưới
    n_ablation = sum(1 for run in r["ablation"]["runs"].values() if "test" in run)
    assert sum(1 for _, part in calls if part == "test") == 3 + n_ablation
    # siêu tham số được chọn là argmax valid (đồng điểm: đứng trước trong lưới)
    for name in ("logistic_regression", "lightgbm_ranker"):
        sel = r[name]["selection"]
        best = max(range(len(sel["grid"])), key=lambda i: (sel["grid"][i]["valid_mrr_at_5"], -i))
        assert len({g["valid_mrr_at_5"] for g in sel["grid"]}) > 1      # lưới không hòa hết: phép chọn có ý nghĩa
        assert sel["chosen_index"] == best
        assert r[name]["valid"]["mrr_at_5"] == sel["grid"][best]["valid_mrr_at_5"]
    assert r["logistic_regression"]["hyperparameters"]["C"] == [0.01, 0.1, 1, 10][r["logistic_regression"]["selection"]["chosen_index"]]


def test_valid_or_test_without_labels_raises(design, tmp_path):
    only_train = make_table("splits", [{**s, "partition": "train"} if s["partition"] == "valid" else s
                                       for s in design["splits"].to_pylist()])
    with pytest.raises(ValueError, match="valid"):
        run_experiments(pairs=design["pairs"], labels=design["labels"], splits=only_train, seed=1, output_dir=tmp_path)


def test_invalid_mask_fraction_raises(design, tmp_path):
    with pytest.raises(ValueError, match="masked_pairs"):
        run_experiments(pairs=design["pairs"], labels=design["labels"], splits=design["splits"], seed=1,
                        output_dir=tmp_path, masked_pairs={1.5: design["pairs"]})


# ------------------------------------------------------------------ thành phần
def test_expected_random_rr_by_hand():
    assert expected_random_rr(0) == 0.0 and expected_random_rr(1) == 1.0
    assert expected_random_rr(2) == pytest.approx((1 + 1 / 2) / 2)
    assert expected_random_rr(3) == pytest.approx((1 + 1 / 2 + 1 / 3) / 3)
    assert expected_random_rr(7) == pytest.approx((1 + 1 / 2 + 1 / 3 + 1 / 4 + 1 / 5) / 7)


def test_random_baseline_matches_hand_computation(design, result):
    _, r = result
    n_of = {}
    for row in design["pairs"].to_pylist():
        n_of[(row["arxiv_id"], row["citation_key"])] = n_of.get((row["arxiv_id"], row["citation_key"]), 0) + 1
    test_papers = {s["arxiv_id"] for s in design["splits"].to_pylist() if s["partition"] == "test"}
    vals = [expected_random_rr(n_of[(l["arxiv_id"], l["citation_key"])])
            for l in design["labels"].to_pylist() if l["arxiv_id"] in test_papers]
    assert r["random_baseline"]["test"]["entries"] == len(vals)
    assert r["random_baseline"]["test"]["mrr_at_5"] == pytest.approx(sum(vals) / len(vals))


def test_bootstrap_ci_matches_manual_computation():
    scores = [1.0, 0.5, 0.0, 0.25, 1.0, 0.2, 0.0, 0.5]
    got = bootstrap_ci(scores, seed=9)
    rng = np.random.default_rng(9)
    means = [np.mean([scores[i] for i in rng.integers(0, 8, size=8)]) for _ in range(1000)]
    assert got["low"] == pytest.approx(np.percentile(means, 2.5)) and got["high"] == pytest.approx(np.percentile(means, 97.5))
    assert bootstrap_ci(scores, 9) == got                                  # tất định theo seed
    assert bootstrap_ci([0.5] * 6, 1)["low"] == bootstrap_ci([0.5] * 6, 1)["high"] == 0.5
    assert bootstrap_ci([], 1) is None


def test_group_features_mapping_and_new_columns_fall_into_groups():
    cols = ["title_similarity", "title_missing", "first_author_match", "author_overlap", "year_diff", "year_missing",
            "arxiv_id_match", "doi_match", "url_match", "page_ratio", "something_new"]
    g = group_features(cols)
    assert [g[c] for c in cols] == ["title", "title", "author", "author", "year", "year", "id", "id", "id", "other", "other"]


def test_candidate_distribution_by_hand():
    def mk(counts):
        pairs, labels, splits = [], [], []
        for i, c in enumerate(counts):
            a = f"2507.{i + 1:05d}"
            splits.append({"arxiv_id": a, "partition": "test"})
            labels.append({"arxiv_id": a, "citation_key": "k", "groundtruth_arxiv_id": "2301.00001", "label_source": "manual", "member_id": "m1"})
            pairs += [{"arxiv_id": a, "citation_key": "k", "ref_arxiv_id": f"2301.{j + 1:05d}", "title_similarity": 0.5} for j in range(c)]
        return make_table("pairs", pairs), make_table("labels", labels), make_table("splits", splits)
    pairs, labels, splits = mk([3, 7, 12, 3, 40])
    ctx = ex._Context.__new__(ex._Context)                                 # chỉ cần ctx.papers cho hàm thuần này
    ctx.papers = {"test": set(splits.column("arxiv_id").to_pylist())}
    cd = ex._candidate_distribution(ctx, pairs)
    assert (cd["papers"], cd["min"], cd["median"], cd["max"]) == (5, 3, 7.0, 40)
    assert cd["histogram"]["1-5"] == 2 and cd["histogram"]["6-10"] == 1 and cd["histogram"]["11-20"] == 1 and cd["histogram"]["21-50"] == 1
    assert sum(cd["histogram"].values()) == 5


def test_title_baseline_is_untrained_ranking_by_title_similarity(tmp_path):
    # title_similarity xếp đúng đáp án lên đầu ở mọi entry -> MRR = 1.0 mà không cần huấn luyện
    d = make_design()
    gt = {(l["arxiv_id"], l["citation_key"]): l["groundtruth_arxiv_id"] for l in d["labels"].to_pylist()}
    rows = [{**r, "title_similarity": 0.9 if r["ref_arxiv_id"] == gt[(r["arxiv_id"], r["citation_key"])] else 0.1}
            for r in d["pairs"].to_pylist()]
    pairs = pa.Table.from_pylist(rows, schema=d["pairs"].schema)
    r = json.loads(run_experiments(pairs=pairs, labels=d["labels"], splits=d["splits"], seed=1, output_dir=tmp_path).read_text())
    assert r["title_baseline"]["test"]["mrr_at_5"] == 1.0 and r["title_baseline"]["valid"]["mrr_at_5"] == 1.0
    assert r["title_baseline"]["features"] == ["title_similarity"] and r["title_baseline"]["selection"] is None


def test_pool_and_member_mrr_consistent_with_evaluate(design, result):
    from m3.src.evaluate import evaluate_mrr_at_5
    _, r = result
    # kiểm tra chéo bằng title baseline (không huấn luyện nên tái tạo được predictions độc lập với experiments.py)
    from m3.src.model import predict_top5
    model = ex.ColumnScoreModel("title_similarity")
    preds = predict_top5(model, queries=design["queries"], pairs=design["pairs"], labels=design["labels"], splits=design["splits"])
    res = evaluate_mrr_at_5(predictions=preds, labels=design["labels"], splits=design["splits"], manual=design["manual_publications"])
    t = r["title_baseline"]["test"]
    assert (t["mrr_at_5"], t["entries"], t["manual"]["mrr_at_5"], t["manual"]["entries"], t["auto"]["mrr_at_5"],
            t["auto"]["entries"]) == (res.mrr_at_5, res.evaluated_entries, res.manual_mrr_at_5, res.manual_entries,
                                      res.auto_mrr_at_5, res.auto_entries)


# ------------------------------------------------------------------ che có tác dụng thật
def _hand_rate(d, part, cols=("arxiv_id_match", "doi_match"), table=None):
    table = table if table is not None else d["pairs"]
    papers = {r["arxiv_id"] for r in d["splits"].to_pylist() if r["partition"] == part}
    rows = [r for r in table.to_pylist() if r["arxiv_id"] in papers]
    hit = sum(1 for r in rows if any(r[c] is not None for c in cols))
    return hit, len(rows)


def test_id_feature_nonnull_rate_matches_hand_count_and_falls_to_zero(design, result):
    _, r = result
    mk = r["mask"]
    assert set(mk["id_columns"]) == {"arxiv_id_match", "doi_match"} and "tử số" in mk["id_feature_nonnull_rate_definition"]
    masked = masked_versions(design["pairs"])
    rates = {}
    for frac, table in masked.items():
        lvl = mk["levels"][f"{frac:g}"]
        assert set(lvl["id_columns"]) == {"arxiv_id_match", "doi_match"}
        assert set(lvl["id_columns_used_by_logistic_regression"]) <= set(lvl["id_columns"])
        for part in ("train", "test"):
            hit, n = _hand_rate(design, part, table=table)
            got = lvl["id_feature_nonnull_rate"][part]
            assert (got["rows_with_nonnull_id"], got["rows"]) == (hit, n) and got["rate"] == hit / n
        rates[frac] = lvl["id_feature_nonnull_rate"]["train"]["rate"]
    assert rates[0.0] > rates[0.25] > rates[0.5] > rates[1.0] == 0.0          # mức che càng cao, đặc trưng id càng ít
    assert mk["levels"]["1"]["id_columns_used_by_logistic_regression"] == []  # cột id bị bỏ vì toàn null


def test_full_mask_with_remaining_id_values_raises_naming_columns(design, tmp_path):
    bad = {0.0: design["pairs"], 1.0: design["pairs"]}                          # "che 100%" nhưng chưa che gì
    with pytest.raises(ValueError, match=r"mức che 1.*arxiv_id_match") as e:
        run_experiments(pairs=design["pairs"], labels=design["labels"], splits=design["splits"], seed=1,
                        output_dir=tmp_path, masked_pairs=bad)
    assert "doi_match" in str(e.value) and "train" in str(e.value) and "test" in str(e.value)
    assert not (tmp_path / "experiments.json").exists()


def test_full_mask_leak_only_in_test_papers_also_raises(design, tmp_path):
    full = masked_versions(design["pairs"], fractions=(1.0,))[1.0]
    test_papers = {r["arxiv_id"] for r in design["splits"].to_pylist() if r["partition"] == "test"}
    rows = full.to_pylist()
    for r in rows:
        if r["arxiv_id"] in test_papers:
            r["doi_match"] = True
    leaky = pa.Table.from_pylist(rows, schema=full.schema)
    with pytest.raises(ValueError, match="doi_match"):
        run_experiments(pairs=design["pairs"], labels=design["labels"], splits=design["splits"], seed=1,
                        output_dir=tmp_path, masked_pairs={1.0: leaky})


def test_warning_when_id_group_misses_doi_or_url(result):
    _, r = result
    (w,) = r["mask"]["warnings"]                                                # design có arxiv_id_match + doi_match
    assert "arxiv_id_match" in w and "doi_match" in w and "không tác động lên url" in w and "arXiv ID/DOI" in w


def test_warning_when_id_group_has_only_arxiv_id_match(design, tmp_path):
    only_arxiv = {f: t.drop(["doi_match"]) for f, t in masked_versions(design["pairs"]).items()}
    r = json.loads(run_experiments(pairs=design["pairs"], labels=design["labels"], splits=design["splits"], seed=1,
                                   output_dir=tmp_path, masked_pairs=only_arxiv).read_text())
    assert r["mask"]["id_columns"] == ["arxiv_id_match"]
    assert r["mask"]["warnings"] == ["nhóm id chỉ gồm ['arxiv_id_match']: thí nghiệm che chỉ tác động lên arXiv ID, "
                                     "không tác động lên DOI/url"]


def test_warning_when_no_id_columns_and_no_warning_when_all_three_present(design, tmp_path):
    none = {f: t.drop(["arxiv_id_match", "doi_match"]) for f, t in masked_versions(design["pairs"]).items()}
    r = json.loads(run_experiments(pairs=design["pairs"], labels=design["labels"], splits=design["splits"], seed=1,
                                   output_dir=tmp_path / "n", masked_pairs=none).read_text())
    assert r["mask"]["id_columns"] == [] and "không có cột nhóm id" in r["mask"]["warnings"][0]
    assert r["mask"]["levels"]["1"]["id_feature_nonnull_rate"]["train"]["rate"] == 0.0
    assert ex._id_coverage_warning(["arxiv_id_match", "doi_match", "url_has_arxiv_id"]) is None


def test_nan_counts_as_null_in_id_rate():
    t = pa.table({"arxiv_id": ["a", "a", "a"], "citation_key": ["k"] * 3, "ref_arxiv_id": ["r1", "r2", "r3"],
                  "arxiv_id_match": pa.array([None, None, True], pa.bool_()),
                  "doi_score": pa.array([float("nan"), None, 0.0], pa.float64())})
    got = ex.id_nonnull_rate(t, {"a"}, ["arxiv_id_match", "doi_score"])
    assert (got["rows_with_nonnull_id"], got["rows"], got["columns_with_values"]) == (1, 3, ["arxiv_id_match", "doi_score"])
    assert ex.id_nonnull_rate(t, {"a"}, ["doi_score"])["rows_with_nonnull_id"] == 1      # chỉ dòng 0.0; NaN và null là null
