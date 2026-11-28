"""Synthetic tests of the catalog subset of TV1 (not real-corpus T1-8/T1-9)."""
import json

import pyarrow.parquet as pq
import pytest

from m1.src.utils.common import paper_key
from m3.src.contracts import make_table, validate_table
from m3.src.inputs import load_m1_tables, prepare_catalog

P = "2507.00001"
R = "2301.00001"
OLD = "hep-th/9901001"


def _paper(run, paper=P, *, validation="ok", status="ok", refs=None, title="Paper"):
    folder = run / "papers" / paper_key(paper)
    folder.mkdir(parents=True)
    files = {
        "metadata": {"arxiv_id": paper, "status": "ok", "title": title},
        "references": {"arxiv_id": paper, "status": status, "references": refs or []},
        "validation": {"arxiv_id": paper, "status": validation},
    }
    for name, data in files.items():
        (folder / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
    return folder


def _ref(ref_id=R, paper=P, **kwargs):
    return {"arxiv_id": paper, "ref_arxiv_id": ref_id, **kwargs}


def test_newest_validated_and_dedup(tmp_path, caplog):
    old, new, failed = (tmp_path / n for n in ("R1", "R2", "R3"))
    _paper(old, title="Old")
    _paper(new, title="New", refs=[_ref("arXiv:2301.00001v2", ref_title="A"),
                                  _ref(R, ref_title="B", ref_authors=["Ada"]), _ref(None)])
    _paper(failed, validation="failed", title="Failed")
    result = load_m1_tables([old, new, failed], [P])
    assert result.papers.to_pylist()[0]["title"] == "New"
    assert result.references.to_pylist()[0]["ref_arxiv_id"] == R
    assert result.references.to_pylist()[0]["ref_title"] == "B"
    assert result.crawl_status.to_pylist() == [{"arxiv_id": P, "status": "ok", "source_reference_count": 3, "candidate_count": 1}]
    assert "conflicting metadata" in caplog.text


def test_old_id_and_crawl_states(tmp_path):
    run = tmp_path / "run"
    _paper(run, OLD, status="empty")
    _paper(run, P, status="running", validation="running")
    bad = _paper(run, "2507.00003")
    (bad / "references.json").write_text("{bad", encoding="utf-8")
    _paper(run, "2507.00004", status="failed", validation="failed")
    _paper(run, "2507.00005", status="ok", validation="pending", refs=[_ref(paper="2507.00005")])
    result = load_m1_tables([run], [OLD, P, "2507.00002", "2507.00003", "2507.00004", "2507.00005"])
    states = {r["arxiv_id"]: r["status"] for r in result.crawl_status.to_pylist()}
    assert states == {OLD: "empty", P: "incomplete", "2507.00002": "missing",
                      "2507.00003": "failed", "2507.00004": "failed", "2507.00005": "incomplete"}
    assert OLD in result.papers.column("arxiv_id").to_pylist()
    assert (run / "papers" / "hep-th__9901001").exists()


def test_foreign_reference_rejected(tmp_path):
    run = tmp_path / "run"
    _paper(run, refs=[_ref(paper="2507.00002")])
    with pytest.raises(ValueError, match="expected"):
        load_m1_tables([run], [P])


def test_catalog_keeps_queries_and_is_deterministic(tmp_path):
    run = tmp_path / "run"
    _paper(run, status="empty")
    bibtex = tmp_path / "bibtex.parquet"
    pq.write_table(make_table("bibtex", [{"arxiv_id": P, "citation_key": "CaseSensitive"},
                                         {"arxiv_id": "2507.00002", "citation_key": "outside"}]), bibtex)
    outs = []
    for name in ("a", "b"):
        out = prepare_catalog(m1_runs=[run], consolidation_plan=None, bibtex_path=bibtex,
                              publication_ids=[P], output_dir=tmp_path / name)
        outs.append(out)
        for table_name in ("papers", "references", "crawl_status", "queries"):
            validate_table(table_name, pq.read_table(out / f"{table_name}.parquet"))
    assert pq.read_table(outs[0] / "queries.parquet").to_pylist() == [{"arxiv_id": P, "citation_key": "CaseSensitive"}]
    assert pq.read_table(outs[0] / "references.parquet").num_rows == 0
    for name in ("papers", "references", "crawl_status", "queries"):
        assert pq.read_table(outs[0] / f"{name}.parquet").equals(pq.read_table(outs[1] / f"{name}.parquet"))


def test_plan_mismatch(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    _paper(old)
    _paper(new)
    with pytest.raises(ValueError, match="consolidation_plan"):
        load_m1_tables([old, new], [P], consolidation_plan={P: old})


def test_duplicate_query_rejected_without_output(tmp_path):
    run = tmp_path / "run"
    _paper(run)
    bibtex = tmp_path / "bibtex.parquet"
    row = {"arxiv_id": P, "citation_key": "same"}
    pq.write_table(make_table("bibtex", [row, row]), bibtex)
    with pytest.raises(ValueError, match="queries"):
        prepare_catalog(m1_runs=[run], consolidation_plan=None, bibtex_path=bibtex,
                        publication_ids=[P], output_dir=tmp_path / "out")
    assert not (tmp_path / "out").exists()
