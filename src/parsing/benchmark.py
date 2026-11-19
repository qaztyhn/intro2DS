"""TV4 — reproducible wall-time, peak-RAM and output-size benchmark for M2.

Official benchmark runs go on Google Colab (CPU only); WSL runs are trials. Nothing
here contains a benchmark number: every figure is measured on the run being
reported and every ratio states its numerator, denominator and denominator
definition (Contract §7.3, rule 8).

Measurement choices (recorded in ``benchmark.json``):

* wall time: ``time.perf_counter`` per stage, accumulated over repeated entries;
* peak RAM: a background thread samples the RSS of this process and its children
  (M1's ``process_tree_rss_bytes``) every ~50 ms. Sampling can miss a spike shorter
  than the interval, so the process-lifetime ``ru_maxrss`` is stored as a cross-check;
  without ``psutil`` the lifetime ``ru_maxrss`` is used and flagged as such;
* ``end_to_end`` = processing + export of every publication + merge. Validation and
  checksums are measured as separate stages and are NOT part of ``end_to_end``.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import json
import os
import platform
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Iterator

from ..utils.common import load_ids
from . import export, validation
from .deduplication import deduplicate_elements

SCHEMA_VERSION = 1
SAMPLE_INTERVAL_SECONDS = 0.05
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
MODES = ("pipeline", "tv4_only")

try:  # same RSS definition as the M1 benchmark: process plus current descendants
    from ..metrics import process_tree_rss_bytes as _tree_rss
except ImportError:  # psutil (or M1 metrics) unavailable
    _tree_rss = None


# ------------------------------------------------------------- measuring ----

def _rss_now() -> int | None:
    """RSS in bytes of this process tree, or ``None`` when it cannot be sampled."""
    if _tree_rss is None:
        return None
    try:
        return int(_tree_rss(os.getpid()))
    except Exception:  # a vanished child or a missing /proc must not break the run
        return None


def lifetime_peak_rss_bytes() -> int:
    """High-water RSS of this process since it started (``ru_maxrss``; KiB on Linux)."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


class _Window:
    __slots__ = ("start", "peak")

    def __init__(self, rss: int | None) -> None:
        self.start = rss
        self.peak = rss


class _Sampler(threading.Thread):
    """Background RSS sampler; every open window keeps the maximum seen while open."""

    def __init__(self, interval: float) -> None:
        super().__init__(daemon=True, name="benchmark-rss-sampler")
        self.interval = interval
        self._windows: list[_Window] = []
        self._lock = threading.Lock()
        self._halt = threading.Event()

    def open(self) -> _Window:
        window = _Window(_rss_now())
        with self._lock:
            self._windows.append(window)
        return window

    def close(self, window: _Window) -> None:
        rss = _rss_now()
        with self._lock:
            if rss is not None and (window.peak is None or rss > window.peak):
                window.peak = rss
            self._windows.remove(window)

    def run(self) -> None:
        while not self._halt.wait(self.interval):
            rss = _rss_now()
            if rss is None:
                continue
            with self._lock:
                for window in self._windows:
                    if window.peak is None or rss > window.peak:
                        window.peak = rss

    def stop(self) -> None:
        self._halt.set()
        self.join()


class _Stage(contextlib.ContextDecorator):
    """``with bench.stage("x"):`` or ``@bench.stage("x")``; repeated use accumulates."""

    def __init__(self, bench: "Benchmark", name: str) -> None:
        self._bench, self._name = bench, name
        self._open: list[tuple[float, _Window]] = []

    def __enter__(self) -> "_Stage":
        self._open.append((time.perf_counter(), self._bench._sampler.open()))
        return self

    def __exit__(self, *exc: Any) -> bool:
        started, window = self._open.pop()
        elapsed = time.perf_counter() - started
        self._bench._sampler.close(window)
        self._bench._record(self._name, elapsed, window)
        return False


class Benchmark:
    """Collects per-stage wall time and peak RSS. Use as a context manager."""

    def __init__(self, interval: float = SAMPLE_INTERVAL_SECONDS) -> None:
        self._sampler = _Sampler(interval)
        self._stages: dict[str, dict[str, Any]] = {}
        self._run: _Window | None = None
        self._run_started = 0.0
        self.run_wall_seconds = 0.0
        self.counters: dict[str, int] = {}

    def __enter__(self) -> "Benchmark":
        self._sampler.start()
        self._run_started = time.perf_counter()
        self._run = self._sampler.open()
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.run_wall_seconds = time.perf_counter() - self._run_started
        self._sampler.close(self._run)
        self._sampler.stop()
        return False

    def stage(self, name: str) -> _Stage:
        return _Stage(self, name)

    def count(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount

    def _record(self, name: str, elapsed: float, window: _Window) -> None:
        entry = self._stages.setdefault(name, {"wall_seconds": 0.0, "calls": 0,
                                               "rss_start_bytes": window.start,
                                               "peak_rss_bytes": window.peak})
        entry["wall_seconds"] += elapsed
        entry["calls"] += 1
        if window.peak is not None and (entry["peak_rss_bytes"] is None
                                        or window.peak > entry["peak_rss_bytes"]):
            entry["peak_rss_bytes"] = window.peak

    def stage_report(self) -> dict[str, dict[str, Any]]:
        """Per stage: wall seconds, calls, RSS at first entry and peak RSS (bytes)."""
        fallback = None if _rss_now() is not None else lifetime_peak_rss_bytes()
        out = {}
        for name, entry in self._stages.items():
            row = dict(entry)
            row["peak_rss_source"] = "psutil_sampling" if fallback is None \
                else "getrusage_process_lifetime"
            if fallback is not None:
                row["peak_rss_bytes"] = fallback
            out[name] = row
        return out

    @property
    def peak_rss_bytes(self) -> int:
        """Peak RSS over the whole run (lifetime ``ru_maxrss`` when sampling is off)."""
        if self._run is not None and self._run.peak is not None:
            return self._run.peak
        return lifetime_peak_rss_bytes()


# ----------------------------------------------------------- environment ----

def detect_platform() -> str:
    """``colab``, ``wsl`` or the lower-cased OS name."""
    if os.environ.get("COLAB_RELEASE_TAG") or "google.colab" in sys.modules:
        return "colab"
    if "microsoft" in platform.release().lower():
        return "wsl"
    return platform.system().lower()


def _package_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _git_state() -> dict[str, Any]:
    """Commit of the repository holding this module, and whether this package differs."""
    here = Path(__file__).resolve().parent

    def run(*args: str) -> str | None:
        try:
            done = subprocess.run(["git", "-C", str(here), *args], capture_output=True,
                                  text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    sha = run("rev-parse", "HEAD")
    status = run("status", "--porcelain", "--", ".") if sha else None
    return {"git_sha": sha, "git_dirty": None if status is None else bool(status)}


def collect_environment() -> dict[str, Any]:
    """Describe the machine and software. No user names, paths or secrets."""
    try:
        import psutil
        total_ram = int(psutil.virtual_memory().total)
    except ImportError:
        total_ram = None
    if total_ram is None and hasattr(os, "sysconf"):
        try:
            total_ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (ValueError, OSError):
            total_ram = None
    affinity = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
    return {"platform": detect_platform(), "system": platform.system(),
            "system_release": platform.release(), "machine": platform.machine(),
            "python_version": platform.python_version(), "cpu_count": os.cpu_count(),
            "cpu_count_available": affinity, "total_ram_bytes": total_ram,
            "psutil_version": _package_version("psutil"),
            "pyarrow_version": _package_version("pyarrow"), **_git_state()}


def id_list_digest(ids: list[str]) -> str:
    """SHA-256 of the unique IDs, sorted and newline-joined (order/duplicates ignored)."""
    return hashlib.sha256("\n".join(sorted(set(ids))).encode("utf-8")).hexdigest()


# -------------------------------------------------------------- helpers -----

def _ratio(numerator: float, denominator: float, definition: str) -> dict[str, Any]:
    return {"numerator": numerator, "denominator": denominator,
            "denominator_definition": definition,
            "value": numerator / denominator if denominator else None}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_bytes(directory: Path) -> int:
    return sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())


def _write_new(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON to ``path`` atomically, refusing to replace an existing file."""
    text = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    handle, temp = tempfile.mkstemp(prefix=".bench-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text)
        try:
            os.link(temp, path)  # fails if ``path`` exists: results are never overwritten
        except FileExistsError:
            raise
        except OSError:  # e.g. a Google Drive mount in Colab has no hard links
            with path.open("x", encoding="utf-8") as out:  # exclusive create, still no overwrite
                out.write(text)
    finally:
        os.unlink(temp)


def _load_pipeline() -> Callable[..., Any] | None:
    """TV1's ``process_publication`` if its module exists, else ``None``."""
    try:
        module = importlib.import_module(f"{__package__}.pipeline")
    except ModuleNotFoundError as error:
        if error.name == f"{__package__}.pipeline":
            return None
        raise  # the pipeline exists but one of ITS imports is broken: do not hide it
    return getattr(module, "process_publication", None)


def _as_rows(arxiv_id: str, result: Any) -> tuple[list[dict], list[dict], list[dict]]:
    """Accept ``(elements, hierarchy, bibtex)`` where bibtex is rows or TV3 BibEntry objects."""
    elements, hierarchy, bibtex = result
    bibtex = list(bibtex)
    if bibtex and hasattr(bibtex[0], "original_key"):
        bibtex = export.bibtex_rows(arxiv_id, bibtex)
    return list(elements), list(hierarchy), bibtex


def _callable_name(function: Callable[..., Any]) -> str:
    return f"{getattr(function, '__module__', '?')}.{getattr(function, '__qualname__', '?')}"


# -------------------------------------------------------------- the run -----

def run_benchmark(run_dir: Path, id_list_path: Path, output_dir: Path, label: str, *,
                  config: dict[str, Any] | None = None,
                  process: Callable[[Path, str], Any] | None = None,
                  documents: dict[str, list] | None = None,
                  keep_parts: bool = False, validate: bool = True,
                  sample_interval: float = SAMPLE_INTERVAL_SECONDS) -> dict[str, Any]:
    """Run M2 over the IDs in ``id_list_path`` and write ``benchmark_<label>.json``.

    ``process(run_dir, arxiv_id)`` must return ``(elements, hierarchy, bibtex)``. When
    omitted, TV1's ``process_publication`` is used if importable (mode ``pipeline``);
    otherwise, if ``documents`` (``{arxiv_id: [ParsedDocument, ...]}``) is given, only
    TV4's deduplication/export/merge are measured (mode ``tv4_only``) and the file says so.

    Nothing is cached or overwritten: the work directory ``output_dir/<label>`` must not
    exist yet and the result file must not exist yet (``FileExistsError`` otherwise).
    A publication that raises is recorded as failed and the run continues.
    Returns the dict that was written.
    """
    if not isinstance(label, str) or not _LABEL.fullmatch(label):
        raise ValueError(f"label must match {_LABEL.pattern!r}: {label!r}")
    ids = load_ids(Path(id_list_path))
    if not ids:
        raise ValueError("the ID list is empty")
    output_dir = Path(output_dir)
    result_path = output_dir / f"benchmark_{label}.json"
    work = output_dir / label
    if result_path.exists() or work.exists():
        raise FileExistsError(f"label {label!r} was already used in {output_dir}")

    if process is not None:
        mode, process_source = "pipeline", _callable_name(process)
    elif (found := _load_pipeline()) is not None:
        process, mode, process_source = found, "pipeline", _callable_name(found)
    elif documents is not None:
        def process(_run_dir: Path, arxiv_id: str, _docs=documents):  # noqa: E306
            elements, hierarchy = deduplicate_elements(_docs[arxiv_id])
            return elements, hierarchy, []
        mode, process_source = "tv4_only", "deduplication.deduplicate_elements"
    else:
        raise RuntimeError("TV1's pipeline is not available and no documents were given; "
                           "pass documents=... to benchmark TV4 alone (mode tv4_only)")
    first_stage = "pipeline" if mode == "pipeline" else "deduplicate"

    parts, tables = work / "parts", work / "tables"
    parts.mkdir(parents=True)
    started_at = datetime.now(timezone.utc).isoformat()
    failures: list[dict[str, str]] = []
    totals = {"publications_ok": 0, "versions_exported": 0, "elements": 0,
              "hierarchy_rows": 0, "bibtex_rows": 0}

    with Benchmark(sample_interval) as bench:
        with bench.stage("end_to_end"):
            for arxiv_id in ids:
                stage = first_stage
                try:
                    with bench.stage(first_stage):
                        result = process(Path(run_dir), arxiv_id)
                    stage = "export_parts"
                    with bench.stage("export_parts"):
                        elements, hierarchy, bibtex = _as_rows(arxiv_id, result)
                        export.write_part(arxiv_id, elements, hierarchy, bibtex, parts)
                except Exception as error:  # one publication must not stop the batch
                    failures.append({"arxiv_id": arxiv_id, "stage": stage,
                                     "error_type": type(error).__name__,
                                     "message": str(error)[:300]})
                    continue
                totals["publications_ok"] += 1
                totals["versions_exported"] += sum(e["kind"] == "document" for e in elements)
                totals["elements"] += len(elements)
                totals["hierarchy_rows"] += len(hierarchy)
                totals["bibtex_rows"] += len(bibtex)
            with bench.stage("merge"):
                export.merge_parts(parts, tables)
        validation_summary = None
        if validate:
            with bench.stage("validate"):
                report = validation.validate_tables(tables)
            validation_summary = {"passed": report["passed"],
                                  "error_total": report["counts"]["error_total"],
                                  "warning_total": report["counts"]["warning_total"]}
        with bench.stage("checksum"):
            output_files = {p.name: {"bytes": p.stat().st_size, "sha256": _file_sha256(p)}
                            for p in sorted(tables.glob("*.parquet"))}

    stages = bench.stage_report()
    parts_bytes = _tree_bytes(parts)
    if not keep_parts:
        shutil.rmtree(parts)
    wall = stages["end_to_end"]["wall_seconds"]
    attempted = len(ids)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "label": label,
        "mode": mode,
        "process_source": process_source,
        "data_source": "m1_run_dir" if mode == "pipeline" else "caller_documents",
        "started_at_utc": started_at,
        "run_dir_name": Path(run_dir).name,
        "id_list": {"file_name": Path(id_list_path).name, "sha256": id_list_digest(ids),
                    "count": len(ids),
                    "sha256_definition": "SHA-256 of unique IDs, sorted, joined by newline"},
        "config": config or {},
        "environment": collect_environment(),
        "counts": {"publications_attempted": attempted, **totals,
                   "publications_failed": len(failures)},
        "failures": failures[:50],
        "stages": stages,
        "end_to_end_wall_seconds": wall,
        "peak_rss_bytes": stages["end_to_end"]["peak_rss_bytes"],
        "peak_rss_bytes_whole_run": bench.peak_rss_bytes,
        "process_lifetime_max_rss_bytes": lifetime_peak_rss_bytes(),
        "output": {"files": output_files,
                   "total_bytes": sum(f["bytes"] for f in output_files.values()),
                   "intermediate_parts_bytes": parts_bytes,
                   "parts_kept": keep_parts},
        "throughput": {
            "versions_per_second": _ratio(
                totals["versions_exported"], wall,
                "wall seconds of stage end_to_end (processing, export, merge; excludes "
                "validation and checksums); numerator counts exported versions only"),
            "publications_per_second": _ratio(
                totals["publications_ok"], wall,
                "wall seconds of stage end_to_end; numerator counts publications that "
                "finished without error"),
            "mean_seconds_per_publication": _ratio(
                wall, attempted,
                "publications attempted (successful and failed); numerator is the wall "
                "seconds of stage end_to_end"),
        },
        "validation": validation_summary,
        "notes": ["peak_rss_bytes is the maximum sampled RSS of this process and its "
                  "children during stage end_to_end; spikes shorter than the sampling "
                  "interval can be missed (see process_lifetime_max_rss_bytes)."],
    }
    _write_new(result_path, payload)
    return payload


# ------------------------------------------------------ reading results -----

def check_comparable(first: dict[str, Any], second: dict[str, Any]) -> list[str]:
    """Reasons two benchmark dicts must not be compared (empty list = comparable).

    Conclusions are only valid for the same ID list, the same environment and the same
    mode; the output checksums are compared too so unequal outputs are not mistaken for
    a speed difference.
    """
    reasons = []
    if first["id_list"]["sha256"] != second["id_list"]["sha256"]:
        reasons.append("different ID lists (sha256 differs)")
    if first["mode"] != second["mode"]:
        reasons.append(f"different modes: {first['mode']} vs {second['mode']}")
    for key in ("platform", "python_version", "cpu_count", "cpu_count_available",
                "total_ram_bytes", "pyarrow_version"):
        if first["environment"].get(key) != second["environment"].get(key):
            reasons.append(f"different environment: {key}")
    out_a = {n: f["sha256"] for n, f in first["output"]["files"].items()}
    out_b = {n: f["sha256"] for n, f in second["output"]["files"].items()}
    if out_a != out_b:
        reasons.append("outputs are not byte-identical")
    return reasons


def load_benchmarks(directory: Path):
    """One row per ``benchmark_*.json`` in ``directory`` as a pandas DataFrame.

    Scalar columns plus ``stage_<name>_seconds``; ordered by label. Meant for plotting
    at Jupyter Book build time. The empty case still returns every base column.
    """
    import pandas as pd

    rows = []
    for path in sorted(Path(directory).glob("benchmark_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        env, counts, ratios = data["environment"], data["counts"], data["throughput"]
        row = {
            "label": data["label"], "mode": data["mode"], "platform": env["platform"],
            "git_sha": env["git_sha"], "python_version": env["python_version"],
            "cpu_count": env["cpu_count"], "total_ram_bytes": env["total_ram_bytes"],
            "id_list_sha256": data["id_list"]["sha256"], "id_count": data["id_list"]["count"],
            "publications_ok": counts["publications_ok"],
            "publications_failed": counts["publications_failed"],
            "versions_exported": counts["versions_exported"],
            "end_to_end_wall_seconds": data["end_to_end_wall_seconds"],
            "peak_rss_bytes": data["peak_rss_bytes"],
            "output_total_bytes": data["output"]["total_bytes"],
            "versions_per_second": ratios["versions_per_second"]["value"],
            "mean_seconds_per_publication": ratios["mean_seconds_per_publication"]["value"],
            "file": path.name,
        }
        for name, stage in data["stages"].items():
            row[f"stage_{name}_seconds"] = stage["wall_seconds"]
        rows.append(row)
    columns = ["label", "mode", "platform", "git_sha", "python_version", "cpu_count",
               "total_ram_bytes", "id_list_sha256", "id_count", "publications_ok",
               "publications_failed", "versions_exported", "end_to_end_wall_seconds",
               "peak_rss_bytes", "output_total_bytes", "versions_per_second",
               "mean_seconds_per_publication", "file"]
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=columns)
    stage_columns = sorted(c for c in frame.columns if c.startswith("stage_"))
    return frame[columns[:-1] + stage_columns + ["file"]].sort_values("label").reset_index(
        drop=True)
