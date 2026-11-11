"""Offline, bounded source extraction and the driver's per-paper hook.

Only regular .tex/.bib files enter source/. Bytes and relative paths are kept.
No tarfile.extract/all calls: links, special entries and unsafe names fail closed.
"""

import contextlib
import gzip
import os
import pathlib
import re
import tarfile
import tempfile
from dataclasses import dataclass

from ..utils.common import (EMPTY, FAILED, OK, SKIPPED, Stopwatch, read_json, write_json_atomic)
from ..schema import (NO_TEX_SOURCE, no_symlinks, paper_directory, relative_path, require_version, safe_path)
from ..utils.errors import (CORRUPT_SOURCE, INVALID_RESPONSE, NOT_FOUND, UNSUPPORTED_FORMAT, CrawlError)
from ..metrics import measure_stage

@dataclass(frozen=True)
class ExtractionLimits:
    max_archive_bytes: int = 256 * 1024 * 1024
    max_unpacked_bytes: int = 512 * 1024 * 1024
    max_file_bytes: int = 64 * 1024 * 1024
    max_members: int = 10000

    def __post_init__(self):
        if any(type(v) is not int or v < 1 for v in vars(self).values()):
            raise ValueError("extraction limits must be positive integers")


_TEX = re.compile(rb"\\(?:documentclass|documentstyle|begin|input|include|"
                  rb"def|newcommand|section|chapter|bye|end)\b")


def _copy(source, destination, limit):
    size = 0
    while chunk := source.read(min(1024 * 1024, limit - size + 1)):
        size += len(chunk)
        if size > limit:
            raise CrawlError("source exceeds extraction byte limit", CORRUPT_SOURCE)
        if destination is not None:
            destination.write(chunk)
    return size


def _is_tex(head):
    # A suffix alone cannot turn an HTML error page or arbitrary bytes into TeX.
    if b"\0" in head or re.search(rb"<\s*(?:!doctype\s+html|html)\b", head, re.I):
        return False
    # Ignore comment-only commands when recognizing an otherwise unknown file.
    text = b"\n".join(line.split(b"%", 1)[0] for line in head.splitlines())
    return _TEX.search(text) is not None


class _TarReader:
    """Bound the bytes tarfile reads for the headers of one member."""

    def __init__(self, handle):
        self.handle = handle
        self.header_bytes = 0
        self.in_data = False

    def start_header(self):
        self.header_bytes = 0
        self.in_data = False

    def read(self, size=-1):
        if size < 0 or size > 1024 * 1024:
            raise CrawlError("tar header/read exceeds size limit", CORRUPT_SOURCE)
        if not self.in_data:
            self.header_bytes += size
            if self.header_bytes > 1024 * 1024:
                raise CrawlError("tar header/read exceeds size limit", CORRUPT_SOURCE)
        return self.handle.read(size)

    def seek(self, *args):
        return self.handle.seek(*args)

    def tell(self):
        return self.handle.tell()


def _is_tar(payload):
    try:
        with payload.open("rb") as handle, tarfile.open(fileobj=_TarReader(handle), mode="r:"):
            return True
    except tarfile.TarError:
        return False

def _members(archive, reader):
    """Yield members one by one, giving each member's headers a fresh byte budget."""
    while True:
        reader.start_header()
        member = archive.next()
        if member is None:
            return
        reader.in_data = True  # from here until the next start_header, only data is read
        yield member
        
def _extract_tar(payload, stage, limits):
    total, kept, files, seen = 0, 0, [], set()
    with payload.open("rb") as handle, tarfile.open(fileobj=(reader := _TarReader(handle)), mode="r:") as archive:
        for count, member in enumerate(_members(archive, reader), 1):
            if count > limits.max_members:
                raise CrawlError("too many archive members", CORRUPT_SOURCE)
            # A conventional top-level './' directory contains no data.
            if member.isdir() and member.name in {".", "./"}:
                continue
            name = relative_path(member.name)
            if name in seen:
                raise CrawlError(f"duplicate archive path: {name}", CORRUPT_SOURCE)
            seen.add(name)
            if not (member.isfile() or member.isdir()) or member.sparse is not None:
                raise CrawlError(f"unsafe archive entry: {name}", CORRUPT_SOURCE)
            if member.isdir():
                continue
            if member.size < 0 or member.size > limits.max_file_bytes:
                raise CrawlError(f"file exceeds size limit: {name}", CORRUPT_SOURCE)
            total += member.size
            if total > limits.max_unpacked_bytes:
                raise CrawlError("archive exceeds unpacked size limit", CORRUPT_SOURCE)
            keep = pathlib.PurePosixPath(name).suffix.lower() in {".tex", ".bib"}
            with archive.extractfile(member) as source:
                if keep:
                    target = safe_path(stage, name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("xb") as dest:
                        actual = _copy(source, dest, limits.max_file_bytes)
                    files.append(name)
                    kept += actual
                else:
                    actual = _copy(source, None, limits.max_file_bytes)
            if actual != member.size:
                raise CrawlError(f"truncated archive entry: {name}", CORRUPT_SOURCE)
    return sorted(files), total, kept


def _same_output(existing, staged, files):
    existing_files = set()
    for path in existing.rglob("*"):
        if path.is_symlink():
            return False
        if path.is_file():
            existing_files.add(path.relative_to(existing).as_posix())
        elif not path.is_dir():
            return False
    if existing_files != set(files):
        return False
    for name in files:
        left, right = safe_path(existing, name), safe_path(staged, name)
        if left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as a, right.open("rb") as b:
            while chunk := a.read(1024 * 1024):
                if chunk != b.read(len(chunk)):
                    return False
    return True


def extract_source(archive_path, output_dir, *, limits=None, reuse_existing=False):
    """Return extraction fields plus output_path/message, without writing a manifest.

    Existing nonempty output is never replaced. reuse_existing permits recovery
    only when re-extraction proves every existing file byte-identical. Unknown sizes remain None on
    failure; no partial files are published. Single-file TeX is named main.tex.
    """
    limits = limits or ExtractionLimits()
    result = dict(extract_status=FAILED, source_format=None, source_files=[],
                  archive_bytes=None, unpacked_total_bytes=None,
                  kept_source_bytes=None, error_code=None,
                  output_path=str(output_dir), message=None)
    try:
        archive_path = no_symlinks(archive_path)
        output_dir = no_symlinks(output_dir)
        size = archive_path.stat().st_size
        result["archive_bytes"] = size
        if size > limits.max_archive_bytes:
            raise CrawlError("archive exceeds download size limit", CORRUPT_SOURCE)
        if output_dir.exists() and (not output_dir.is_dir()
                                    or (any(output_dir.iterdir()) and not reuse_existing)):
            raise CrawlError("existing source output must be validated before reuse",
                             INVALID_RESPONSE)
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".extract-", dir=output_dir.parent) as tmp:
            tmp = pathlib.Path(tmp)
            stage = tmp / "source"
            stage.mkdir()
            with archive_path.open("rb") as source:
                head = source.read(65536)
            compressed = head.startswith(b"\x1f\x8b")
            payload = archive_path
            if compressed:
                result["source_format"] = "gz"
                payload = tmp / "payload"
                # Includes tar headers, padding and extended headers, not just files.
                stream_limit = limits.max_unpacked_bytes + limits.max_members * 1024 + 1024 * 1024
                with gzip.open(archive_path, "rb") as source, payload.open("wb") as dest:
                    _copy(source, dest, stream_limit)
                with payload.open("rb") as source:
                    head = source.read(65536)
            if _is_tar(payload):
                result["source_format"] = "tar.gz" if compressed else "tar"
                files, total, kept = _extract_tar(payload, stage, limits)
            elif head[257:262] == b"ustar":
                raise CrawlError("corrupt tar header", CORRUPT_SOURCE)
            elif head.startswith(b"%PDF-"):
                result["source_format"] = "gz" if compressed else "pdf"
                files, total, kept = [], payload.stat().st_size, 0
                if total > min(limits.max_file_bytes, limits.max_unpacked_bytes):
                    raise CrawlError("PDF source exceeds size limit", CORRUPT_SOURCE)
            elif _is_tex(head):
                result["source_format"] = "gz" if compressed else "tex"
                with payload.open("rb") as source, (stage / "main.tex").open("wb") as dest:
                    total = _copy(source, dest, min(limits.max_file_bytes, limits.max_unpacked_bytes))
                files, kept = ["main.tex"], total
            else:
                result["source_format"] = "gz" if compressed else "unknown"
                raise CrawlError("source format is unsupported", UNSUPPORTED_FORMAT)
            has_tex = any(pathlib.PurePosixPath(f).suffix.lower() == ".tex" for f in files)
            if output_dir.exists() and any(output_dir.iterdir()):
                if not _same_output(output_dir, stage, files):
                    raise CrawlError("existing source differs from re-extracted source; inspect before retrying",
                                     INVALID_RESPONSE)
            else:
                os.replace(stage, output_dir)
            result.update(extract_status=OK if has_tex else EMPTY, source_files=files,
                          unpacked_total_bytes=total, kept_source_bytes=kept,
                          error_code=None if has_tex else NO_TEX_SOURCE)
    except (OSError, EOFError, tarfile.TarError, ValueError, RecursionError, CrawlError) as exc:
        result["error_code"] = (exc.error_code if isinstance(exc, CrawlError)
                                else NOT_FOUND if isinstance(exc, FileNotFoundError)
                                else CORRUPT_SOURCE)
        result["message"] = str(exc)[:300]
    return result


def _completed_source(manifest, source_dir):
    """Check cached output, including byte totals; never downgrade a terminal record."""
    try:
        files = manifest["source_files"]
        if not isinstance(files, list) or len(files) != len(set(files)):
            return False
        paths = [safe_path(source_dir, f) for f in files]
        if any(not p.is_file() or p.suffix.lower() not in {".tex", ".bib"} for p in paths):
            return False
        has_tex = any(p.suffix.lower() == ".tex" for p in paths)
        if (manifest["extract_status"] == OK) != has_tex:
            return False
        if manifest["extract_status"] == EMPTY and manifest.get("error_code") != NO_TEX_SOURCE:
            return False
        return (source_dir.is_dir() and manifest.get("kept_source_bytes") ==
                sum(p.stat().st_size for p in paths))
    except (KeyError, TypeError, ValueError, OSError):
        return False


def extract_version(cfg, arxiv_id, version):
    """Update the downloader's manifest, retaining its fields and parse status."""
    require_version(version)
    paper = paper_directory(cfg, arxiv_id)
    directory = safe_path(paper, f"versions/v{version}")
    path = safe_path(directory, "manifest.json")
    manifest = read_json(path)
    if not isinstance(manifest, dict):
        raise CrawlError("download manifest missing or invalid", INVALID_RESPONSE)
    if (manifest.get("arxiv_id") != arxiv_id or manifest.get("version") != version
            or manifest.get("versioned_id") != f"{arxiv_id}v{version}"):
        raise CrawlError("download manifest identity mismatch", INVALID_RESPONSE)
    from .validation import validate_manifest
    if any(issue["severity"] == "error" for issue in validate_manifest(manifest)):
        raise CrawlError("download manifest violates the shared schema", INVALID_RESPONSE)
    source = safe_path(directory, "source")
    if manifest.get("extract_status") in {OK, EMPTY}:
        if not _completed_source(manifest, source):
            raise CrawlError("cached source is inconsistent; inspect before retrying", CORRUPT_SOURCE)
        return manifest
    if manifest.get("download_status") != OK:
        return manifest
    try:
        archive = safe_path(pathlib.Path(cfg["output_root"]) / cfg["run_id"],
                            manifest.get("archive_path"))
    except ValueError as exc:
        raise CrawlError(str(exc), CORRUPT_SOURCE) from exc
    limits = ExtractionLimits(**cfg.get("extraction_limits", {}))
    result = extract_source(archive, source, limits=limits, reuse_existing=True)
    for key in ("extract_status", "source_format", "source_files", "archive_bytes",
                "unpacked_total_bytes", "kept_source_bytes", "error_code"):
        if key not in {"source_format", "archive_bytes"} or result[key] is not None:
            manifest[key] = result[key]
    write_json_atomic(path, manifest)
    return manifest


def extract_paper(cfg, arxiv_id, logger, metrics_logger=None):
    """Driver hook: process all discovered versions, then propagate any failures."""
    paper = paper_directory(cfg, arxiv_id)
    metadata = read_json(safe_path(paper, "metadata.json"))
    if not metadata or metadata.get("status") != OK or not metadata.get("versions"):
        logger.log("extract", SKIPPED, arxiv_id=arxiv_id, message="no discovered versions")
        return {}
    results, errors = {}, []
    for version in metadata["versions"]:
        watch = Stopwatch()
        try:
            require_version(version)
            previous = read_json(safe_path(paper, f"versions/v{version}/manifest.json"))
            cached = (isinstance(previous, dict)
                      and previous.get("extract_status") in (OK, EMPTY))
            timing = (measure_stage(metrics_logger, "extract", arxiv_id=arxiv_id, version=version)
                    if metrics_logger is not None else contextlib.nullcontext())
            with timing:
                result = extract_version(cfg, arxiv_id, version)
            results[version] = result
            status = result["extract_status"]
            if result["download_status"] != OK:
                status = SKIPPED
            if status == FAILED:
                errors.append(result.get("error_code"))
            if cached:
                status = SKIPPED
            logger.log("extract", status, arxiv_id=arxiv_id, version=version,
                       error_code=result.get("error_code"), elapsed_seconds=watch.elapsed,
                       message="using completed source" if cached else None)
        except (CrawlError, ValueError, OSError) as exc:
            code = exc.error_code if isinstance(exc, CrawlError) else INVALID_RESPONSE
            errors.append(code)
            logger.log("extract", FAILED, arxiv_id=arxiv_id, version=version,
                       error_code=code, message=str(exc)[:300], elapsed_seconds=watch.elapsed)
    if errors:
        raise CrawlError("one or more versions failed extraction", errors[0])
    return results
