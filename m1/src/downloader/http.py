"""Baseline source download: HTTP GET https://export.arxiv.org/src/<id>v<N>, one request per version.

Reads   <run_dir>/papers/<paper_key>/metadata.json   (written by discovery)
Writes  <run_dir>/tmp/<paper_key>/v<N>/source.archive   (temporary; extract may delete it)
        <run_dir>/papers/<paper_key>/versions/v<N>/manifest.json

The manifest follows the per-version record of Intro2DS_Project.md (section 1.B).
This module only downloads and detects the format; extract_status / parse_status
stay "pending" for the extraction module.
"""

import contextlib
import gzip
import pathlib
from typing import Any, Dict, Optional

from .. import metrics
from ..utils.common import (
    FAILED,
    OK,
    PENDING,
    SKIPPED,
    EventLogger,
    Stopwatch,
    load_ids,
    paper_key,
    read_json,
    write_json_atomic,
)
from ..config import http_options, load_config
from ..utils.errors import CORRUPT_SOURCE, INVALID_RESPONSE, CrawlError
from ..utils.http import Throttle, download_file

STAGE = "download"
SRC_URL = "https://export.arxiv.org/src/{versioned_id}"


def detect_format(path: pathlib.Path) -> str:
    """Return one of: tar.gz, gz, tar, pdf, unknown. Looks at magic bytes only."""
    with path.open("rb") as handle:
        head = handle.read(512)
    if head[:2] == b"\x1f\x8b":
        try:
            with gzip.open(path, "rb") as gz:
                inner = gz.read(512)
        except (OSError, EOFError) as exc:
            raise CrawlError(f"unreadable gzip: {exc}", CORRUPT_SOURCE) from exc
        return "tar.gz" if inner[257:262] == b"ustar" else "gz"
    if head[257:262] == b"ustar":
        return "tar"
    if head[:4] == b"%PDF":
        return "pdf"
    return "unknown"


def make_manifest(arxiv_id: str, version: int) -> Dict[str, Any]:
    return {
        "arxiv_id": arxiv_id,
        "version": version,
        "versioned_id": f"{arxiv_id}v{version}",
        "download_status": FAILED,
        "extract_status": PENDING,
        "parse_status": PENDING,
        "source_format": None,
        "source_files": [],
        "archive_path": None,  # relative to the run directory
        "archive_bytes": None,
        "unpacked_total_bytes": None,
        "kept_source_bytes": None,
        "error_code": None,
    }


def _download_archive(opts: Dict[str, Any], url: str, target: pathlib.Path) -> int:
    """Download ``url`` into ``target`` through the shared HTTP layer; return its size in bytes.

    Retry, backoff and throttling are done by utils/http.py (a cut-short stream is retried there).
    """
    download_file(url, target, **opts)
    size = target.stat().st_size
    if size == 0:
        target.unlink(missing_ok=True)
        raise CrawlError("empty response body", INVALID_RESPONSE)
    return size

def download_version(
    cfg: Dict[str, Any], opts: Dict[str, Any], arxiv_id: str, version: int
) -> Dict[str, Any]:
    """Download one version and return its manifest (also written to disk)."""
    run_dir = cfg["output_root"] / cfg["run_id"]
    key = paper_key(arxiv_id)
    manifest = make_manifest(arxiv_id, version)
    manifest_path = run_dir / "papers" / key / "versions" / f"v{version}" / "manifest.json"
    archive = run_dir / "tmp" / key / f"v{version}" / "source.archive"

    try:
        size = _download_archive(opts, SRC_URL.format(versioned_id=manifest["versioned_id"]), archive)
        manifest["source_format"] = detect_format(archive)
        manifest["archive_bytes"] = size
        manifest["archive_path"] = archive.relative_to(run_dir).as_posix()
        manifest["download_status"] = OK
    except CrawlError as error:
        manifest["error_code"] = error.error_code
        manifest["download_status"] = FAILED
        write_json_atomic(manifest_path, manifest)
        raise
    write_json_atomic(manifest_path, manifest)
    return manifest


def download_paper(
    cfg: Dict[str, Any], arxiv_id: str, opts: Dict[str, Any], logger: EventLogger, metrics_logger: Optional[metrics.EventLogger] = None
) -> Dict[str, int]:
    """Download every version of one paper. Returns counts per status (one per version)."""
    run_dir = cfg["output_root"] / cfg["run_id"]
    key = paper_key(arxiv_id)
    counts = {OK: 0, FAILED: 0, SKIPPED: 0}

    metadata = read_json(run_dir / "papers" / key / "metadata.json")
    if not metadata or metadata.get("status") != OK or not metadata.get("versions"):
        counts[SKIPPED] += 1
        logger.log(STAGE, SKIPPED, arxiv_id=arxiv_id, message="discovery not ok; no version list")
        return counts

    for version in metadata["versions"]:
        manifest_path = run_dir / "papers" / key / "versions" / f"v{version}" / "manifest.json"
        existing = read_json(manifest_path)
        if existing and existing.get("download_status") == OK:
            counts[SKIPPED] += 1
            logger.log(STAGE, SKIPPED, arxiv_id=arxiv_id, version=version, message="already downloaded")
            continue

        watch = Stopwatch()
        stage_cm = contextlib.nullcontext()
        version_opts = opts
        if metrics_logger is not None: 
            stage_cm = metrics.measure_stage(metrics_logger, STAGE, arxiv_id=arxiv_id, version=version)
            # one hook per logical request (= one version), created before its retry loop
            version_opts = {
                **opts, 
                "on_attempt": metrics.make_request_hook(
                    metrics_logger, STAGE, arxiv_id=arxiv_id, version=version
                ),
            }
        try:
            with stage_cm:
                manifest = download_version(cfg, version_opts, arxiv_id, version)
        except Exception as exc:  # noqa: BLE001 - one version must never stop the run
            error = exc if isinstance(exc, CrawlError) else CrawlError(repr(exc), INVALID_RESPONSE)
            if not isinstance(exc, CrawlError):  # unexpected bug: still leave a manifest behind
                failed = make_manifest(arxiv_id, version)
                failed["error_code"] = error.error_code
                write_json_atomic(manifest_path, failed)
            counts[FAILED] += 1
            logger.log(
                STAGE, FAILED, arxiv_id=arxiv_id, version=version, error_code=error.error_code,
                http_status=error.status_code,
                message=str(error)[:300], elapsed_seconds=watch.elapsed,
            )
        else:
            counts[OK] += 1
            logger.log(
                STAGE, OK, arxiv_id=arxiv_id, version=version, elapsed_seconds=watch.elapsed,
                message=f"{manifest['source_format']}, {manifest['archive_bytes']} bytes",
            )
    return counts


def run_download(cfg: Dict[str, Any], logger: Optional[EventLogger] = None, metrics_logger: Optional[metrics.EventLogger] = None) -> Dict[str, int]:
    """Download every version of every paper whose discovery status is ok."""
    run_dir = cfg["output_root"] / cfg["run_id"]
    if logger is None:
        logger = EventLogger(run_dir / "events.jsonl", cfg["run_id"])
    throttle = Throttle(cfg["arxiv_delay_seconds"])
    opts = http_options(cfg, throttle)

    totals = {OK: 0, FAILED: 0, SKIPPED: 0}
    for arxiv_id in load_ids(cfg["ids_file"]):
        for status, n in download_paper(cfg, arxiv_id, opts, logger, metrics_logger).items():
            totals[status] += n
    return totals


def main() -> None:
    cfg = load_config()
    counts = run_download(cfg)
    print(f"run_id={cfg['run_id']} download (per version): {counts}")


if __name__ == "__main__":
    main()
