"""Helpers shared across the pipeline (discovery, download, stages, tools).

Contents: status constants, paper-id helpers, atomic JSON read/write, the run-level
EventLogger (events.jsonl) and Stopwatch. Per-operation timing and resource metrics are
a separate EventLogger in metrics.py (metrics_events.jsonl).

Status vocabulary follows section 4 of Intro2DS_Project.md; error codes live in utils/errors.py.
"""

import json
import os
import pathlib
import re
import time
from threading import Lock
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from .errors import INVALID_RESPONSE, CrawlError

# --- status vocabulary -------------------------------------------------------
PENDING = "pending"
RUNNING = "running"
OK = "ok"
EMPTY = "empty"
FAILED = "failed"
SKIPPED = "skipped"


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
        raise CrawlError(f"no version suffix in id {short_id!r}", INVALID_RESPONSE)
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
    try: 
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException: 
        tmp.unlink(missing_ok=True) # never leave a stale .tmp behind; the old file stays intact
        raise


def read_json(path: pathlib.Path) -> Optional[Dict[str, Any]]:
    """Return parsed JSON, or None if the file is missing or unreadable."""
    try:
        with pathlib.Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


class EventLogger:
    """Append-only events.jsonl for one run. One line per event.

    Not the same class as metrics.EventLogger, which writes metrics_events.jsonl.
    """
    def __init__(self, path: pathlib.Path, run_id: str):
        self.path = pathlib.Path(path)
        self.run_id = run_id
        self._lock = Lock()
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
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")


class Stopwatch:
    def __init__(self):
        self._start = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._start
