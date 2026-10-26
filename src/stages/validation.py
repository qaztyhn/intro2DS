"""Read-only M1 schema and corpus validation. Never deletes or repairs records."""

import argparse
import json
import pathlib
from datetime import datetime

from ..common import paper_key

from ..schema import (MANIFEST_FIELDS, NO_TEX_SOURCE, REFERENCE_FIELDS,
                     SOURCE_FORMATS, STATUSES, paper_directory, relative_path,
                     safe_path, valid_arxiv_id)


def _issue(issues, code, path, message, severity="error"):
    issues.append(dict(code=code, path=str(path), message=message, severity=severity))


def _required(issues, record, fields, path):
    for field in sorted(fields - record.keys()):
        _issue(issues, "MISSING_FIELD", f"{path}.{field}", "required field is missing")


def _status(issues, record, field, path):
    value = record.get(field)
    if not isinstance(value, str) or value not in STATUSES:
        _issue(issues, "INVALID_STATUS", f"{path}.{field}", "unknown status")
        return None
    return value


def _positive(value):
    return type(value) is int and value > 0


def _nullable_string(issues, record, field, path):
    value = record.get(field)
    if value is not None and not isinstance(value, str):
        _issue(issues, "INVALID_TYPE", f"{path}.{field}", "expected string or null")


def validate_manifest(record, *, arxiv_id=None, version=None, source_dir=None):
    """Validate record shape and, optionally, the files relative to source/."""
    issues, path = [], "manifest"
    if not isinstance(record, dict):
        _issue(issues, "INVALID_TYPE", path, "expected object")
        return issues
    _required(issues, record, MANIFEST_FIELDS, path)
    identity = record.get("arxiv_id")
    number = record.get("version")
    if not valid_arxiv_id(identity) or (arxiv_id is not None and identity != arxiv_id):
        _issue(issues, "INVALID_ID", path + ".arxiv_id", "missing, invalid or mismatched ID")
    if not _positive(number) or (version is not None and number != version):
        _issue(issues, "INVALID_VERSION", path + ".version", "expected positive matching version")
    if record.get("versioned_id") != f"{identity}v{number}":
        _issue(issues, "IDENTITY_MISMATCH", path + ".versioned_id", "versioned ID mismatch")
    download = _status(issues, record, "download_status", path)
    status = _status(issues, record, "extract_status", path)
    _status(issues, record, "parse_status", path)
    source_format = record.get("source_format")
    if source_format is not None and (not isinstance(source_format, str)
                                      or source_format not in SOURCE_FORMATS):
        _issue(issues, "INVALID_FORMAT", path + ".source_format", "unknown source format")
    _nullable_string(issues, record, "error_code", path)
    for key in ("archive_bytes", "unpacked_total_bytes", "kept_source_bytes"):
        value = record.get(key)
        if value is not None and (type(value) is not int or value < 0):
            _issue(issues, "INVALID_SIZE", path + "." + key, "expected non-negative integer or null")
    before, after = record.get("unpacked_total_bytes"), record.get("kept_source_bytes")
    if type(before) is int and type(after) is int and after > before:
        _issue(issues, "INVALID_SIZE", path, "kept size exceeds unpacked total")
    archive_path = record.get("archive_path")
    if archive_path is not None:
        try:
            relative_path(archive_path)
        except ValueError as exc:
            _issue(issues, "UNSAFE_PATH", path + ".archive_path", str(exc))
    files = record.get("source_files")
    if not isinstance(files, list):
        _issue(issues, "INVALID_TYPE", path + ".source_files", "expected list")
        files = []
    names, total, all_present = set(), 0, True
    for index, name in enumerate(files):
        field = f"manifest.source_files[{index}]"
        try:
            name = relative_path(name)
            if name in names:
                _issue(issues, "DUPLICATE_SOURCE", field, "duplicate normalized file path")
            names.add(name)
            if pathlib.PurePosixPath(name).suffix.lower() not in {".tex", ".bib"}:
                _issue(issues, "INVALID_SOURCE_TYPE", field, "only .tex/.bib files are allowed")
            if source_dir is not None:
                target = safe_path(source_dir, name)
                if not target.is_file():
                    _issue(issues, "MISSING_SOURCE", field, f"missing file: {target}")
                    all_present = False
                else:
                    total += target.stat().st_size
        except (ValueError, OSError) as exc:
            all_present = False
            _issue(issues, "UNSAFE_PATH", field, str(exc))
    has_tex = any(pathlib.PurePosixPath(name).suffix.lower() == ".tex" for name in names)
    if status in {"ok", "empty"}:
        if download != "ok":
            _issue(issues, "INCONSISTENT_STATUS", path, "extraction completed without successful download")
        if source_format is None or source_format == "unknown":
            _issue(issues, "INVALID_FORMAT", path, "completed extraction needs a known format")
        if status == "ok" and (not has_tex or record.get("error_code") is not None):
            _issue(issues, "INCONSISTENT_STATUS", path, "ok needs a TeX file and no error")
        if status == "empty" and (has_tex or record.get("error_code") != NO_TEX_SOURCE):
            _issue(issues, "INCONSISTENT_STATUS", path, "empty needs no TeX and NO_TEX_SOURCE")
        if source_dir is not None:
            try:
                directory = safe_path(pathlib.Path(source_dir).parent, pathlib.Path(source_dir).name)
                if not directory.is_dir():
                    _issue(issues, "MISSING_SOURCE", path, "completed extraction needs a source directory")
                else:
                    for child in directory.rglob("*"):
                        name = child.relative_to(directory).as_posix()
                        if child.is_symlink():
                            _issue(issues, "UNSAFE_PATH", name, "symlinks are forbidden in source")
                        elif child.is_file() and name not in names:
                            _issue(issues, "UNDECLARED_SOURCE", name, "file is not listed in manifest")
                        elif not child.is_file() and not child.is_dir():
                            _issue(issues, "INVALID_SOURCE_TYPE", name, "special files are forbidden in source")
                if all_present and type(after) is int and total != after:
                    _issue(issues, "SIZE_MISMATCH", path + ".kept_source_bytes", "file sizes disagree with manifest")
            except (ValueError, OSError) as exc:
                _issue(issues, "UNSAFE_PATH", path, str(exc))
    if status == "failed" and not record.get("error_code"):
        _issue(issues, "MISSING_ERROR_CODE", path, "failed extraction needs an error code")
    return issues


def _date(issues, value, path):
    if value is None:
        return
    try:
        if not isinstance(value, str):
            raise ValueError("expected an ISO date string")
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _issue(issues, "INVALID_DATE", path, "expected ISO date/datetime or null")


def _strings(issues, value, path, *, nullable=False):
    if value is None and nullable:
        return
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        _issue(issues, "INVALID_TYPE", path, "expected list of strings")


def validate_metadata(record, *, arxiv_id=None):
    issues, path = [], "metadata"
    if not isinstance(record, dict):
        _issue(issues, "INVALID_TYPE", path, "expected object")
        return issues
    _required(issues, record, {"arxiv_id", "title", "authors", "submission_date",
                             "revised_dates", "venue", "latest_version", "versions",
                             "status", "error_code"}, path)
    identity = record.get("arxiv_id")
    if not valid_arxiv_id(identity) or (arxiv_id is not None and identity != arxiv_id):
        _issue(issues, "INVALID_ID", path + ".arxiv_id", "missing, invalid or mismatched ID")
    status = _status(issues, record, "status", path)
    for key in ("title", "submission_date", "venue", "error_code"):
        _nullable_string(issues, record, key, path)
    _strings(issues, record.get("authors"), path + ".authors", nullable=True)
    _strings(issues, record.get("revised_dates"), path + ".revised_dates", nullable=True)
    _date(issues, record.get("submission_date"), path + ".submission_date")
    revised = record.get("revised_dates")
    if isinstance(revised, list):
        for index, date in enumerate(revised):
            _date(issues, date, f"{path}.revised_dates[{index}]")
    versions = record.get("versions")
    if versions is not None and (not isinstance(versions, list)
                                 or any(not _positive(v) for v in versions)):
        _issue(issues, "INVALID_VERSION", path + ".versions", "expected positive integer list or null")
    elif isinstance(versions, list):
        if len(versions) != len(set(versions)):
            _issue(issues, "DUPLICATE_VERSION", path + ".versions", "version is repeated")
        if versions and (not _positive(record.get("latest_version"))
                         or record.get("latest_version") != max(versions)):
            _issue(issues, "VERSION_MISMATCH", path, "latest_version differs from version list")
        if status == "ok" and versions and any(v != i for i, v in enumerate(sorted(set(versions)), 1)):
            _issue(issues, "MISSING_VERSION", path + ".versions", "discovery list has gaps")
        if status == "ok" and isinstance(revised, list) and len(revised) != max(len(versions) - 1, 0):
            _issue(issues, "DATE_COUNT_MISMATCH", path + ".revised_dates", "revision dates do not match versions")
    if status == "ok":
        if not isinstance(versions, list) or not versions:
            _issue(issues, "MISSING_VERSION", path + ".versions", "successful discovery needs versions")
        for key in ("title", "authors", "submission_date", "revised_dates"):
            if record.get(key) is None or (key in {"title", "authors"} and not record.get(key)):
                _issue(issues, "MISSING_METADATA", path + "." + key,
                       "metadata is unknown or incomplete", "warning")
    return issues


def validate_references(record, *, arxiv_id=None):
    """Missing cited IDs/metadata are valid and retained; malformed types are errors."""
    issues, path = [], "references"
    if not isinstance(record, dict):
        _issue(issues, "INVALID_TYPE", path, "expected object")
        return issues
    _required(issues, record, {"arxiv_id", "status", "error_code", "source",
                             "fetched_at", "references"}, path)
    identity = record.get("arxiv_id")
    if not valid_arxiv_id(identity) or (arxiv_id is not None and identity != arxiv_id):
        _issue(issues, "INVALID_ID", path + ".arxiv_id", "missing, invalid or mismatched citing ID")
    status = _status(issues, record, "status", path)
    for key in ("source", "error_code", "fetched_at"):
        _nullable_string(issues, record, key, path)
    _date(issues, record.get("fetched_at"), path + ".fetched_at")
    refs = record.get("references")
    if not isinstance(refs, list):
        _issue(issues, "INVALID_TYPE", path + ".references", "expected list")
        return issues
    if status == "empty" and (refs or record.get("error_code") != "NO_REFERENCES_RETURNED"):
        _issue(issues, "INCONSISTENT_STATUS", path, "empty needs [] and NO_REFERENCES_RETURNED")
    if status == "ok" and record.get("error_code") is not None:
        _issue(issues, "INCONSISTENT_STATUS", path, "ok references must not have an error")
    if status == "failed" and not record.get("error_code"):
        _issue(issues, "MISSING_ERROR_CODE", path, "failed references need an error code")
    seen = set()
    for index, ref in enumerate(refs):
        field = f"references.references[{index}]"
        if not isinstance(ref, dict):
            _issue(issues, "INVALID_TYPE", field, "expected normalized reference object")
            continue
        _required(issues, ref, REFERENCE_FIELDS, field)
        if ref.get("arxiv_id") != identity:
            _issue(issues, "IDENTITY_MISMATCH", field + ".arxiv_id", "wrong citing ID")
        for key in REFERENCE_FIELDS - {"ref_authors"}:
            _nullable_string(issues, ref, key, field)
        _strings(issues, ref.get("ref_authors"), field + ".ref_authors")
        _date(issues, ref.get("ref_submission_date"), field + ".ref_submission_date")
        if ref.get("ref_arxiv_id") is not None and not valid_arxiv_id(ref["ref_arxiv_id"]):
            _issue(issues, "INVALID_ID", field + ".ref_arxiv_id", "invalid cited arXiv ID")
        if any(ref.get(key) is None for key in ("ref_title", "ref_submission_date", "ref_s2_id")):
            _issue(issues, "MISSING_METADATA", field, "optional cited metadata is unknown", "warning")
        # Even identical rows are reported, never silently dropped.
        signature = json.dumps(ref, sort_keys=True, ensure_ascii=False)
        if signature in seen:
            _issue(issues, "DUPLICATE_REFERENCE", field, "identical reference row", "warning")
        seen.add(signature)
    return issues


def _read_record(root, relative, issues):
    try:
        path = safe_path(root, relative)
        with path.open(encoding="utf-8") as handle:
            record = json.load(handle)
        if not isinstance(record, dict):
            _issue(issues, "INVALID_TYPE", path, "expected JSON object")
            return None
        return record
    except FileNotFoundError:
        _issue(issues, "NOT_COLLECTED", relative, "record has not been collected", "pending")
    except (ValueError, OSError) as exc:
        _issue(issues, "UNREADABLE_RECORD", relative, str(exc))
    return None


def _collection_state(issues, status, path, error_code=None):
    if not isinstance(status, str):
        return  # The schema validator already reports the invalid type.
    if status in {"pending", "skipped"}:
        _issue(issues, "NOT_COLLECTED", path, "stage has not completed", "pending")
    elif status == "running":
        _issue(issues, "IN_PROGRESS", path, "stage is in progress", "pending")
    elif status == "failed":
        _issue(issues, "COLLECTION_FAILED", path, f"collection failed: {error_code}")
    elif status == "empty":
        _issue(issues, "VALID_EMPTY", path, "confirmed empty result", "info")


def _report_status(issues):
    if any(issue["severity"] == "error" for issue in issues):
        return "failed"
    if any(issue["code"] == "IN_PROGRESS" for issue in issues):
        return "running"
    if any(issue["severity"] == "pending" for issue in issues):
        return "pending"
    return "ok"


def validate_paper(cfg, arxiv_id, *, expected_versions=None):
    """Inspect a paper independently; optional benchmark versions supplement discovery."""
    issues = []
    report = {"arxiv_id": arxiv_id, "status": "failed", "issues": issues}
    try:
        paper = paper_directory(cfg, arxiv_id)
    except ValueError as exc:
        _issue(issues, "INVALID_ID_OR_PATH", arxiv_id, str(exc))
        return report
    metadata = _read_record(paper, "metadata.json", issues)
    discovered = None
    if metadata is not None:
        issues.extend(validate_metadata(metadata, arxiv_id=arxiv_id))
        _collection_state(issues, metadata.get("status"), "metadata", metadata.get("error_code"))
        values = metadata.get("versions")
        if isinstance(values, list) and all(_positive(v) for v in values):
            discovered = set(values)
    expected = discovered
    if expected_versions is not None:
        if (not isinstance(expected_versions, list) or not expected_versions
                or any(not _positive(v) for v in expected_versions)
                or len(expected_versions) != len(set(expected_versions))):
            _issue(issues, "INVALID_EXPECTED_VERSIONS", "expected_versions", "invalid benchmark version list")
        else:
            expected = set(expected_versions)
            if discovered is not None and expected != discovered:
                _issue(issues, "DISCOVERY_MISMATCH", "metadata.versions", "discovery differs from expected versions")
    found = set()
    try:
        directory = safe_path(paper, "versions")
        if directory.exists():
            for child in sorted(directory.iterdir()):
                if not (child.name.startswith("v") and child.name[1:].isdigit()
                        and int(child.name[1:]) > 0):
                    _issue(issues, "INVALID_VERSION_DIRECTORY", child, "expected v<N> directory")
                    continue
                number = int(child.name[1:])
                if number in found:
                    _issue(issues, "DUPLICATE_VERSION", child, "multiple directories for one version")
                found.add(number)
                if child.name != f"v{number}":
                    _issue(issues, "INVALID_VERSION_DIRECTORY", child, "noncanonical version directory")
    except (ValueError, OSError) as exc:
        _issue(issues, "UNSAFE_PATH", "versions", str(exc))
    if expected is not None:
        for number in sorted(found - expected):
            _issue(issues, "UNEXPECTED_VERSION", f"versions/v{number}", "version absent from discovery/benchmark")
    for number in sorted(found | (expected or set())):
        relative = f"versions/v{number}/manifest.json"
        manifest = _read_record(paper, relative, issues)
        if manifest is None:
            continue
        source_dir = paper / "versions" / f"v{number}" / "source"
        issues.extend(dict(issue, path=f"versions/v{number}/{issue['path']}")
                      for issue in validate_manifest(manifest, arxiv_id=arxiv_id,
                                                     version=number, source_dir=source_dir))
        _collection_state(issues, manifest.get("download_status"), relative + ".download_status",
                          manifest.get("error_code"))
        _collection_state(issues, manifest.get("extract_status"), relative + ".extract_status",
                          manifest.get("error_code"))
        if manifest.get("archive_path") is not None:
            try:
                safe_path(pathlib.Path(cfg["output_root"]) / cfg["run_id"], manifest["archive_path"])
            except ValueError as exc:
                _issue(issues, "UNSAFE_PATH", relative + ".archive_path", str(exc))
    refs = _read_record(paper, "references.json", issues)
    if refs is not None:
        issues.extend(validate_references(refs, arxiv_id=arxiv_id))
        _collection_state(issues, refs.get("status"), "references", refs.get("error_code"))
    report["status"] = _report_status(issues)
    return report


def validate_run(cfg, arxiv_ids=None):
    """Validate all input IDs, keeping duplicate/missing IDs visible in the report."""
    issues, reports = [], []
    run = pathlib.Path(cfg["output_root"]) / cfg["run_id"]
    if arxiv_ids is None:
        record = _read_record(run, "run.json", issues)
        arxiv_ids = record.get("input_ids") if record else None
        if not isinstance(arxiv_ids, list):
            _issue(issues, "INVALID_INPUT_IDS", "run.input_ids", "supply input IDs or a run.json with input_ids")
            arxiv_ids = []
    seen = set()
    for index, arxiv_id in enumerate(arxiv_ids):
        if not isinstance(arxiv_id, str):
            _issue(issues, "INVALID_ID", f"input_ids[{index}]", "ID must be a string")
            continue
        if arxiv_id in seen:
            _issue(issues, "DUPLICATE_PAPER", f"input_ids[{index}]", f"duplicate ID: {arxiv_id}")
            continue
        seen.add(arxiv_id)
        report = validate_paper(cfg, arxiv_id)
        reports.append(report)
        issues.extend(dict(issue, arxiv_id=arxiv_id) for issue in report["issues"])
    # Inspect unexpected folders too: a second directory must not hide a
    # duplicate record just because its name is absent from input_ids.
    try:
        directory = safe_path(run, "papers")
        if directory.is_dir():
            canonical = {paper_key(value) for value in seen if valid_arxiv_id(value)}
            stored_ids = set()
            for child in sorted(directory.iterdir()):
                if child.name in canonical:
                    continue  # Already inspected via validate_paper.
                metadata = _read_record(directory, f"{child.name}/metadata.json", issues)
                if metadata is None:
                    continue
                identity = metadata.get("arxiv_id")
                if not valid_arxiv_id(identity):
                    _issue(issues, "INVALID_ID", child, "invalid stored paper ID")
                    continue
                if identity in seen or identity in stored_ids:
                    _issue(issues, "DUPLICATE_PAPER", child, f"duplicate stored ID: {identity}")
                stored_ids.add(identity)
                if child.name != paper_key(identity):
                    _issue(issues, "IDENTITY_MISMATCH", child, "folder name differs from paper_key")
                if identity not in seen:
                    _issue(issues, "UNEXPECTED_PAPER", child, "paper is absent from run input IDs", "warning")
    except (ValueError, OSError) as exc:
        _issue(issues, "UNSAFE_PATH", "papers", str(exc))
    return {"status": _report_status(issues), "papers": reports, "issues": issues}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=pathlib.Path)
    parser.add_argument("arxiv_ids", nargs="*", help="defaults to run.json input_ids")
    args = parser.parse_args()
    run = args.run_dir.absolute()
    report = validate_run({"output_root": run.parent, "run_id": run.name}, args.arxiv_ids or None)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "ok" else 1 if report["status"] == "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
