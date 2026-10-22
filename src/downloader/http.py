"""Baseline source download: HTTP GET https://export.arxiv.org/src/<id>v<N>, one request per version.

Reads   <run_dir>/papers/<paper_key>/metadata.json   (written by discovery)
Writes  <run_dir>/tmp/<paper_key>/v<N>/source.archive   (temporary; extract may delete it)
        <run_dir>/papers/<paper_key>/versions/v<N>/manifest.json

The manifest follows the per-version record of Intro2DS_Project.md (section 1.B).
This module only downloads and detects the format; extract_status / parse_status
stay "pending" for the extraction module.
"""

import gzip
import pathlib
from typing import Any, Dict, Optional

import requests

from ..common import (
    CORRUPT_SOURCE,
    FAILED,
    INVALID_RESPONSE,
    NETWORK_ERROR,
    OK,
    PENDING,
    SKIPPED,
    EventLog,
    PipelineError,
    Stopwatch,
    Throttle,
    load_ids,
    paper_key,
    read_json,
    write_json_atomic,
)
from ..config import load_config
from ..request_utils import RequestClient

STAGE = "download"
SRC_URL = "https://export.arxiv.org/src/{versioned_id}"
CHUNK_BYTES = 64 * 1024
MAX_CORRUPT_RELOADS = 1  # "can re-download once before concluding" (CORRUPT_SOURCE)


def detect_format(path: pathlib.Path) -> str:
    """Return one of: tar.gz, gz, tar, pdf, unknown. Looks at magic bytes only."""
    with path.open("rb") as handle:
        head = handle.read(512)
    if head[:2] == b"\x1f\x8b":
        try:
            with gzip.open(path, "rb") as gz:
                inner = gz.read(512)
        except (OSError, EOFError) as exc:
            raise PipelineError(CORRUPT_SOURCE, f"unreadable gzip: {exc}") from exc
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


def _stream_to_file(client: RequestClient, url: str, target: pathlib.Path) -> int:
    """Download ``url`` to ``target``; return bytes written. Re-downloads once if the
    transfer is cut short or does not match Content-Length."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    last_error: Optional[PipelineError] = None

    for _ in range(MAX_CORRUPT_RELOADS + 1):
        response = client.get(url, stream=True)  # network/HTTP errors raised here
        try:
            written = 0
            with part.open("wb") as handle:
                for chunk in response.iter_content(CHUNK_BYTES):
                    handle.write(chunk)
                    written += len(chunk)
        except requests.RequestException as exc:
            last_error = PipelineError(NETWORK_ERROR, f"stream interrupted: {exc}")
            continue
        finally:
            response.close()

        expected = response.headers.get("Content-Length")
        encoded = response.headers.get("Content-Encoding")
        if written == 0:
            last_error = PipelineError(INVALID_RESPONSE, "empty response body")
            continue
        if expected and not encoded and expected.isdigit() and int(expected) != written:
            last_error = PipelineError(CORRUPT_SOURCE, f"got {written} bytes, expected {expected}")
            continue
        part.replace(target)
        return written

    part.unlink(missing_ok=True)
    assert last_error is not None
    raise last_error


def download_version(
    cfg: Dict[str, Any], client: RequestClient, throttle: Throttle, arxiv_id: str, version: int
) -> Dict[str, Any]:
    """Download one version and return its manifest (also written to disk)."""
    run_dir = cfg["output_root"] / cfg["run_id"]
    key = paper_key(arxiv_id)
    manifest = make_manifest(arxiv_id, version)
    manifest_path = run_dir / "papers" / key / "versions" / f"v{version}" / "manifest.json"
    archive = run_dir / "tmp" / key / f"v{version}" / "source.archive"

    try:
        throttle.wait()
        size = _stream_to_file(client, SRC_URL.format(versioned_id=manifest["versioned_id"]), archive)
        manifest["source_format"] = detect_format(archive)
        manifest["archive_bytes"] = size
        manifest["archive_path"] = archive.relative_to(run_dir).as_posix()
        manifest["download_status"] = OK
    except PipelineError as error:
        manifest["error_code"] = error.error_code
        manifest["download_status"] = FAILED
        write_json_atomic(manifest_path, manifest)
        raise
    write_json_atomic(manifest_path, manifest)
    return manifest


def run_download(cfg: Dict[str, Any], events: Optional[EventLog] = None) -> Dict[str, int]:
    """Download every version of every paper whose discovery status is ok.
    Returns counts per status (one count per version)."""
    run_dir = cfg["output_root"] / cfg["run_id"]
    if events is None:
        events = EventLog(run_dir / "events.jsonl", cfg["run_id"])
    client = RequestClient(cfg["user_agent"], cfg["timeout"], cfg["max_retries"])
    throttle = Throttle(cfg["arxiv_delay_seconds"])

    counts = {OK: 0, FAILED: 0, SKIPPED: 0}
    for arxiv_id in load_ids(cfg["ids_file"]):
        key = paper_key(arxiv_id)
        metadata = read_json(run_dir / "papers" / key / "metadata.json")
        if not metadata or metadata.get("status") != OK or not metadata.get("versions"):
            counts[SKIPPED] += 1
            events.log(STAGE, SKIPPED, arxiv_id=arxiv_id, message="discovery not ok; no version list")
            continue

        for version in metadata["versions"]:
            manifest_path = run_dir / "papers" / key / "versions" / f"v{version}" / "manifest.json"
            existing = read_json(manifest_path)
            if existing and existing.get("download_status") == OK:
                counts[SKIPPED] += 1
                events.log(STAGE, SKIPPED, arxiv_id=arxiv_id, version=version, message="already downloaded")
                continue

            watch = Stopwatch()
            try:
                manifest = download_version(cfg, client, throttle, arxiv_id, version)
            except Exception as exc:  # noqa: BLE001 - one version must never stop the run
                error = exc if isinstance(exc, PipelineError) else PipelineError(INVALID_RESPONSE, repr(exc))
                if not isinstance(exc, PipelineError):  # unexpected bug: still leave a manifest behind
                    failed = make_manifest(arxiv_id, version)
                    failed["error_code"] = error.error_code
                    write_json_atomic(manifest_path, failed)
                counts[FAILED] += 1
                events.log(
                    STAGE, FAILED, arxiv_id=arxiv_id, version=version, error_code=error.error_code,
                    http_status=error.http_status, attempts=error.attempts,
                    message=str(error)[:300], elapsed_seconds=watch.elapsed,
                )
            else:
                counts[OK] += 1
                events.log(
                    STAGE, OK, arxiv_id=arxiv_id, version=version, elapsed_seconds=watch.elapsed,
                    message=f"{manifest['source_format']}, {manifest['archive_bytes']} bytes",
                )
    return counts


def main() -> None:
    cfg = load_config()
    counts = run_download(cfg)
    print(f"run_id={cfg['run_id']} download (per version): {counts}")


if __name__ == "__main__":
    main()
