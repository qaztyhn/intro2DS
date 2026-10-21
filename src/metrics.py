"""Shared metric calculations for the crawling pipeline."""

from collections.abc import Iterable
from math import isfinite
from numbers import Real
from time import perf_counter
import os
from pathlib import Path
import psutil

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