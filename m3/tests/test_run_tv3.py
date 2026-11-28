"""`run_tv3.run` end-to-end trên dữ liệu tổng hợp (không phải dữ liệu thật), chạy khi chặn socket."""
import hashlib
import json
import socket

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from m3.src import run_tv3
from m3.src.contracts import make_table, validate_table
from m3.src.evaluate import evaluate_mrr_at_5
from m3.src.run_tv3 import choose_model, main, run
from m3.tests.test_experiments import make_design, masked_versions

SEED = 2026


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_inputs(root, d, *, extra_query=True):
    root.mkdir(parents=True, exist_ok=True)
    queries = d["queries"]
    if extra_query:                                         # bài có query nhưng không có trong splits
        queries = make_table("queries", queries.to_pylist() + [{"arxiv_id": "2507.99999", "citation_key": "z"}])
    for name, table in (("pairs", d["pairs"]), ("labels", d["labels"]), ("splits", d["splits"]), ("queries", queries)):
        pq.write_table(table, root / f"{name}.parquet")
    return root


def write_masks(root, pairs):
    for frac, table in masked_versions(pairs).items():
        (root / f"mask_{frac:g}").mkdir(parents=True)
        pq.write_table(table, root / f"mask_{frac:g}" / "pairs.parquet")
    return root


@pytest.fixture(scope="module")
def design():
    return make_design()


@pytest.fixture(scope="module")
def full_run(design, tmp_path_factory):
    base = tmp_path_factory.mktemp("run_full")
    inp, masks = write_inputs(base / "in", design), write_masks(base / "masks", design["pairs"])
    with pytest.MonkeyPatch.context() as mp:                # toàn bộ lần chạy diễn ra khi mạng bị chặn
        def blocked(*a, **k):
            raise AssertionError("truy cập mạng bị chặn trong test offline")
        for fn in ("socket", "create_connection", "getaddrinfo"):
            mp.setattr(socket, fn, blocked)
        with pytest.raises(AssertionError):
            socket.socket()
        info = run(input_dir=inp, output_dir=base / "out", seed=SEED, masked_pairs_dir=masks)
    return base, inp, info


def test_four_files_written_and_nothing_else(full_run):
    base, _, info = full_run
    out = base / "out"
    assert sorted(p.name for p in out.iterdir()) == ["experiments.json", "handoff_G4.md", "metrics.json", "predictions.parquet"]
    assert [p.name for p in base.iterdir() if p.name.startswith(".")] == []          # không để thư mục tạm
    assert info["predictions"] == str(out / "predictions.parquet")


def test_predictions_valid_and_cover_all_papers_in_splits(full_run, design):
    base, _, _ = full_run
    preds = pq.read_table(base / "out" / "predictions.parquet")
    validate_table("predictions", preds)
    assert pa.types.is_dictionary(preds.schema.field("partition").type)
    papers = {r["arxiv_id"] for r in design["splits"].to_pylist()}
    assert set(preds.column("arxiv_id").to_pylist()) == papers                      # all_entries: mọi bài trong splits
    assert preds.num_rows == design["queries"].num_rows
    assert {r["partition"] for r in preds.to_pylist()} == {"train", "valid", "test"}
    keys = list(zip(preds.column("arxiv_id").to_pylist(), preds.column("citation_key").to_pylist()))
    assert keys == sorted(keys)


def test_metrics_content_and_mrr_matches_independent_evaluation(full_run, design):
    base, _, _ = full_run
    out = base / "out"
    m = json.loads((out / "metrics.json").read_text())
    exp = json.loads((out / "experiments.json").read_text())
    assert m["seed"] == SEED and m["scope"] == "all_entries"
    assert m["selected_model"]["name"] in ("logistic_regression", "lightgbm_ranker") and m["selected_model"]["reason"]
    assert m["hyperparameters"] and m["features_used"] and "features_dropped_all_null" in m
    assert m["commit"] is None or isinstance(m["commit"], str)
    assert {"scikit-learn", "lightgbm", "pyarrow"} <= set(m["versions"])
    assert m["predictions"]["papers_in_queries_not_in_splits"] == ["2507.99999"]
    assert m["masked_pairs_levels"] == [0.0, 0.25, 0.5, 1.0]
    for blk in (m["test"], m["test"]["manual"], m["test"]["auto"]):
        assert blk["ci95"]["low"] <= blk["ci95"]["high"] and blk["entries"] > 0
    # MRR trong metrics = chấm lại độc lập từ predictions.parquet đã ghi
    preds = pq.read_table(out / "predictions.parquet")
    res = evaluate_mrr_at_5(predictions=preds, labels=design["labels"], splits=design["splits"],
                            manual=design["manual_publications"])
    assert (m["test"]["mrr_at_5"], m["test"]["entries"]) == (res.mrr_at_5, res.evaluated_entries)
    assert (m["test"]["manual"]["mrr_at_5"], m["test"]["auto"]["mrr_at_5"]) == (res.manual_mrr_at_5, res.auto_mrr_at_5)
    # model được chọn theo MRR valid (hòa thì LR)
    lr, gb = exp["logistic_regression"]["valid"]["mrr_at_5"], exp["lightgbm_ranker"]["valid"]["mrr_at_5"]
    assert m["selected_model"]["name"] == ("lightgbm_ranker" if gb > lr else "logistic_regression")
    assert m["valid"] == exp[m["selected_model"]["name"]]["valid"]
    assert exp["mask"]["status"] == "ok"                                             # masked_pairs_dir đã được dùng


def test_handoff_has_paths_rows_sha256_and_checklist(full_run):
    base, inp, _ = full_run
    out = base / "out"
    text = (out / "handoff_G4.md").read_text()
    for name in ("predictions.parquet", "metrics.json", "experiments.json"):
        assert str(out.resolve() / name) in text and sha(out / name) in text
    assert f"{pq.ParquetFile(out / 'predictions.parquet').metadata.num_rows} (dòng dữ liệu)" in text
    assert f"--input {inp.resolve()} --output {out.resolve()} --seed {SEED} --scope all_entries --masked-pairs-dir" in text
    assert "predictions_scope: `all_entries`" in text and f"seed: `{SEED}`" in text
    for code in ("T3-1", "T3-2", "T3-3", "T3-4", "T3-5", "T3-6", "T3-7", "T3-8", "Tích hợp"):
        assert f"| {code} |" in text
    assert sha(inp / "pairs.parquet") in text


def test_same_seed_gives_identical_predictions_and_empty_existing_dir_is_allowed(full_run, design, tmp_path):
    base, inp, _ = full_run
    out2 = tmp_path / "out2"
    out2.mkdir()                                             # thư mục đã tồn tại nhưng rỗng: được phép
    run(input_dir=inp, output_dir=out2, seed=SEED)           # lần này không có masked_pairs
    assert sha(out2 / "predictions.parquet") == sha(base / "out" / "predictions.parquet")
    a, b = (json.loads((d / "metrics.json").read_text()) for d in (base / "out", out2))
    assert a["test"] == b["test"] and a["hyperparameters"] == b["hyperparameters"]
    assert json.loads((out2 / "experiments.json").read_text())["mask"]["status"] == "skipped"


def test_non_empty_output_dir_raises_and_is_untouched(design, tmp_path):
    inp = write_inputs(tmp_path / "in", design)
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("giữ nguyên")
    with pytest.raises(FileExistsError, match="không ghi đè"):
        run(input_dir=inp, output_dir=out, seed=SEED)
    assert [p.name for p in out.iterdir()] == ["keep.txt"] and (out / "keep.txt").read_text() == "giữ nguyên"
    f = tmp_path / "afile"
    f.write_text("x")
    with pytest.raises(FileExistsError):
        run(input_dir=inp, output_dir=f, seed=SEED)


def test_missing_input_file_raises_and_creates_nothing(design, tmp_path):
    inp = write_inputs(tmp_path / "in", design)
    (inp / "queries.parquet").unlink()
    with pytest.raises(FileNotFoundError, match="queries.parquet"):
        run(input_dir=inp, output_dir=tmp_path / "out", seed=SEED)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["in"]
    with pytest.raises(FileNotFoundError):
        run(input_dir=tmp_path / "nope", output_dir=tmp_path / "out", seed=SEED)


def test_input_tables_are_validated_on_read(design, tmp_path, monkeypatch):
    def must_not_run(**kw):
        raise AssertionError("run_experiments không được chạy khi bảng đầu vào không hợp lệ")

    monkeypatch.setattr(run_tv3, "run_experiments", must_not_run)         # lỗi phải đến từ bước đọc + validate
    inp = write_inputs(tmp_path / "in", design)
    bad = [{**r, "label_source": "robot"} for r in design["labels"].to_pylist()]
    pq.write_table(pa.Table.from_pylist(bad, schema=design["labels"].schema), inp / "labels.parquet")
    with pytest.raises(ValueError, match=r"\[labels\]"):
        run(input_dir=inp, output_dir=tmp_path / "out", seed=SEED)
    assert not (tmp_path / "out").exists()


def test_masked_pairs_dir_errors(design, tmp_path):
    inp = write_inputs(tmp_path / "in", design)
    (tmp_path / "empty").mkdir()
    with pytest.raises(FileNotFoundError, match="mask_"):
        run(input_dir=inp, output_dir=tmp_path / "o1", seed=SEED, masked_pairs_dir=tmp_path / "empty")
    (tmp_path / "half" / "mask_0.25").mkdir(parents=True)
    with pytest.raises(FileNotFoundError, match="pairs.parquet"):
        run(input_dir=inp, output_dir=tmp_path / "o2", seed=SEED, masked_pairs_dir=tmp_path / "half")
    with pytest.raises(FileNotFoundError):
        run(input_dir=inp, output_dir=tmp_path / "o3", seed=SEED, masked_pairs_dir=tmp_path / "missing")
    assert not any((tmp_path / n).exists() for n in ("o1", "o2", "o3"))


def test_invalid_scope_raises(design, tmp_path):
    with pytest.raises(ValueError, match="scope"):
        run(input_dir=write_inputs(tmp_path / "in", design), output_dir=tmp_path / "o", seed=SEED, scope="everything")


def test_failure_midway_leaves_no_output_and_no_temp_dir(design, tmp_path, monkeypatch):
    inp = write_inputs(tmp_path / "in", design)

    def boom(*, output_dir, **kw):
        (output_dir / "experiments.json").write_text("{}")
        raise RuntimeError("lỗi giữa chừng")

    monkeypatch.setattr(run_tv3, "run_experiments", boom)
    with pytest.raises(RuntimeError, match="giữa chừng"):
        run(input_dir=inp, output_dir=tmp_path / "out", seed=SEED)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["in"]


def test_cli_with_labeled_only_scope(design, tmp_path, capsys):
    inp = write_inputs(tmp_path / "in", design)
    code = main(["--input", str(inp), "--output", str(tmp_path / "out"), "--scope", "labeled_only", "--seed", "7"])
    assert code == 0
    text = capsys.readouterr().out
    assert "MRR@5 test" in text and str(tmp_path / "out") in text
    m = json.loads((tmp_path / "out" / "metrics.json").read_text())
    assert m["seed"] == 7 and m["scope"] == "labeled_only"
    preds = pq.read_table(tmp_path / "out" / "predictions.parquet")
    assert preds.num_rows == design["labels"].num_rows == m["predictions"]["rows"]
    assert "predictions_scope: `labeled_only`" in (tmp_path / "out" / "handoff_G4.md").read_text()


def test_choose_model_tie_prefers_logistic_regression():
    def e(lr, gb):
        out = {"logistic_regression": {"valid": {"mrr_at_5": lr}}}
        out["lightgbm_ranker"] = {"valid": {"mrr_at_5": gb}} if gb is not None else {"status": "skipped", "reason": "x"}
        return out
    assert choose_model(e(0.8, 0.8))[0] == "logistic_regression"
    assert choose_model(e(0.8, 0.7))[0] == "logistic_regression"
    assert choose_model(e(0.8, 0.8001))[0] == "lightgbm_ranker"
    name, reason = choose_model(e(0.8, None))
    assert name == "logistic_regression" and "bị bỏ qua" in reason


def test_cli_reports_errors_with_exit_code_1(design, tmp_path, capsys):
    inp = write_inputs(tmp_path / "in", design)
    out = tmp_path / "out"
    out.mkdir()
    (out / "x").write_text("x")
    assert main(["--input", str(inp), "--output", str(out)]) == 1
    assert "FileExistsError" in capsys.readouterr().err
    assert main(["--input", str(tmp_path / "nope"), "--output", str(tmp_path / "o2")]) == 1
    assert "FileNotFoundError" in capsys.readouterr().err


def test_output_files_are_world_readable(full_run):
    base, _, _ = full_run
    for p in (base / "out").iterdir():
        assert p.stat().st_mode & 0o044 == 0o044, p.name
