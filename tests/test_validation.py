import pytest

from src.utils.common import read_json, write_json_atomic
from src.downloader.http import make_manifest
from src.stages.validation import (validate_manifest, validate_metadata, validate_paper,
                            validate_references, validate_run)


def codes(report):
    return {issue["code"] for issue in report["issues"]}


def test_valid_multiversion_corpus_empty_sources_and_unknown_reference_metadata(corpus):
    cfg, identity, _ = corpus
    report = validate_paper(cfg, identity)
    assert report["status"] == "ok"
    assert {"VALID_EMPTY", "MISSING_METADATA"} <= codes(report)
    assert not [i for i in report["issues"] if i["severity"] == "error"]


def test_reports_missing_file_without_deleting_records(corpus):
    cfg, identity, paper = corpus
    (paper / "versions/v1/source/main.tex").unlink()
    manifest = paper / "versions/v1/manifest.json"
    before = manifest.read_bytes()
    report = validate_paper(cfg, identity)
    assert report["status"] == "failed"
    assert "MISSING_SOURCE" in codes(report)
    assert manifest.read_bytes() == before


@pytest.mark.parametrize("field,value,code", [
    ("version", True, "INVALID_VERSION"),
    ("download_status", [], "INVALID_STATUS"),
    ("extract_status", "success", "INVALID_STATUS"),
    ("source_files", ["../escape.tex"], "UNSAFE_PATH"),
    ("archive_path", "/tmp/escape", "UNSAFE_PATH"),
    ("archive_bytes", False, "INVALID_SIZE"),
    ("kept_source_bytes", -1, "INVALID_SIZE"),
    ("source_files", ["main.tex", "./main.tex"], "DUPLICATE_SOURCE"),
    ("source_files", ["image.png"], "INVALID_SOURCE_TYPE"),
    ("source_format", {}, "INVALID_FORMAT"),
    ("arxiv_id", None, "INVALID_ID"),
])
def test_invalid_manifest_types_and_paths_are_reported_not_raised(field, value, code):
    manifest = make_manifest("2507.01659", 1)
    manifest[field] = value
    assert code in {issue["code"] for issue in validate_manifest(manifest)}


def test_inconsistent_status_and_file_sizes(corpus):
    cfg, identity, paper = corpus
    path = paper / "versions/v1/manifest.json"
    manifest = read_json(path)
    manifest.update(extract_status="empty", error_code="NO_TEX_SOURCE", kept_source_bytes=0)
    write_json_atomic(path, manifest)
    report = validate_paper(cfg, identity)
    assert {"INCONSISTENT_STATUS", "SIZE_MISMATCH"} <= codes(report)


def test_symlink_and_unlisted_source(corpus, tmp_path):
    cfg, identity, paper = corpus
    source = paper / "versions/v1/source"
    (source / "image.png").write_bytes(b"not allowed")
    (source / "external.tex").symlink_to(tmp_path / "outside.tex")
    report = validate_paper(cfg, identity)
    assert {"UNSAFE_PATH", "UNDECLARED_SOURCE"} <= codes(report)


def test_duplicate_versions_and_discovery_mismatch(corpus):
    cfg, identity, paper = corpus
    path = paper / "metadata.json"
    metadata = read_json(path)
    metadata["versions"] = [1, 1, 2]
    write_json_atomic(path, metadata)
    report = validate_paper(cfg, identity, expected_versions=[1, 2, 3])
    assert {"DUPLICATE_VERSION", "DISCOVERY_MISMATCH", "NOT_COLLECTED"} <= codes(report)


def test_wrong_version_identity_and_extra_version(corpus):
    cfg, identity, paper = corpus
    manifest = read_json(paper / "versions/v1/manifest.json")
    manifest["arxiv_id"] = "2507.01660"
    write_json_atomic(paper / "versions/v3/manifest.json", manifest)
    assert {"UNEXPECTED_VERSION", "INVALID_ID", "INVALID_VERSION"} <= codes(validate_paper(cfg, identity))


def test_not_collected_and_running_are_distinct_from_invalid_and_empty(corpus):
    cfg, identity, paper = corpus
    path = paper / "references.json"
    path.unlink()
    assert validate_paper(cfg, identity)["status"] == "pending"
    running = dict(arxiv_id=identity, source="semantic_scholar", fetched_at=None,
                   status="running", error_code=None, references=[])
    write_json_atomic(path, running)
    assert validate_paper(cfg, identity)["status"] == "running"
    running.update(status="empty", error_code="NO_REFERENCES_RETURNED")
    write_json_atomic(path, running)
    assert validate_paper(cfg, identity)["status"] == "ok"
    running.update(status="failed", error_code="TIMEOUT")
    write_json_atomic(path, running)
    assert "COLLECTION_FAILED" in codes(validate_paper(cfg, identity))


def test_corrupt_json_does_not_stop_other_papers(corpus):
    cfg, identity, paper = corpus
    (paper / "metadata.json").write_bytes(b"{broken")
    report = validate_run(cfg, [identity, identity, "2507.01660", "../bad", None])
    assert report["status"] == "failed"
    assert {"UNREADABLE_RECORD", "DUPLICATE_PAPER", "NOT_COLLECTED", "INVALID_ID"} <= codes(report)
    assert len(report["papers"]) == 3


def test_duplicate_references_and_missing_metadata_are_reported_but_retained(corpus):
    cfg, identity, paper = corpus
    path = paper / "references.json"
    refs = read_json(path)
    refs["references"] *= 2
    write_json_atomic(path, refs)
    report = validate_paper(cfg, identity)
    assert "DUPLICATE_REFERENCE" in codes(report)
    assert len(read_json(path)["references"]) == 2


def test_missing_manifest_fields_and_metadata_date_errors(corpus):
    _, identity, paper = corpus
    manifest = make_manifest(identity, 1)
    del manifest["archive_bytes"]
    assert "MISSING_FIELD" in {i["code"] for i in validate_manifest(manifest)}
    metadata = read_json(paper / "metadata.json")
    metadata.update(authors="An", submission_date="2025", versions=[1, 3], latest_version=3)
    assert {"INVALID_TYPE", "INVALID_DATE", "MISSING_VERSION"} <= {
        i["code"] for i in validate_metadata(metadata)}


def test_malformed_nested_reference_types_and_status_do_not_crash(corpus):
    cfg, identity, paper = corpus
    refs = read_json(paper / "references.json")
    refs["status"] = []
    refs["references"][0].update(ref_arxiv_id=[], ref_authors=None)
    write_json_atomic(paper / "references.json", refs)
    assert validate_paper(cfg, identity)["status"] == "failed"
    assert "INVALID_STATUS" in {i["code"] for i in validate_references(refs)}


def test_validate_run_uses_run_input_ids(corpus):
    cfg, identity, _ = corpus
    write_json_atomic(cfg["output_root"] / cfg["run_id"] / "run.json", {"input_ids": [identity]})
    report = validate_run(cfg)
    assert report["status"] == "ok"
    assert len(report["papers"]) == 1


def test_duplicate_paper_in_unexpected_folder_is_detected(corpus):
    cfg, identity, paper = corpus
    duplicate = paper.parent / "incorrect-key" / "metadata.json"
    write_json_atomic(duplicate, read_json(paper / "metadata.json"))
    assert {"DUPLICATE_PAPER", "IDENTITY_MISMATCH"} <= codes(validate_run(cfg, [identity]))
