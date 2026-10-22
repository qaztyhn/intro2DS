"""Helpers shared by the baseline modules (discovery, download).

Status / error vocabulary follows section 4 of Intro2DS_Project.md.
"""

import json
import os
import pathlib
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# --- status vocabulary -------------------------------------------------------
PENDING = "pending"
RUNNING = "running"
OK = "ok"
EMPTY = "empty"
FAILED = "failed"
SKIPPED = "skipped"

# --- error codes -------------------------------------------------------------
TIMEOUT = "TIMEOUT"
NETWORK_ERROR = "NETWORK_ERROR"
HTTP_429 = "HTTP_429"
HTTP_5XX = "HTTP_5XX"
NOT_FOUND = "NOT_FOUND"
INVALID_RESPONSE = "INVALID_RESPONSE"
CORRUPT_SOURCE = "CORRUPT_SOURCE"
UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"


class PipelineError(Exception):
    """A failure that has already been classified with one of the error codes."""

    def __init__(
        self,
        error_code: str,
        message: str = "",
        http_status: Optional[int] = None,
        attempts: Optional[int] = None,
    ):
        super().__init__(message or error_code)
        self.error_code = error_code
        self.http_status = http_status
        self.attempts = attempts


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def paper_key(arxiv_id: str) -> str:
    """Directory name for a paper: '.' -> '-', '/' -> '__'. The original id is
    always kept inside the data files."""
    return arxiv_id.replace(".", "-").replace("/", "__")


_VERSION_SUFFIX = re.compile(r"v(\d+)$")


def split_versioned_id(short_id: str):
    """'2310.12345v3' -> ('2310.12345', 3); 'hep-th/9901001v1' -> ('hep-th/9901001', 1)."""
    match = _VERSION_SUFFIX.search(short_id)
    if not match:
        raise PipelineError(INVALID_RESPONSE, f"no version suffix in id {short_id!r}")
    return short_id[: match.start()], int(match.group(1))


def load_ids(ids_path: pathlib.Path):
    """Read base arXiv ids; ignore blank lines and lines starting with '#'.
    Duplicates are dropped while keeping the first occurrence order."""
    seen, ids = set(), []
    with pathlib.Path(ids_path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line not in seen:
                seen.add(line)
                ids.append(line)
    return ids


def write_json_atomic(path: pathlib.Path, data: Any) -> None:
    """Write via a temp file + rename so a crash never leaves a half-written file."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


def read_json(path: pathlib.Path) -> Optional[Dict[str, Any]]:
    """Return parsed JSON, or None if the file is missing or unreadable."""
    try:
        with pathlib.Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


class EventLog:
    """Append-only events.jsonl for one run. One line per event."""

    def __init__(self, path: pathlib.Path, run_id: str):
        self.path = pathlib.Path(path)
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        stage: str,
        status: str,
        arxiv_id: Optional[str] = None,
        version: Optional[int] = None,
        error_code: Optional[str] = None,
        http_status: Optional[int] = None,
        attempts: Optional[int] = None,
        message: Optional[str] = None,
        elapsed_seconds: Optional[float] = None,
    ) -> None:
        event = {
            "ts": utc_now_iso(),
            "run_id": self.run_id,
            "arxiv_id": arxiv_id,
            "version": version,
            "stage": stage,
            "status": status,
            "error_code": error_code,
            "http_status": http_status,
            "attempts": attempts,
            "message": message,
            "elapsed_seconds": None if elapsed_seconds is None else round(elapsed_seconds, 3),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")


class Stopwatch:
    def __init__(self):
        self._start = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._start


class Throttle:
    """Keeps at least ``delay`` seconds between two requests to arXiv.
    Call ``wait()`` before a request; call ``mark()`` after requests made elsewhere
    (e.g. by the arxiv package) so the next ``wait()`` accounts for them."""

    def __init__(self, delay: float):
        self.delay = delay
        self._last: Optional[float] = None

    def wait(self) -> None:
        if self._last is not None:
            remaining = self.delay - (time.monotonic() - self._last)
            if remaining > 0:
                time.sleep(remaining)
        self._last = time.monotonic()

    def mark(self) -> None:
        self._last = time.monotonic()
