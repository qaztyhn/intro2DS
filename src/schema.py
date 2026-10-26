"""Small shared M1 contract; disk layout and manifest fields stay unchanged."""

import pathlib
import re

from .common import EMPTY, FAILED, OK, PENDING, RUNNING, SKIPPED, paper_key

STATUSES = {PENDING, RUNNING, OK, EMPTY, FAILED, SKIPPED}
SOURCE_FORMATS = {"tar", "tar.gz", "gz", "tex", "pdf", "unknown"}
MANIFEST_FIELDS = {
    "arxiv_id", "version", "versioned_id", "download_status", "extract_status",
    "parse_status", "source_format", "source_files", "archive_path",
    "archive_bytes", "unpacked_total_bytes", "kept_source_bytes", "error_code",
}
REFERENCE_FIELDS = {
    "arxiv_id", "ref_arxiv_id", "ref_title", "ref_authors",
    "ref_submission_date", "ref_s2_id",
}
NO_TEX_SOURCE = "NO_TEX_SOURCE"
# Syntax check only; existence must be established by discovery.
_ID = re.compile(r"(?:\d{2}(?:0[1-9]|1[0-2])\.\d{4,5}|"
                 r"[A-Za-z][A-Za-z0-9.-]*/\d{2}(?:0[1-9]|1[0-2])\d{3})\Z", re.ASCII)


def valid_arxiv_id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def require_arxiv_id(value):
    if not valid_arxiv_id(value):
        raise ValueError(f"invalid base arXiv ID: {value!r}")
    return value


def require_version(value):
    if type(value) is not int or value < 1:
        raise ValueError("version must be a positive integer")
    return value


def relative_path(value):
    """Accept portable POSIX paths, rejecting traversal and Windows paths too."""
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"invalid relative path: {value!r}")
    if any(ord(c) < 32 for c in value) or pathlib.PureWindowsPath(value).drive:
        raise ValueError(f"unsafe relative path: {value!r}")
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"unsafe relative path: {value!r}")
    return path.as_posix()


def no_symlinks(path):
    path = pathlib.Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError(f"symlink in data path: {path}")
    return path


def safe_path(root, value):
    root = no_symlinks(root)
    target = no_symlinks(root / relative_path(value))
    if not target.is_relative_to(root):
        raise ValueError(f"path escapes data root: {value!r}")
    return target


def paper_directory(cfg, arxiv_id):
    require_arxiv_id(arxiv_id)
    run = pathlib.Path(cfg["output_root"]) / cfg["run_id"]
    return safe_path(run, f"papers/{paper_key(arxiv_id)}")
