import copy
import json

import pytest

from src import common, storage
from src.downloader.http import make_manifest

ID = "hep-th/9901001"


@pytest.fixture
def cfg(tmp_path):
    return {"output_root": tmp_path, "run_id": "offline"}


def metadata():
    return dict(arxiv_id=ID, title="Paper", authors=["An"],
                submission_date="1999-01-01T00:00:00+00:00", revised_dates=[],
                venue=None, latest_version=1, versions=[1], status="ok", error_code=None)


def references():
    return dict(arxiv_id=ID, status="ok", error_code=None, source="semantic_scholar",
                fetched_at="2026-10-23T00:00:00+00:00", references=[
                    dict(arxiv_id=ID, ref_arxiv_id=None, ref_title=None,
                         ref_authors=[], ref_submission_date=None, ref_s2_id=None),
                    dict(arxiv_id=ID, ref_arxiv_id="2507.01659", ref_title="Bài báo",
                         ref_authors=["Bình"], ref_submission_date=None, ref_s2_id="s2-1")])


def test_round_trip_all_records_preserves_nulls_old_ids_and_missing_metadata(cfg):
    meta, refs = metadata(), references()
    storage.save_metadata(cfg, ID, meta)
    ref_path = storage.save_references(cfg, ID, refs)
    manifest = make_manifest(ID, 1)
    storage.save_manifest(cfg, ID, 1, manifest)
    assert "hep-th__9901001" in ref_path.as_posix()
    assert storage.load_metadata(cfg, ID) == meta
    assert storage.load_references(cfg, ID) == refs
    assert storage.load_manifest(cfg, ID, 1) == manifest
    assert storage.load_paper(cfg, ID)["versions"] == {1: manifest}
    assert storage.load_references(cfg, ID)["references"][0]["ref_arxiv_id"] is None


def test_rerun_does_not_duplicate_or_rewrite(cfg):
    refs = references()
    path = storage.save_references(cfg, ID, refs)
    before = path.stat().st_mtime_ns
    for _ in range(3):
        assert storage.save_references(cfg, ID, refs) == path
    assert path.stat().st_mtime_ns == before
    assert len(json.loads(path.read_text())["references"]) == 2


def test_failed_or_incomplete_result_cannot_replace_good_metadata_or_references(cfg):
    meta, refs = metadata(), references()
    storage.save_metadata(cfg, ID, meta)
    storage.save_references(cfg, ID, refs)
    failed = dict(refs, status="failed", error_code="TIMEOUT", references=[])
    storage.save_references(cfg, ID, failed)
    storage.save_references(cfg, ID, dict(refs, references=refs["references"][:1]))
    less = copy.deepcopy(refs)
    less["references"][1]["ref_title"] = None
    storage.save_references(cfg, ID, less)
    storage.save_metadata(cfg, ID, dict(meta, status="failed", versions=None, title=None))
    assert storage.load_metadata(cfg, ID) == meta
    assert storage.load_references(cfg, ID) == refs


@pytest.mark.parametrize("replace_with_other_reference", [False, True])
def test_reference_occurrences_cannot_be_removed(cfg, replace_with_other_reference):
    refs = references()
    ref_a, ref_b = refs["references"][1], refs["references"][0]
    refs["references"] = [copy.deepcopy(ref_a), copy.deepcopy(ref_a)]
    if replace_with_other_reference:
        refs["references"].append(ref_b)
    path = storage.save_references(cfg, ID, refs)
    before = path.read_bytes()
    fewer = copy.deepcopy(refs)
    fewer["references"].pop(0)
    if replace_with_other_reference:
        # Equal list lengths still must not hide a lost occurrence of ref_a.
        fewer["references"].append(copy.deepcopy(ref_b))
    storage.save_references(cfg, ID, fewer)
    assert path.read_bytes() == before
    assert storage.load_references(cfg, ID) == refs


def test_duplicate_references_can_each_be_enriched(cfg):
    refs = references()
    ref = refs["references"][1]
    refs["references"] = [copy.deepcopy(ref), copy.deepcopy(ref)]
    storage.save_references(cfg, ID, refs)
    richer = copy.deepcopy(refs)
    for reference in richer["references"]:
        reference["ref_submission_date"] = "2025-07-01"
    storage.save_references(cfg, ID, richer)
    assert storage.load_references(cfg, ID) == richer


@pytest.mark.parametrize("reverse_old", [False, True])
@pytest.mark.parametrize("reverse_new", [False, True])
def test_reference_enrichment_matches_distinct_occurrences_regardless_of_order(cfg, reverse_old, reverse_new):
    refs = references()  # Unknown metadata first, then a known reference.
    richer = copy.deepcopy(refs)
    known = richer["references"][1]
    known["ref_submission_date"] = "2025-07-01"
    other = dict(richer["references"][0], ref_title="Another reference", ref_s2_id="s2-2")
    richer["references"] = [known, other]
    if reverse_old:
        refs["references"].reverse()
    if reverse_new:
        richer["references"].reverse()
    storage.save_references(cfg, ID, refs)
    storage.save_references(cfg, ID, richer)
    assert storage.load_references(cfg, ID) == richer


def test_nested_author_occurrences_cannot_be_removed(cfg):
    refs = references()
    refs["references"][1]["ref_authors"] = ["Bình", "Bình"]
    storage.save_references(cfg, ID, refs)
    fewer = copy.deepcopy(refs)
    fewer["references"][1]["ref_authors"].pop()
    storage.save_references(cfg, ID, fewer)
    assert storage.load_references(cfg, ID) == refs


def test_enrichment_can_fill_unknown_values(cfg):
    meta = metadata()
    storage.save_metadata(cfg, ID, meta)
    richer = dict(meta, venue="Journal", latest_version=2, versions=[1, 2],
                  revised_dates=["2000-01-01T00:00:00+00:00"])
    # latest_version is derived, so allow its legitimate increase with versions.
    storage.save_metadata(cfg, ID, richer)
    assert storage.load_metadata(cfg, ID)["venue"] == "Journal"


def test_missing_differs_from_invalid_json_and_corrupt_file_is_not_overwritten(cfg):
    assert storage.load_metadata(cfg, ID) is None
    path = storage.save_metadata(cfg, ID, metadata())
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        storage.load_metadata(cfg, ID)
    with pytest.raises(json.JSONDecodeError):
        storage.save_metadata(cfg, ID, metadata())
    assert path.read_text() == "{broken"


def test_empty_version_is_saved_and_protected(cfg):
    manifest = make_manifest(ID, 1)
    manifest.update(download_status="ok", extract_status="empty", source_format="pdf",
                    error_code="NO_TEX_SOURCE", archive_bytes=12,
                    unpacked_total_bytes=12, kept_source_bytes=0)
    storage.save_manifest(cfg, ID, 1, manifest)
    storage.save_manifest(cfg, ID, 1, make_manifest(ID, 1))
    assert storage.load_manifest(cfg, ID, 1) == manifest


@pytest.mark.parametrize("bad_id", ["../escape", "", "2507.01659v1", "/tmp/x", "2500.12345"])
def test_rejects_bad_ids_before_writing(cfg, bad_id):
    with pytest.raises(ValueError):
        storage.save_metadata(cfg, bad_id, dict(metadata(), arxiv_id=bad_id))


def test_rejects_manifest_traversal_and_wrong_identity(cfg):
    manifest = make_manifest(ID, 1)
    manifest["source_files"] = ["../escape.tex"]
    with pytest.raises(ValueError):
        storage.save_manifest(cfg, ID, 1, manifest)
    with pytest.raises(ValueError):
        storage.save_manifest(cfg, ID, True, make_manifest(ID, 1))


def test_atomic_write_failure_preserves_previous_file_and_cleans_temporary_files(tmp_path, monkeypatch):
    path = tmp_path / "record.json"
    common.write_json_atomic(path, {"good": True})
    def fail_replace(*args):
        raise OSError("simulated interruption")
    monkeypatch.setattr(common.os, "replace", fail_replace)
    with pytest.raises(OSError):
        common.write_json_atomic(path, {"bad": True})
    assert json.loads(path.read_text()) == {"good": True}
    assert not list(tmp_path.glob("*.tmp"))
