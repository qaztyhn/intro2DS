"""Read/write the existing per-run JSON corpus, with conservative resume guards.

No database, duplicate corpus or global append-only index is introduced. The
downloader's manifest and discovery's metadata remain the canonical records.
"""

import json

from ..utils.common import EMPTY, OK, Stopwatch, write_json_atomic
from ..schema import (MANIFEST_FIELDS, REFERENCE_FIELDS, STATUSES, paper_directory,
                     require_version, safe_path)


def _read(path):
    """Missing means not collected; malformed data must be reported, not replaced."""
    try:
        with path.open(encoding="utf-8") as handle:
            record = json.load(handle)
    except FileNotFoundError:
        return None
    if not isinstance(record, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return record


def load_metadata(cfg, arxiv_id):
    return _read(safe_path(paper_directory(cfg, arxiv_id), "metadata.json"))


def load_references(cfg, arxiv_id):
    return _read(safe_path(paper_directory(cfg, arxiv_id), "references.json"))


def load_manifest(cfg, arxiv_id, version):
    require_version(version)
    return _read(safe_path(paper_directory(cfg, arxiv_id),
                           f"versions/v{version}/manifest.json"))


def _identity(record, arxiv_id, status_key):
    if not isinstance(record, dict) or record.get("arxiv_id") != arxiv_id:
        raise ValueError("record arxiv_id does not match its paper directory")
    status = record.get(status_key)
    if not isinstance(status, str) or status not in STATUSES:
        raise ValueError(f"invalid {status_key}")


def _preserves(old, new):
    """Preserve known values and list multiplicities while allowing enrichment."""
    if old is None:
        return True
    if isinstance(old, dict):
        return isinstance(new, dict) and all(
            _preserves(value, new.get(key)) for key, value in old.items()
            if key not in {"status", "error_code", "fetched_at", "latest_version"})
    if isinstance(old, list):
        if not isinstance(new, list) or len(old) > len(new):
            return False
        # Match each old occurrence to a distinct new occurrence. Reassign
        # earlier matches when sparse metadata fits several candidates, so
        # enrichment remains independent of either list's ordering.
        matched = {}  # new index -> old index
        for index in range(len(old)):
            pending, parents, visited = [index], {index: None}, set()
            found = False
            while pending and not found:
                old_index = pending.pop()
                for new_index, candidate in enumerate(new):
                    if new_index in visited or not _preserves(old[old_index], candidate):
                        continue
                    visited.add(new_index)
                    if new_index not in matched:
                        # Follow the alternating path back to this occurrence.
                        while True:
                            matched[new_index] = old_index
                            previous = parents[old_index]
                            if previous is None:
                                break
                            old_index, new_index = previous
                        found = True
                        break
                    owner = matched[new_index]
                    if owner not in parents:
                        parents[owner] = (old_index, new_index)
                        pending.append(owner)
            if not found:
                return False
        return True
    return type(old) is type(new) and old == new


def _save(path, record, status_key, *, freeze_complete=False):
    previous = _read(path)
    if previous == record:
        return path
    if previous and previous.get(status_key) in {OK, EMPTY}:
        if (record[status_key] not in {OK, EMPTY} or freeze_complete
                or not _preserves(previous, record)):
            return path
    write_json_atomic(path, record)
    return path


def save_metadata(cfg, arxiv_id, record):
    """Save the discovery record as-is, including nulls and additional fields."""
    _identity(record, arxiv_id, "status")
    for key in ("title", "submission_date", "venue"):
        if record.get(key) is not None and not isinstance(record[key], str):
            raise ValueError(f"invalid metadata {key}")
    for key in ("authors", "revised_dates"):
        value = record.get(key)
        if value is not None and (not isinstance(value, list)
                                  or any(not isinstance(v, str) for v in value)):
            raise ValueError(f"invalid metadata {key}")
    versions = record.get("versions")
    if versions is not None:
        if not isinstance(versions, list):
            raise ValueError("versions must be a list or null")
        for version in versions:
            require_version(version)
        if len(set(versions)) != len(versions):
            raise ValueError("duplicate versions")
    if record["status"] == OK and not versions:
        raise ValueError("successful discovery must include versions")
    if record["status"] == OK and record.get("latest_version") != max(versions):
        raise ValueError("latest_version must match the discovered version list")
    from .validation import validate_metadata
    issues = validate_metadata(record, arxiv_id=arxiv_id)
    if any(issue["severity"] == "error" for issue in issues):
        raise ValueError(f"invalid metadata: {issues}")
    path = safe_path(paper_directory(cfg, arxiv_id), "metadata.json")
    return _save(path, record, "status")


def save_references(cfg, arxiv_id, record):
    """Persist ALL normalized references, including records with missing IDs."""
    _identity(record, arxiv_id, "status")
    references = record.get("references")
    if not isinstance(references, list):
        raise ValueError("references must be a list")
    if record["status"] == EMPTY and references:
        raise ValueError("empty references must have an empty list")
    for ref in references:
        if not isinstance(ref, dict) or not REFERENCE_FIELDS <= ref.keys():
            raise ValueError("missing normalized reference fields")
        if ref["arxiv_id"] != arxiv_id:
            raise ValueError("reference citing ID mismatch")
        for key in REFERENCE_FIELDS - {"ref_authors"}:
            if ref[key] is not None and not isinstance(ref[key], str):
                raise ValueError(f"invalid reference {key}")
        if not isinstance(ref["ref_authors"], list) or any(
                not isinstance(author, str) for author in ref["ref_authors"]):
            raise ValueError("invalid ref_authors")
    from .validation import validate_references
    issues = validate_references(record, arxiv_id=arxiv_id)
    if any(issue["severity"] == "error" for issue in issues):
        raise ValueError(f"invalid references: {issues}")
    path = safe_path(paper_directory(cfg, arxiv_id), "references.json")
    return _save(path, record, "status")


def save_manifest(cfg, arxiv_id, version, record):
    require_version(version)
    _identity(record, arxiv_id, "extract_status")
    if (not MANIFEST_FIELDS <= record.keys() or record["version"] != version
            or type(record["version"]) is not int
            or record["versioned_id"] != f"{arxiv_id}v{version}"):
        raise ValueError("invalid manifest fields or version identity")
    # Record-level checks do not require completed files (pending is valid).
    from .validation import validate_manifest
    issues = validate_manifest(record, arxiv_id=arxiv_id, version=version)
    if any(issue["severity"] == "error" for issue in issues):
        raise ValueError(f"invalid manifest: {issues}")
    path = safe_path(paper_directory(cfg, arxiv_id), f"versions/v{version}/manifest.json")
    return _save(path, record, "extract_status", freeze_complete=True)


def load_paper(cfg, arxiv_id):
    """M2 entry point: metadata, references and manifests keyed by integer version."""
    paper = paper_directory(cfg, arxiv_id)
    metadata = load_metadata(cfg, arxiv_id)
    versions = {}
    expected = metadata.get("versions") if metadata else None
    if isinstance(expected, list):
        for version in expected:
            versions[require_version(version)] = load_manifest(cfg, arxiv_id, version)
    # Also retain versions not in discovery, so validation can flag the mismatch.
    directory = safe_path(paper, "versions")
    if directory.exists():
        for child in sorted(directory.iterdir()):
            if child.name.startswith("v") and child.name[1:].isdigit():
                version = int(child.name[1:])
                if version > 0:
                    versions[version] = load_manifest(cfg, arxiv_id, version)
    return {"metadata": metadata, "references": load_references(cfg, arxiv_id),
            "versions": versions}


def store_paper(cfg, arxiv_id, logger):
    """Driver hook: validate the canonical files; do not copy or rewrite them."""
    from .validation import validate_paper
    from ..utils.errors import CrawlError

    watch = Stopwatch()
    report = validate_paper(cfg, arxiv_id)
    path = safe_path(paper_directory(cfg, arxiv_id), "validation.json")
    write_json_atomic(path, report)
    logger.log("storage", report["status"], arxiv_id=arxiv_id,
               error_code="SCHEMA_ERROR" if report["status"] == "failed" else None,
               message=f"validation issues={len(report['issues'])}", elapsed_seconds=watch.elapsed)
    if report["status"] != "ok":
        raise CrawlError(f"corpus is invalid or incomplete; see {path}", "SCHEMA_ERROR")
    return report
