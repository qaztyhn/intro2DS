"""Shared metric calculations for the crawling pipeline."""

from contextlib import contextmanager
from uuid import uuid4
from collections.abc import Iterable
from math import isfinite
from numbers import Real
from time import perf_counter
import os
from pathlib import Path
import psutil
from threading import Event, Thread
import json
from datetime import datetime, timezone
from threading import Lock

def safe_ratio(numerator: int, denominator: int) -> float | None:
    """Return a proportion in [0, 1], or None for a zero denominator."""
    for name, value in (
        ("numerator", numerator),
        ("denominator", denominator),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an integer")

    if denominator < 0 or numerator < 0:
        raise ValueError("Counts must be non-negative")

    if numerator > denominator:
        raise ValueError("Numerator cannot exceed denominator")

    if denominator == 0:
        return None

    return numerator / denominator


def mean_or_none(values: Iterable[float]) -> float | None:
    """Return the mean of non-negative finite measurements."""
    total = 0.0
    count = 0

    for value in values:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise TypeError("Measurements must be real numbers")

        if not isfinite(value) or value < 0:
            raise ValueError("Measurements must be finite and non-negative")

        total += value
        count += 1

    return total / count if count else None


class Timer:
    """Measure elapsed wall time for one operation."""

    def __init__(self):
        self.duration_s: float | None = None
        self._started_at: float | None = None

    def __enter__(self):
        if self._started_at is not None:
            raise RuntimeError("Timer is already running")

        self.duration_s = None
        self._started_at = perf_counter()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self.duration_s = perf_counter() - self._started_at
        finally:
            self._started_at = None

        return False

def directory_size_bytes(directory: str | Path) -> int:
    """Sum logical file sizes without following symbolic links."""
    root = Path(directory)

    if root.is_symlink():
        raise ValueError("Root directory must not be a symbolic link")

    if not root.is_dir():
        raise NotADirectoryError(str(root))

    total = 0

    def raise_walk_error(error):
        raise error

    for current, directories, files in os.walk(
        root,
        followlinks=False,
        onerror=raise_walk_error,
    ):
        current = Path(current)

        directories[:] = [
            name
            for name in directories
            if not (current / name).is_symlink()
        ]

        for name in files:
            path = current / name

            try:
                if path.is_symlink():
                    continue

                total += path.stat().st_size
            except FileNotFoundError:
                # Temporary files may disappear during sampling.
                continue

    return total

def process_tree_rss_bytes(pid: int | None = None) -> int:
    """Sum RSS bytes of a root process and its current descendants."""
    root = psutil.Process(pid)
    processes = [root, *root.children(recursive=True)]
    total = 0

    for process in processes:
        try:
            total += process.memory_info().rss
        except psutil.NoSuchProcess:
            # A child may exit between enumeration and measurement.
            continue

    return total

def summarize_rss_samples(
    samples: Iterable[tuple[float, int]],
) -> dict:
    """Summarize (elapsed_seconds, rss_bytes) samples.

    Use each sample's RSS until the next sample.
    The last sample marks the end of the measured interval.
    """
    rows = list(samples)

    for index, (timestamp, rss) in enumerate(rows):
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, Real)
        ):
            raise TypeError("Sample time must be a real number")

        if not isfinite(timestamp) or timestamp < 0:
            raise ValueError("Sample time must be finite and non-negative")

        if isinstance(rss, bool) or not isinstance(rss, int):
            raise TypeError("RSS must be an integer")

        if rss < 0:
            raise ValueError("RSS must be non-negative")

        if index > 0 and timestamp <= rows[index - 1][0]:
            raise ValueError("Sample times must be strictly increasing")

    duration = rows[-1][0] - rows[0][0] if len(rows) >= 2 else 0.0

    area = sum(
        rss * (next_time - timestamp)
        for (timestamp, rss), (next_time, _) in zip(rows, rows[1:])
    )

    return {
        "sample_count": len(rows),
        "sampled_duration_s": duration,
        "rss_peak_bytes": max((rss for _, rss in rows), default=None),
        "rss_mean_bytes": area / duration if duration > 0 else None,
    }

class ResourceSampler:
    """Collect RAM and disk samples for one pipeline run."""

    def __init__(
        self,
        directory: str | Path,
        pid: int | None = None,
    ):
        self.directory = Path(directory)
        self.pid = os.getpid() if pid is None else pid
        self._started_at = perf_counter()
        self.samples: list[dict] = []

    def sample(self) -> dict:
        """Collect one sample; propagate measurement errors."""
        rss_bytes = process_tree_rss_bytes(self.pid)
        disk_bytes = directory_size_bytes(self.directory)
        elapsed_s = perf_counter() - self._started_at

        record = {
            "elapsed_s": elapsed_s,
            "rss_bytes": rss_bytes,
            "disk_bytes": disk_bytes,
        }
        self.samples.append(record)
        return record.copy()

    def summary(self) -> dict:
        """Summarize successfully collected samples."""
        result = summarize_rss_samples(
            (sample["elapsed_s"], sample["rss_bytes"])
            for sample in self.samples
        )
        result["disk_peak_bytes"] = max(
            (sample["disk_bytes"] for sample in self.samples),
            default=None,
        )
        return result

class ResourceMonitor:
    """Sample resources in the background during a pipeline run."""

    def __init__(
        self,
        directory: str | Path,
        interval_s: float = 1.0,
        pid: int | None = None,
    ):
        if isinstance(interval_s, bool) or not isinstance(interval_s, Real):
            raise TypeError("Sampling interval must be a real number")
        if not isfinite(interval_s) or interval_s <= 0:
            raise ValueError("Sampling interval must be finite and positive")

        self.directory = Path(directory)
        self.interval_s = interval_s
        self.pid = pid
        self.sampler: ResourceSampler | None = None
        self.error: Exception | None = None
        self._stop = Event()
        self._thread: Thread | None = None

    def _run(self):
        try:
            while not self._stop.wait(self.interval_s):
                self.sampler.sample()
        except Exception as error:
            self.error = error
            self._stop.set()

    def __enter__(self):
        if self._thread is not None:
            raise RuntimeError("Monitor instances can only be used once")

        self.sampler = ResourceSampler(self.directory, self.pid)
        self.sampler.sample()

        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._stop.set()
        self._thread.join()

        if self.error is None:
            try:
                self.sampler.sample()
            except Exception as error:
                self.error = error

        if self.error is not None and exc_type is None:
            raise RuntimeError("Resource monitoring failed") from self.error

        return False

class EventLogger:
    """Append structured events to a JSONL file."""

    def __init__(self, path: str | Path, run_id: str):
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")

        self.path = Path(path)
        self.run_id = run_id
        self._lock = Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, stage: str, event: str, **fields) -> None:
        for name, value in (("stage", stage), ("event", event)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")

        reserved = {"run_id", "timestamp", "stage", "event"}
        if reserved.intersection(fields):
            raise ValueError("Fields cannot overwrite reserved keys")

        record = {
            "run_id": self.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "event": event,
            **fields,
        }

        # Validate serialization before writing anything.
        line = json.dumps(record, ensure_ascii=False, allow_nan=False)

        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

@contextmanager
def measure_stage(logger: EventLogger, stage: str, **fields):
    """Measure an operation and log its start and final outcome."""
    reserved = {
        "operation_id",
        "status",
        "duration_s",
        "error_type",
        "error_message",
    }
    if reserved.intersection(fields):
        raise ValueError("Fields cannot overwrite measurement keys")

    operation_id = uuid4().hex

    logger.log(
        stage,
        "started",
        operation_id=operation_id,
        **fields,
    )

    timer = Timer()

    try:
        with timer:
            yield timer
    except BaseException as error:
        try:
            logger.log(
                stage,
                "finished",
                operation_id=operation_id,
                status="failed",
                duration_s=timer.duration_s,
                error_type=type(error).__name__,
                error_message=str(error),
                **fields,
            )
        except Exception as logging_error:
            error.add_note(
                f"Could not write failure log: {logging_error}"
            )
        raise
    else:
        logger.log(
            stage,
            "finished",
            operation_id=operation_id,
            status="ok",
            duration_s=timer.duration_s,
            **fields,
        )

def summarize_references(
    input_ids: Iterable[str],
    results: dict[str, dict],
) -> dict:
    """Summarize reference retrieval outcomes for the input papers."""
    ids = list(input_ids)

    if any(not isinstance(pid, str) or not pid.strip() for pid in ids):
        raise ValueError("Input IDs must be non-empty strings")

    if len(ids) != len(set(ids)):
        raise ValueError("Input IDs must be unique")

    if set(results) - set(ids):
        raise ValueError("Results contain IDs outside the input set")

    allowed_statuses = {
        "pending", "running", "ok", "empty", "failed", "skipped"
    }
    valid_count = 0
    empty_count = 0
    total_references = 0
    with_arxiv_id = 0
    status_counts = dict.fromkeys(sorted(allowed_statuses), 0)

    for paper_id in ids:
        result = results.get(paper_id, {"status": "pending"})
        status = result["status"]

        if status not in allowed_statuses:
            raise ValueError(f"Unknown reference status: {status}")

        status_counts[status] += 1

        if status not in {"ok", "empty"}:
            continue

        references = result["references"]

        if not isinstance(references, list):
            raise TypeError("References must be a list")

        if any(not isinstance(ref, dict) for ref in references):
            raise TypeError("Each reference must be a dictionary")

        if status == "empty" and references:
            raise ValueError("Empty status cannot contain references")

        if status == "ok" and not references:
            raise ValueError("Use empty status for an empty response")

        valid_count += 1
        empty_count += status == "empty"
        total_references += len(references)
        with_arxiv_id += sum(
            isinstance(ref.get("ref_arxiv_id"), str)
            and bool(ref["ref_arxiv_id"].strip())
            for ref in references
        )

    return {
        "input_paper_count": len(ids),
        "reference_status_counts": status_counts,
        "reference_response_success_rate": safe_ratio(valid_count, len(ids)),
        "reference_empty_paper_count": empty_count,
        "reference_count": total_references,
        "references_per_valid_response": (
            total_references / valid_count if valid_count else None
        ),
        "reference_with_arxiv_id_count": with_arxiv_id,
        "reference_with_arxiv_id_rate": safe_ratio(
            with_arxiv_id, total_references
        ),
    }

def summarize_versions(
    expected_versions: dict[str, list[int] | None],
    results: dict[str, dict[int, dict]],
) -> dict:
    """Summarize version coverage for papers with known version lists."""
    if set(results) - set(expected_versions):
        raise ValueError("Results contain unexpected paper IDs")

    expected_count = 0
    processed_count = 0
    with_tex_count = 0
    no_tex_count = 0
    unknown_paper_count = 0

    allowed = {"pending", "running", "ok", "empty", "failed", "skipped"}

    for paper_id, versions in expected_versions.items():
        paper_results = results.get(paper_id, {})

        if versions is None:
            unknown_paper_count += 1
            if paper_results:
                raise ValueError(
                    "Version results require a known expected version list"
                )
            continue

        if not versions:
            raise ValueError("Known version lists must not be empty")

        if any(
            isinstance(v, bool) or not isinstance(v, int) or v < 1
            for v in versions
        ):
            raise ValueError("Versions must be positive integers")

        if len(versions) != len(set(versions)):
            raise ValueError("Expected versions must be unique")

        if set(paper_results) - set(versions):
            raise ValueError("Results contain unexpected versions")

        expected_count += len(versions)

        for version in versions:
            record = paper_results.get(
                version, {"extract_status": "pending"}
            )
            status = record["extract_status"]

            if status not in allowed:
                raise ValueError(f"Unknown extraction status: {status}")

            if status == "ok":
                with_tex_count += 1
                processed_count += 1
            elif status == "empty":
                if record.get("error_code") != "NO_TEX_SOURCE":
                    raise ValueError(
                        "Empty extraction must confirm NO_TEX_SOURCE"
                    )
                no_tex_count += 1
                processed_count += 1

    return {
        "input_paper_count": len(expected_versions),
        "unknown_versions_paper_count": unknown_paper_count,
        "known_expected_version_count": expected_count,
        "processed_version_count": processed_count,
        "with_tex_version_count": with_tex_count,
        "no_tex_version_count": no_tex_count,
        "unresolved_version_count": expected_count - processed_count,
        "known_version_processing_coverage": safe_ratio(
            processed_count, expected_count
        ),
    }

def summarize_papers(
    input_ids: Iterable[str],
    results: dict[str, dict],
) -> dict:
    """Count complete papers using discovery, sources, refs and schema."""
    ids = list(input_ids)

    if any(not isinstance(pid, str) or not pid.strip() for pid in ids):
        raise ValueError("Input IDs must be non-empty strings")

    if len(ids) != len(set(ids)):
        raise ValueError("Input IDs must be unique")

    if set(results) - set(ids):
        raise ValueError("Results contain unexpected paper IDs")

    counts = {
        "discovery_ok_count": 0,
        "source_complete_count": 0,
        "references_ok_count": 0,
        "schema_ok_count": 0,
        "complete_paper_count": 0,
    }

    allowed = {"pending", "running", "ok", "empty", "failed", "skipped"}

    for paper_id in ids:
        record = results.get(paper_id, {})
        discovery_status = record.get("discovery_status", "pending")
        validation_status = record.get("validation_status", "pending")

        if discovery_status not in allowed:
            raise ValueError("Unknown discovery status")
        if validation_status not in allowed:
            raise ValueError("Unknown validation status")

        versions = summarize_versions(
            {paper_id: record.get("expected_versions")},
            {paper_id: record.get("versions", {})},
        )
        references = summarize_references(
            [paper_id],
            {paper_id: record.get(
                "references", {"status": "pending"}
            )},
        )

        discovery_ok = discovery_status == "ok"
        source_complete = (
            versions["unknown_versions_paper_count"] == 0
            and versions["unresolved_version_count"] == 0
        )
        references_ok = (
            references["reference_response_success_rate"] == 1.0
        )
        schema_ok = validation_status == "ok"

        counts["discovery_ok_count"] += discovery_ok
        counts["source_complete_count"] += source_complete
        counts["references_ok_count"] += references_ok
        counts["schema_ok_count"] += schema_ok
        counts["complete_paper_count"] += (
            discovery_ok
            and source_complete
            and references_ok
            and schema_ok
        )

    return {
        "input_paper_count": len(ids),
        **counts,
        "incomplete_paper_count": len(ids) - counts["complete_paper_count"],
        "paper_completion_rate": safe_ratio(
            counts["complete_paper_count"], len(ids)
        ),
    }

def summarize_source_sizes(
    input_ids: Iterable[str],
    results: dict[str, dict],
) -> dict:
    """Compare source sizes on papers with complete paired measurements."""
    ids = list(input_ids)

    if any(not isinstance(pid, str) or not pid.strip() for pid in ids):
        raise ValueError("Input IDs must be non-empty strings")

    if len(ids) != len(set(ids)):
        raise ValueError("Input IDs must be unique")

    if set(results) - set(ids):
        raise ValueError("Results contain unexpected paper IDs")

    before_sizes = []
    after_sizes = []

    for paper_id in ids:
        record = results.get(paper_id, {})
        complete = record.get("size_measurement_complete", False)

        if not isinstance(complete, bool):
            raise TypeError("size_measurement_complete must be a boolean")

        if not complete:
            continue

        before = record.get("unpacked_total_bytes")
        after = record.get("kept_source_bytes")

        for value in (before, after):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError("Complete sizes must be integer bytes")
            if value < 0:
                raise ValueError("Sizes must be non-negative")

        if after > before:
            raise ValueError("Kept source size cannot exceed original size")

        before_sizes.append(before)
        after_sizes.append(after)

    total_before = sum(before_sizes)
    total_after = sum(after_sizes)

    return {
        "input_paper_count": len(ids),
        "size_measured_paper_count": len(before_sizes),
        "size_unmeasured_paper_count": len(ids) - len(before_sizes),
        "source_bytes_before_total": total_before,
        "source_bytes_after_total": total_after,
        "source_bytes_before_mean": mean_or_none(before_sizes),
        "source_bytes_after_mean": mean_or_none(after_sizes),
        "source_reduction_rate": safe_ratio(
            total_before - total_after, total_before
        ),
    }

def read_events(path: str | Path) -> list[dict]:
    """Read JSONL events, reporting malformed records by line number."""
    events = []
    required = {"run_id", "timestamp", "stage", "event"}

    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Invalid JSON at line {line_number}"
                ) from error

            if not isinstance(record, dict):
                raise ValueError(
                    f"Expected an event object at line {line_number}"
                )

            missing = required - record.keys()
            if missing:
                raise ValueError(
                    f"Missing fields at line {line_number}: "
                    f"{sorted(missing)}"
                )

            for key in required:
                value = record[key]
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"Invalid {key} at line {line_number}"
                    )

            events.append(record)

    return events

def summarize_stage_times(
    events: Iterable[dict],
    run_id: str,
    stage: str,
) -> dict:
    """Summarize timed operations for one stage of one run."""
    started = {}
    finished = {}
    durations = []
    success_count = 0
    failed_count = 0

    for event in events:
        if event["run_id"] != run_id or event["stage"] != stage:
            continue

        event_type = event["event"]
        if event_type not in {"started", "finished"}:
            continue

        operation_id = event.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise ValueError("Timed events require an operation_id")

        if event_type == "started":
            if operation_id in started:
                raise ValueError("Duplicate operation start")
            started[operation_id] = event
            continue

        if operation_id not in started:
            raise ValueError("Operation finished without a start")

        if operation_id in finished:
            raise ValueError("Duplicate operation finish")

        start = started[operation_id]
        for key in ("arxiv_id", "version"):
            if start.get(key) != event.get(key):
                raise ValueError(f"Mismatched {key} between timed events")

        duration = event.get("duration_s")
        if isinstance(duration, bool) or not isinstance(duration, Real):
            raise TypeError("Duration must be a real number")
        if not isfinite(duration) or duration < 0:
            raise ValueError("Duration must be finite and non-negative")

        status = event.get("status")
        if status not in {"ok", "failed"}:
            raise ValueError("Timed outcome must be ok or failed")

        finished[operation_id] = event
        durations.append(duration)
        success_count += status == "ok"
        failed_count += status == "failed"

    return {
        "run_id": run_id,
        "stage": stage,
        "started_operation_count": len(started),
        "finished_operation_count": len(finished),
        "unfinished_operation_count": len(started) - len(finished),
        "successful_operation_count": success_count,
        "failed_operation_count": failed_count,
        "duration_sum_s": sum(durations),
        "duration_mean_s": mean_or_none(durations),
        "duration_min_s": min(durations, default=None),
        "duration_max_s": max(durations, default=None),
    }
def log_resource_results(
    logger: EventLogger,
    sampler: ResourceSampler,
    output_directory: str | Path,
) -> dict:
    """Log collected samples and measure final corpus output size.

    Call after monitoring has stopped and output writing has finished.
    """
    summary = sampler.summary()
    summary["output_storage_bytes"] = directory_size_bytes(
        output_directory
    )

    for sample in sampler.samples:
        logger.log(
            "resources",
            "sample",
            **sample,
        )

    logger.log(
        "resources",
        "summary",
        **summary,
    )

    return summary

def make_request_hook(
    logger: EventLogger,
    stage: str,
    *,
    arxiv_id: str | None = None,
    version: int | None = None,
):
    """Create a request-attempt callback with fixed context."""

    def on_attempt(
        *,
        attempt: int,
        http_status: int | None,
        error_code: str | None,
        duration: float,
        error_message: str | None,
    ) -> None:
        if isinstance(attempt, bool) or not isinstance(attempt, int):
            raise TypeError("Attempt must be an integer")
        if attempt < 1:
            raise ValueError("Attempt must start at 1")

        if isinstance(duration, bool) or not isinstance(duration, Real):
            raise TypeError("Duration must be a real number")
        if not isfinite(duration) or duration < 0:
            raise ValueError("Duration must be finite and non-negative")

        logger.log(
            stage,
            "request_attempt_finished",
            arxiv_id=arxiv_id,
            version=version,
            attempt=attempt,
            http_status=http_status,
            error_code=error_code,
            duration_s=duration,
            error_message=error_message,
        )
    return on_attempt