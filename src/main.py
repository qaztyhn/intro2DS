"""B/D1 pipeline driver. One run = one run_id = one events.jsonl."""

import importlib
import os
import pathlib
import platform
import sys
from concurrent.futures import ThreadPoolExecutor
from importlib import metadata

from .utils.common import FAILED, OK, SKIPPED, EventLogger, Stopwatch, load_ids, paper_key, read_json, utc_now_iso, write_json_atomic
from .config import http_options, load_config
from .discovery.arxiv_api import discover_paper, make_clients
from .discovery.batch import discover_batches
from .pipeline import run_bounded_pipeline
from .discovery.oai_pmh import discover_paper as d1_discover_paper
from .downloader.http import download_paper
from .stages.extract import extract_paper
from .stages.references import crawl_references
from .stages.storage import store_paper
from . import metrics

PAPER_STAGES = ( "extract", "references", "storage")

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

def finish_run(run_dir, record, status, wall, elapsed=None):
    record["finished_at"] = utc_now_iso()
    record["run_status"] = status
    record["wall_seconds"] = round(wall.elapsed if elapsed is None else elapsed, 3)
    write_json_atomic(run_dir / "run.json", record)

def make_references_hook(metrics_logger):
    """Build the references stage hook: func(cfg, arxiv_id, logger)."""

    def references_paper(cfg, arxiv_id, logger):
        run_dir = cfg["output_root"] / cfg["run_id"]
        path = run_dir / "papers" / paper_key(arxiv_id) / "references.json"
        existing = read_json(path)
        if existing and existing.get("status") in (OK, "empty"):
            logger.log("references", SKIPPED, arxiv_id=arxiv_id, message="already fetched")
            return

        watch = Stopwatch()
        on_attempt = metrics.make_request_hook(metrics_logger, "references", arxiv_id=arxiv_id)
        opts = http_options(cfg, None, on_attempt=on_attempt)
        opts.pop("throttle")
        opts["max_retries"] = cfg.get("s2_max_retries", opts["max_retries"])
        opts["base_delay"] = cfg.get("s2_backoff_base_seconds", opts["base_delay"]) 
        with metrics.measure_stage(metrics_logger, "references", arxiv_id=arxiv_id):
            result = crawl_references(arxiv_id, **opts)

        write_json_atomic(path, result)
        logger.log(
            "references", result["status"], arxiv_id=arxiv_id, error_code=result["error_code"],
            elapsed_seconds=watch.elapsed, message=f"{len(result['references'])} references",
        )

    return references_paper

def make_extract_hook(metrics_logger):
    """Build the extract stage hook: func(cfg, arxiv_id, logger). One measured operation per version."""

    def extract_hook(cfg, arxiv_id, logger):
        extract_paper(cfg, arxiv_id, logger, metrics_logger=metrics_logger)

    return extract_hook

def make_storage_hook(metrics_logger):
    """Build the storage stage hook: func(cfg, arxiv_id, logger). One measured operation per paper."""

    def storage_hook(cfg, arxiv_id, logger):
        with metrics.measure_stage(metrics_logger, "storage", arxiv_id=arxiv_id):
            store_paper(cfg, arxiv_id, logger)

    return storage_hook

def load_paper_hooks(logger, metrics_logger):
    """Return [(stage, func)] in PAPER_STAGES order. All stages are implemented, none is skipped."""
    factories = {
        "extract": make_extract_hook,
        "references": make_references_hook,
        "storage": make_storage_hook,
    }
    return [(stage, factories[stage](metrics_logger)) for stage in PAPER_STAGES]

def process_paper(cfg, arxiv_id, client, opts, throttle, hooks, logger, metrics_logger=None, discovery_status=None) -> str:
    """Run the whole pipeline for one paper and log one 'paper' event with its total time."""
    watch = Stopwatch()
    # Keep the baseline symbol patchable for existing callers and offline tests.
    discover = d1_discover_paper if cfg.get("config_name", "B") == "D1" else discover_paper
    d_status = (discover(cfg, arxiv_id, client, opts, throttle, logger, metrics_logger)
                if discovery_status is None else discovery_status)

    counts = {OK: 0, FAILED: 0, SKIPPED: 0}
    stage_failed = False
    stage_errors = []
    if d_status != FAILED:
        def run_stage(stage, func):
            stage_watch = Stopwatch()
            try:
                func(cfg, arxiv_id, logger)
                return None
            except Exception as exc:  # one stage must not stop the remaining stages
                logger.log(
                    stage, FAILED, arxiv_id=arxiv_id, error_code=type(exc).__name__,
                    message=str(exc)[:300], elapsed_seconds=stage_watch.elapsed,
                )
                return type(exc).__name__

        stage_map = dict(hooks)
        if cfg.get("config_name") == "A_BATCH":
            # References needs only the base paper ID. arXiv source requests
            # remain sequential and share their existing throttle.
            # The executor always joins before storage/validation.
            with ThreadPoolExecutor(max_workers=2) as pool:
                ref_future = (pool.submit(run_stage, "references", stage_map["references"])
                              if "references" in stage_map else None)
                counts = download_paper(cfg, arxiv_id, opts, logger, metrics_logger)
                if "extract" in stage_map:
                    error = run_stage("extract", stage_map["extract"])
                    if error:
                        stage_errors.append(error)
                if ref_future is not None:
                    error = ref_future.result()
                    if error:
                        stage_errors.append(error)
            if "storage" in stage_map:
                error = run_stage("storage", stage_map["storage"])
                if error:
                    stage_errors.append(error)
        else:
            counts = download_paper(cfg, arxiv_id, opts, logger, metrics_logger)
            for stage, func in hooks:
                error = run_stage(stage, func)
                if error:
                    stage_errors.append(error)
        stage_failed = bool(stage_errors)

    if d_status == FAILED or counts[FAILED] or stage_failed:
        status = FAILED
    elif d_status == SKIPPED and counts[OK] == 0 and not hooks:
        status = SKIPPED
    else:
        status = OK
    logger.log(
        "paper", status, arxiv_id=arxiv_id, elapsed_seconds=watch.elapsed,
        error_code=(root_error_code(cfg, arxiv_id, d_status, counts, stage_errors)
                    if status == FAILED else None),
        message=f"discovery={d_status} versions ok/failed/skipped="
                f"{counts[OK]}/{counts[FAILED]}/{counts[SKIPPED]}",
    )
    return status

def root_error_code(cfg, arxiv_id, d_status, counts, stage_errors):
    """Root cause of a failed paper: discovery, then download/extract, then references.

    Storage only reports SCHEMA_ERROR when an earlier stage left incomplete data,
    so the paper event records the earlier stage's code instead (e.g. HTTP_429).
    """
    paper_dir = cfg["output_root"] / cfg["run_id"] / "papers" / paper_key(arxiv_id)
    if d_status == FAILED:
        return (read_json(paper_dir / "metadata.json") or {}).get("error_code")
    for path in sorted((paper_dir / "versions").glob("v*/manifest.json")):
        manifest = read_json(path) or {}
        if FAILED in (manifest.get("download_status"), manifest.get("extract_status")) and manifest.get("error_code"):
            return manifest["error_code"]
    references = read_json(paper_dir / "references.json") or {}
    if references.get("status") == FAILED and references.get("error_code"):
        return references["error_code"]
    return stage_errors[0] if stage_errors else None

def main() -> None:
    cfg = load_config(pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else None)
    run_dir = cfg["output_root"] / cfg["run_id"]

    record = build_run_record(cfg, run_dir, load_ids(cfg["ids_file"]))
    write_json_atomic(run_dir / "run.json", record)

    logger = EventLogger(run_dir / "events.jsonl", cfg["run_id"])
    metrics_logger = metrics.EventLogger(run_dir / "metrics_events.jsonl", cfg["run_id"])
    wall = Stopwatch()
    logger.log("pipeline", "running", message=f"start, cache_mode={record['cache_mode']}")
    print(f"run_id={cfg['run_id']} cache_mode={record['cache_mode']}")

    status = "completed"
    monitor = None
    try:
        hooks = load_paper_hooks(logger, metrics_logger)
        client, opts, throttle = make_clients(cfg)  # one Throttle shared by discovery and download
        ids = record["input_ids"]
        counts = {OK: 0, FAILED: 0, SKIPPED: 0}
        with metrics.ResourceMonitor(run_dir) as monitor:
            if cfg.get("config_name") == "A_PIPELINE":
                counts = run_bounded_pipeline(
                    cfg, ids, client, opts, throttle, hooks, logger,
                    metrics_logger, root_error_code,
                )
            discovery_statuses = (
                discover_batches(
                    cfg, ids, client, opts, throttle, logger, metrics_logger,
                    batch_size=cfg.get("discovery_batch_size", 20),
                ) if cfg.get("config_name") == "A_BATCH" else None
            )
            for index, arxiv_id in enumerate(([] if cfg.get("config_name") == "A_PIPELINE" else ids), start=1):
                paper_status = process_paper(
                    cfg, arxiv_id, client, opts, throttle, hooks, logger, metrics_logger,
                    discovery_status=(discovery_statuses[arxiv_id] if discovery_statuses is not None else None),
                )
                counts[paper_status] += 1
                print(f"[{index}/{len(ids)}] {arxiv_id}: {paper_status}")
            if cfg.get("config_name") != "A_PIPELINE":
                print("papers:", counts)
    except KeyboardInterrupt:
        status = "interrupted"
        logger.log("pipeline", FAILED, error_code="INTERRUPTED",
                   message="stopped by user (Ctrl+C)", elapsed_seconds=wall.elapsed)
        print("interrupted; run again with the same run_id to resume")
    except Exception as exc:  # noqa: BLE001 - record the crash, then re-raise
        status = "crashed"
        logger.log("pipeline", FAILED, error_code=type(exc).__name__,
                   message=str(exc)[:300], elapsed_seconds=wall.elapsed)
        raise
    else:
        logger.log("pipeline", "ok", elapsed_seconds=wall.elapsed,
                   message=f"end, papers ok/failed/skipped={counts[OK]}/{counts[FAILED]}/{counts[SKIPPED]}")
        print(f"pipeline wall time: {wall.elapsed:.1f}s")
    finally:
        elapsed = wall.elapsed
        if monitor is not None:
            try:
                (run_dir / "papers").mkdir(parents=True, exist_ok=True)
                metrics.log_resource_results(metrics_logger, monitor.sampler, run_dir / "papers")
            except Exception as exc:  
                logger.log("metrics", FAILED, error_code=type(exc).__name__, message=str(exc)[:300])
        finish_run(run_dir, record, status, wall, elapsed)

if __name__ == "__main__":
    main()
