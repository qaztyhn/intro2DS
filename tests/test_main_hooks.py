"""The driver wires extract, references and storage; none is skipped."""

import pytest

from src import main, metrics
from src.common import EventLogger
from src.utils.errors import CrawlError


def test_all_paper_stages_are_wired_in_order(tmp_path):
    metrics_logger = metrics.EventLogger(tmp_path / "metrics_events.jsonl", "offline")
    hooks = main.load_paper_hooks(None, metrics_logger)
    assert [stage for stage, _ in hooks] == ["extract", "references", "storage"]
    assert all(callable(func) for _, func in hooks)


def test_storage_hook_is_measured_even_when_validation_fails(tmp_path):
    cfg = {"output_root": tmp_path, "run_id": "offline"}
    logger = EventLogger(tmp_path / "events.jsonl", "offline")
    metrics_logger = metrics.EventLogger(tmp_path / "metrics_events.jsonl", "offline")
    storage = dict(main.load_paper_hooks(logger, metrics_logger))["storage"]
    with pytest.raises(CrawlError):  # no corpus on disk, so validation reports failure
        storage(cfg, "2507.01659", logger)
    events = metrics.read_events(tmp_path / "metrics_events.jsonl")
    finished = [e for e in events if e["stage"] == "storage" and e["event"] == "finished"]
    assert len(finished) == 1
    assert finished[0]["status"] == "failed"
    assert finished[0]["arxiv_id"] == "2507.01659"



def test_process_paper_passes_metrics_logger_to_discovery_and_download(tmp_path, monkeypatch):
    cfg = {"output_root": tmp_path, "run_id": "offline"}
    logger = EventLogger(tmp_path / "events.jsonl", "offline")
    metrics_logger = metrics.EventLogger(tmp_path / "metrics_events.jsonl", "offline")
    received = {}

    def fake_discover(cfg, arxiv_id, client, opts, throttle, logger, metrics_logger=None):
        received["discovery"] = metrics_logger
        return "ok"

    def fake_download(cfg, arxiv_id, opts, logger, metrics_logger=None):
        received["download"] = metrics_logger
        return {"ok": 1, "failed": 0, "skipped": 0}

    monkeypatch.setattr(main, "discover_paper", fake_discover)
    monkeypatch.setattr(main, "download_paper", fake_download)

    status = main.process_paper(cfg, "2507.01659", None, {}, None, [], logger, metrics_logger)

    assert status == "ok"
    assert received["discovery"] is metrics_logger
    assert received["download"] is metrics_logger