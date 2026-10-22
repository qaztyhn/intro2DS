"""Baseline driver (minimal): discovery -> download for one run, one shared run_id and
one events.jsonl. Extract / references / metrics will be plugged in here once the
other modules exist.

    python -m src.main [path/to/config.yaml]
"""

import importlib
import pathlib
import sys

from .common import EventLog, Stopwatch, write_json_atomic
from .config import load_config
from .discovery.arxiv_api import run_discovery
from .downloader.http import run_download

OPTIONAL_STAGES = (
    ("extract", "src.extract", "run_extract"),
    ("references", "src.references", "run_references"),
)

def main() -> None:
    cfg = load_config(pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else None)
    run_dir = cfg["output_root"] / cfg["run_id"]
    write_json_atomic(run_dir / "run.json", {k: str(v) if isinstance(v, pathlib.Path) else v for k, v in cfg.items()})
    
    events = EventLog(run_dir / "events.jsonl", cfg["run_id"])
    wall = Stopwatch();
    events.log("pineline", "running", message="start")
    print(f"run_id={cfg['run_id']}")

    watch = Stopwatch()
    print("discovery:", run_discovery(cfg, events))
    events.log("discovery_total", "ok", elapsed_seconds=watch.elapsed)

    watch = Stopwatch()
    print("download (per version):", run_download(cfg, events))
    events.log("download_total", "ok", elapsed_seconds=watch.elapsed)

    for stage, module_name, func_name in OPTIONAL_STAGES:
        func = getattr(importlib.import_module(module_name), func_name, None)
        if func is None:
            events.log(stage, "skipped", message=f"{module_name}.{func_name} not implemented yet")
            print(f"{stage}: skipped (not implemented yet)")
            continue
        watch = Stopwatch()
        print(f"{stage}:", func(cfg, events))
        events.log(f"{stage}_total", "ok", elapsed_seconds=watch.elapsed)

    events.log("pipeline", "ok", message="end", elapsed_seconds=wall.elapsed)
    print(f"pipeline wall time: {wall.elapsed:.1f}s")

if __name__ == "__main__":
    main()
