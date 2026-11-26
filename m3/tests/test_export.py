"""Test cho export.py: xuất dataset Hugging Face và kiểm tra bằng check_dataset."""
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from m3.src.contracts import HF_TABLES, fixture, make_table
from m3.src.export import EXTRA_SCHEMAS, HF_SCHEMAS, DatasetInputs, check_dataset, export_dataset

P, P2, P3 = "2507.00001", "2507.00002", "2507.00003"
OTHER_REF = "2301.00077"   # chỉ tồn tại trong references của bài P2
ALL = [P, P2, P3]


def make_inputs(predictions="fixture") -> DatasetInputs:
    f = fixture()
    papers = make_table("papers", [{"arxiv_id": a, "title": f"Paper {a}"} for a in ALL])
    references = make_table("references", f["references"].to_pylist()
                            + [{"arxiv_id": P2, "ref_arxiv_id": OTHER_REF}])
    bibtex = make_table("bibtex", [{"arxiv_id": P, "citation_key": k, "entry_type": "article"} for k in ("q1", "q2", "q3")]
                        + [{"arxiv_id": P2, "citation_key": "q4", "entry_type": "article"}])
    elements = pa.Table.from_pylist([
        {"element_id": "2507-00001#root1", "arxiv_id": P, "kind": "document", "content": "root"},
        {"element_id": "2507-00001#s1", "arxiv_id": P, "kind": "sentence", "content": "Hello."},
        {"element_id": "2507-00002#root1", "arxiv_id": P2, "kind": "document", "content": "root"}],
        schema=EXTRA_SCHEMAS["elements"])
    hierarchy = pa.Table.from_pylist([
        {"arxiv_id": P, "version": 1, "element_id": "2507-00001#s1", "parent_id": "2507-00001#root1"}],
        schema=EXTRA_SCHEMAS["hierarchy"])
    preds = f["predictions"] if predictions == "fixture" else predictions
    return DatasetInputs(papers=papers, references=references, elements=elements, hierarchy=hierarchy,
                         bibtex=bibtex, splits=f["splits"], labels=f["labels"], predictions=preds)


def test_export_schema_and_row_counts(tmp_path):
    res = export_dataset(make_inputs(), output_dir=tmp_path / "ds", publication_ids=ALL)
    for name in HF_TABLES:
        files = sorted((tmp_path / "ds" / "data" / name).glob("*.parquet"))
        t = pa.concat_tables([pq.read_table(p) for p in files])
        assert t.schema.names == HF_SCHEMAS[name].names, name
        assert all(t.schema.field(n).type == HF_SCHEMAS[name].field(n).type for n in t.schema.names), name
        assert sum(pq.ParquetFile(p).metadata.num_rows for p in files) == t.num_rows == res.row_counts[name]
    assert check_dataset(tmp_path / "ds", publication_ids=ALL)["ok"]


def test_check_dataset_detects_candidate_from_other_paper(tmp_path):
    rows = fixture()["predictions"].to_pylist()
    rows[0]["cand_1"] = OTHER_REF                      # ID chỉ có ở bài P2
    bad = pa.Table.from_pylist(rows, schema=fixture()["predictions"].schema)
    export_dataset(make_inputs(bad), output_dir=tmp_path / "ds", publication_ids=ALL)
    r = check_dataset(tmp_path / "ds", publication_ids=ALL)
    assert not r["ok"] and any("không thuộc references" in e and "cand_1" in e for e in r["errors"])


def test_export_slices_by_publication_ids(tmp_path):
    export_dataset(make_inputs(), output_dir=tmp_path / "ds", publication_ids=[P])
    for name in HF_TABLES:
        t = pa.concat_tables([pq.read_table(p) for p in (tmp_path / "ds" / "data" / name).glob("*.parquet")])
        assert set(t["arxiv_id"].to_pylist()) <= {P}, name
    preds = pq.read_table(next((tmp_path / "ds" / "data" / "predictions").glob("*.parquet")))
    assert set(preds["partition"].to_pylist()) == {"test"}          # partition lấy từ splits chung
    assert check_dataset(tmp_path / "ds", publication_ids=[P])["ok"]


def test_dataset_card_has_six_configs_and_loads_streaming(tmp_path):
    export_dataset(make_inputs(), output_dir=tmp_path / "ds", publication_ids=ALL)
    readme = (tmp_path / "ds" / "README.md").read_text(encoding="utf-8")
    assert readme.count("config_name:") == 6
    assert not list((tmp_path / "ds").rglob("*.py"))              # không có script nạp tùy biến
    datasets = pytest.importorskip("datasets")
    for name in HF_TABLES:
        ds = datasets.load_dataset(str(tmp_path / "ds"), name, split="train", streaming=True)
        next(iter(ds), None)                                      # bảng rỗng trả None, không lỗi


def test_export_with_empty_predictions(tmp_path):
    res = export_dataset(make_inputs(predictions=None), output_dir=tmp_path / "ds", publication_ids=ALL)
    assert res.row_counts["predictions"] == 0 and res.shard_counts["predictions"] == 1
    assert check_dataset(tmp_path / "ds", publication_ids=ALL)["ok"]


def test_predictions_scope_changes_row_count(tmp_path):
    a = export_dataset(make_inputs(), output_dir=tmp_path / "a", publication_ids=ALL, predictions_scope="all_entries")
    b = export_dataset(make_inputs(), output_dir=tmp_path / "b", publication_ids=ALL, predictions_scope="labeled_only")
    assert (a.row_counts["predictions"], b.row_counts["predictions"]) == (3, 2)   # q3 chưa có nhãn
    with pytest.raises(ValueError):
        export_dataset(make_inputs(), output_dir=tmp_path / "c", publication_ids=ALL, predictions_scope="x")


def test_no_internal_tables_and_output_dir_must_be_new(tmp_path):
    export_dataset(make_inputs(), output_dir=tmp_path / "ds", publication_ids=ALL)
    assert {p.name for p in (tmp_path / "ds" / "data").iterdir()} == set(HF_TABLES)
    with pytest.raises(FileExistsError):
        export_dataset(make_inputs(), output_dir=tmp_path / "ds", publication_ids=ALL)