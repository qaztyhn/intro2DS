"""Baseline (B) pipeline driver. One run = one run_id = one events.jsonl.

Papers are processed one at a time:
    discovery -> download (every version) -> extract -> references -> storage
and each paper ends with one "paper" event holding its total processing time.
A failing paper is logged and the run continues with the next one.

extract / references / storage are optional per-paper hooks and metrics is an optional
resource sampler (see PAPER_STAGES and start_metrics). A module that does not define its
function yet is logged as "skipped". Contract: docs/handover-baseline-discovery-download.md

    python -m src.main [path/to/config.yaml]
"""

import importlib
import os
import pathlib
import platform
import sys
from importlib import metadata

from .common import FAILED, OK, SKIPPED, EventLog, Stopwatch, load_ids, read_json, utc_now_iso, write_json_atomic
from .config import load_config
from .discovery.arxiv_api import discover_paper, make_clients
from .downloader.http import download_paper

# One hook per paper, each defined by the module's owner:  func(cfg, arxiv_id, events)
PAPER_STAGES = (
    ("extract", ".extract", "extract_paper"),
    ("references", ".references", "references_paper"),
    ("storage", ".storage", "store_paper"),
)

def _package_version(name: str):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None

def build_run_record(cfg, run_dir, input_ids):
    """Content of run.json at start-up. Keeps the first start time when resuming."""
    previous = read_json(run_dir / "run.json")
    record = {k: str(v) if isinstance(v, pathlib.Path) else v for k, v in cfg.items()}
    started = utc_now_iso()
    first_started = started
    if previous:
        first_started = previous.get("first_started_at") or previous.get("started_at") or started
    record.update({
        "started_at": started,
        "first_started_at": first_started,
        "finished_at": None,
        "run_status": "running",
        "cache_mode": "warm" if previous else "cold",
        "resumed": previous is not None,
        "input_ids": input_ids,
        "num_input_ids": len(input_ids),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "arxiv": _package_version("arxiv"),
            "requests": _package_version("requests"),
            "PyYAML": _package_version("PyYAML"),
        },
    })
    return record

def finish_run(run_dir, record, status, wall):
    record["finished_at"] = utc_now_iso()
    record["run_status"] = status
    record["wall_seconds"] = round(wall.elapsed, 3)
    write_json_atomic(run_dir / "run.json", record)

def start_metrics(cfg, run_dir, events):
    """Optional resource sampler (Hien). Contract: metrics.start_sampler(cfg, run_dir) -> object with .stop()."""
    module = importlib.import_module(".metrics", package=__package__)
    start = getattr(module, "start_sampler", None)
    if start is None:
        events.log("metrics", SKIPPED, message="src.metrics.start_sampler not implemented yet")
        print("metrics: skipped (not implemented yet)")
        return None
    return start(cfg, run_dir)

def load_paper_hooks(events):
    """Return [(stage, func)] for the stage hooks that exist. Missing ones are logged once."""
    hooks = []
    for stage, module_name, func_name in PAPER_STAGES:
        module = importlib.import_module(module_name, package=__package__)
        func = getattr(module, func_name, None)
        if func is None:
            events.log(stage, SKIPPED, message=f"{module_name}.{func_name} not implemented yet")
            print(f"{stage}: skipped (not implemented yet)")
        else:
            hooks.append((stage, func))
    return hooks

def process_paper(cfg, arxiv_id, client, http, throttle, hooks, events) -> str:
    """Run the whole pipeline for one paper and log one 'paper' event with its total time."""
    watch = Stopwatch()
    d_status = discover_paper(cfg, arxiv_id, client, http, throttle, events)

    counts = {OK: 0, FAILED: 0, SKIPPED: 0}
    stage_failed = False
    if d_status != FAILED:
        counts = download_paper(cfg, arxiv_id, http, throttle, events)
        for stage, func in hooks:
            stage_watch = Stopwatch()
            try:
                func(cfg, arxiv_id, events)
            except Exception as exc:  # noqa: BLE001 - one paper must never stop the run
                stage_failed = True
                events.log(
                    stage, FAILED, arxiv_id=arxiv_id, error_code=type(exc).__name__,
                    message=str(exc)[:300], elapsed_seconds=stage_watch.elapsed,
                )

    if d_status == FAILED or counts[FAILED] or stage_failed:
        status = FAILED
    elif d_status == SKIPPED and counts[OK] == 0 and not hooks:
        status = SKIPPED
    else:
        status = OK
    events.log(
        "paper", status, arxiv_id=arxiv_id, elapsed_seconds=watch.elapsed,
        message=f"discovery={d_status} versions ok/failed/skipped="
                f"{counts[OK]}/{counts[FAILED]}/{counts[SKIPPED]}",
    )
    return status

def main() -> None:
    cfg = load_config(pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else None)
    run_dir = cfg["output_root"] / cfg["run_id"]

    record = build_run_record(cfg, run_dir, load_ids(cfg["ids_file"]))
    write_json_atomic(run_dir / "run.json", record)

    events = EventLog(run_dir / "events.jsonl", cfg["run_id"])
    wall = Stopwatch()
    events.log("pipeline", "running", message=f"start, cache_mode={record['cache_mode']}")
    print(f"run_id={cfg['run_id']} cache_mode={record['cache_mode']}")

    status = "completed"
    sampler = None
    try:
        sampler = start_metrics(cfg, run_dir, events)
        hooks = load_paper_hooks(events)
        client, http, throttle = make_clients(cfg)  # one Throttle shared by discovery and download
        ids = record["input_ids"]
        counts = {OK: 0, FAILED: 0, SKIPPED: 0}
        for index, arxiv_id in enumerate(ids, start=1):
            paper_status = process_paper(cfg, arxiv_id, client, http, throttle, hooks, events)
            counts[paper_status] += 1
            print(f"[{index}/{len(ids)}] {arxiv_id}: {paper_status}")
        print("papers:", counts)
    except KeyboardInterrupt:
        status = "interrupted"
        events.log("pipeline", FAILED, error_code="INTERRUPTED",
                   message="stopped by user (Ctrl+C)", elapsed_seconds=wall.elapsed)
        print("interrupted; run again with the same run_id to resume")
    except Exception as exc:  # noqa: BLE001 - record the crash, then re-raise
        status = "crashed"
        events.log("pipeline", FAILED, error_code=type(exc).__name__,
                   message=str(exc)[:300], elapsed_seconds=wall.elapsed)
        raise
    else:
        events.log("pipeline", "ok", elapsed_seconds=wall.elapsed,
                   message=f"end, papers ok/failed/skipped={counts[OK]}/{counts[FAILED]}/{counts[SKIPPED]}")
        print(f"pipeline wall time: {wall.elapsed:.1f}s")
    finally:
        if sampler is not None:
            try:
                sampler.stop()
            except Exception as exc:  # noqa: BLE001 - a broken sampler must not hide the run result
                events.log("metrics", FAILED, error_code=type(exc).__name__, message=str(exc)[:300])
        finish_run(run_dir, record, status, wall)

if __name__ == "__main__":
    main()
