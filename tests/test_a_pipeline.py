"""Offline tests for bounded A pipeline: overlap, validation join and backpressure."""
import threading
import time

from src import pipeline
from src.utils.common import EventLogger


def test_pipeline_overlaps_papers_and_stores_after_both(tmp_path, monkeypatch):
    cfg = {
        "output_root": tmp_path, "run_id": "test", "config_name": "A_PIPELINE",
        "discovery_batch_size": 2, "pipeline_max_inflight": 2,
        "pipeline_extract_queue": 1, "pipeline_references_queue": 1,
        "pipeline_prefetch_remaining": 1,
    }
    ids = ["2507.01659", "2507.01660", "2507.01661"]
    logger = EventLogger(tmp_path / "test" / "events.jsonl", "test")
    discovered = []
    downloaded = []
    extracted = set()
    referenced = set()
    stored = []
    lock = threading.Lock()
    during_download = threading.Event()

    def discover(cfg, items, *args, **kwargs):
        discovered.append(list(items))
        return {pid: "ok" for pid in items}

    def download(cfg, pid, *args):
        downloaded.append(pid)
        if pid == ids[0]:
            assert during_download.wait(2), "reference worker never overlapped download"
        return {"ok": 1, "failed": 0, "skipped": 0}

    def extract(cfg, pid, logger):
        with lock:
            extracted.add(pid)

    def refs(cfg, pid, logger):
        during_download.set()
        with lock:
            referenced.add(pid)

    def store(cfg, pid, logger):
        with lock:
            assert pid in extracted and pid in referenced
            stored.append(pid)

    monkeypatch.setattr(pipeline, "discover_batches", discover)
    monkeypatch.setattr("src.downloader.http.download_paper", download)
    result = pipeline.run_bounded_pipeline(
        cfg, ids, None, {}, None,
        [("extract", extract), ("references", refs), ("storage", store)],
        logger, None, lambda *args: None,
    )
    assert result == {"ok": 3, "failed": 0, "skipped": 0}
    assert set(stored) == set(ids)
    assert discovered == [ids[:2], ids[2:]]
    assert downloaded == ids


def test_discovery_failure_does_not_enter_workers(tmp_path, monkeypatch):
    cfg = {"output_root": tmp_path, "run_id": "test",
           "discovery_batch_size": 2, "pipeline_prefetch_remaining": 0}
    ids = ["2507.01659", "2507.01660"]
    logger = EventLogger(tmp_path / "test" / "events.jsonl", "test")
    monkeypatch.setattr(
        pipeline, "discover_batches",
        lambda cfg, ids, *args, **kwargs:
            {ids[0]: "failed", ids[1]: "ok"},
    )
    monkeypatch.setattr(
        "src.downloader.http.download_paper",
        lambda *args: {"ok": 1, "failed": 0, "skipped": 0},
    )
    processed = []
    hooks = [(stage, lambda cfg, pid, logger: processed.append((stage, pid)))
             for stage in ("extract", "references", "storage")]
    counts = pipeline.run_bounded_pipeline(
        cfg, ids, None, {}, None, hooks, logger, None,
        lambda *args: "HTTP_429",
    )
    assert counts == {"ok": 1, "failed": 1, "skipped": 0}
    assert all(pid == ids[1] for _, pid in processed)


def test_bad_bounds_rejected(tmp_path):
    cfg = {"discovery_batch_size": 20, "pipeline_max_inflight": 0}
    try:
        pipeline.run_bounded_pipeline(cfg, [], None, {}, None, [], None, None, None)
    except ValueError as error:
        assert "pipeline_max_inflight" in str(error)
    else:
        raise AssertionError("invalid pipeline bounds must fail")
