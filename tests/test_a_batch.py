"""Offline tests for A + Batch discovery and per-paper source/reference lanes."""
import json
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from src import main
from src.discovery import arxiv_api, batch
from src.utils.common import EventLogger, read_json


def fake_result(arxiv_id, version):
    date = datetime(2025, 7, 1, tzinfo=timezone.utc)
    return SimpleNamespace(
        get_short_id=lambda: f"{arxiv_id}v{version}",
        title=f"Title {arxiv_id}", authors=[SimpleNamespace(name="Test Author")],
        published=date, updated=date, journal_ref=None,
    )


def test_batch_discovery_groups_ids_and_keeps_versions(tmp_path, monkeypatch):
    cfg = {"output_root": tmp_path, "run_id": "batch"}
    logger = EventLogger(tmp_path / "batch" / "events.jsonl", "batch")
    ids = ["2507.01659", "2507.01660", "2507.01661"]
    queries = []

    def query(_client, _throttle, requested):
        queries.append(list(requested))
        if all("v" not in x for x in requested):
            # Reverse response order to verify ID-keyed mapping.
            return [fake_result(x, 2 if x == ids[0] else 1)
                    for x in reversed(requested)]
        return []

    monkeypatch.setattr(arxiv_api, "_query", query)
    outcomes = batch.discover_batches(cfg, ids, None, {}, None, logger, batch_size=2)

    assert outcomes == {item: "ok" for item in ids}
    assert queries == [ids[:2], ids[2:]]
    assert read_json(arxiv_api.paper_dir(cfg, ids[0]) / "metadata.json")["versions"] == [1, 2]
    assert read_json(arxiv_api.paper_dir(cfg, ids[1]) / "metadata.json")["versions"] == [1]
    queries.clear()
    outcomes = batch.discover_batches(cfg, ids, None, {}, None, logger, batch_size=2)
    assert all(status == "skipped" for status in outcomes.values())
    assert not queries


def test_missing_batch_id_uses_single_paper_fallback(tmp_path, monkeypatch):
    cfg = {"output_root": tmp_path, "run_id": "batch"}
    logger = EventLogger(tmp_path / "batch" / "events.jsonl", "batch")
    ids = ["2507.01659", "2507.01660"]
    calls = []
    monkeypatch.setattr(arxiv_api, "_query", lambda client, throttle, ids: [fake_result(ids[0], 1)])
    def fallback(cfg, arxiv_id, client, opts, throttle, logger, metrics_logger=None):
        calls.append(arxiv_id)
        return "failed"
    monkeypatch.setattr(arxiv_api, "discover_paper", fallback)
    outcomes = batch.discover_batches(cfg, ids, None, {}, None, logger, batch_size=2)
    assert outcomes == {ids[0]: "ok", ids[1]: "failed"}
    assert calls == [ids[1]]


def test_revisions_are_batched_across_papers(tmp_path, monkeypatch):
    cfg = {"output_root": tmp_path, "run_id": "batch"}
    logger = EventLogger(tmp_path / "batch" / "events.jsonl", "batch")
    ids = ["2507.01659", "2507.01660"]
    queries = []
    def query(_client, _throttle, requested):
        queries.append(list(requested))
        if all("v" not in x for x in requested):
            return [fake_result(item, 3) for item in requested]
        return [fake_result(item[:-2], 2) for item in requested]
    monkeypatch.setattr(arxiv_api, "_query", query)
    statuses = batch.discover_batches(cfg, ids, None, {}, None, logger, batch_size=20)
    assert statuses == {x: "ok" for x in ids}
    assert queries == [ids, [f"{x}v2" for x in ids]]
    for arxiv_id in ids:
        assert len(read_json(arxiv_api.paper_dir(cfg, arxiv_id) / "metadata.json")["revised_dates"]) == 2


def test_two_lanes_overlap_and_storage_runs_after_both(tmp_path, monkeypatch):
    cfg = {"output_root": tmp_path, "run_id": "batch", "config_name": "A_BATCH"}
    logger = EventLogger(tmp_path / "batch" / "events.jsonl", "batch")
    started = threading.Event()
    finished = threading.Event()
    observed = []

    def download(cfg, arxiv_id, opts, logger, metrics_logger):
        assert started.wait(timeout=2), "references did not start during download"
        observed.append("download")
        return {"ok": 1, "failed": 0, "skipped": 0}

    def references(cfg, arxiv_id, logger):
        started.set()
        time.sleep(0.02)
        observed.append("references")
        finished.set()

    def extract(cfg, arxiv_id, logger):
        observed.append("extract")

    def storage(cfg, arxiv_id, logger):
        assert finished.is_set()
        observed.append("storage")

    monkeypatch.setattr(main, "download_paper", download)
    hooks = [("extract", extract), ("references", references), ("storage", storage)]
    result = main.process_paper(cfg, "2507.01659", None, {}, None, hooks, logger,
                                discovery_status="ok")
    assert result == "ok"
    assert observed.index("download") < observed.index("extract")
    assert observed[-1] == "storage"


def test_failed_discovery_skips_every_lane(tmp_path, monkeypatch):
    cfg = {"output_root": tmp_path, "run_id": "batch", "config_name": "A_BATCH"}
    logger = EventLogger(tmp_path / "batch" / "events.jsonl", "batch")
    monkeypatch.setattr(main, "download_paper", lambda *args: 1 / 0)
    assert main.process_paper(cfg, "2507.01659", None, {}, None, [], logger,
                              discovery_status="failed") == "failed"
