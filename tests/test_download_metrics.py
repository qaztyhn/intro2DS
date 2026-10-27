"""Download stage metrics: one operation per downloaded version, one request hook per version.

Offline: utils.http.download_file is replaced by a fake, no arXiv request is made.
"""

import io
import tarfile

import pytest

from src import metrics
from src.common import EventLogger, read_json, write_json_atomic
from src.downloader import http as downloader
from src.utils.errors import NOT_FOUND, CrawlError

ARXIV_ID = "2507.01659"
KEY = "2507-01659"


def tar_gz_bytes() -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        member = tarfile.TarInfo("main.tex")
        member.size = 3
        archive.addfile(member, io.BytesIO(b"tex"))
    return stream.getvalue()


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = {"output_root": tmp_path, "run_id": "offline"}
    run = tmp_path / "offline"
    write_json_atomic(
        run / "papers" / KEY / "metadata.json",
        {"arxiv_id": ARXIV_ID, "status": "ok", "versions": [1, 2, 3]},
    )
    logger = EventLogger(run / "events.jsonl", "offline")
    metrics_logger = metrics.EventLogger(run / "metrics_events.jsonl", "offline")
    received = []  # on_attempt seen by the fake, per call

    def install(script):
        """script: {version: [(http_status, error_code), ...]}; last entry decides the outcome."""

        def fake_download_file(url, target, on_attempt=None, **kwargs):
            version = int(url.rsplit("v", 1)[1])
            received.append((version, on_attempt))
            steps = script[version]
            for number, (status, code) in enumerate(steps, start=1):
                if on_attempt is not None:
                    on_attempt(attempt=number, http_status=status, error_code=code,
                               duration=0.01, error_message=None)
            last_status, last_code = steps[-1]
            if last_code is not None:
                raise CrawlError("boom", last_code, last_status)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar_gz_bytes())

        monkeypatch.setattr(downloader, "download_file", fake_download_file)

    def events():
        return metrics.read_events(run / "metrics_events.jsonl")

    opts = {"headers": {}, "timeout": 5, "max_retries": 2, "base_delay": 0,
            "throttle": None, "on_attempt": None}
    return cfg, run, logger, metrics_logger, opts, install, events, received


def test_each_downloaded_version_is_one_measured_operation(env):
    cfg, _, logger, metrics_logger, opts, install, events, _ = env
    install({1: [(200, None)], 2: [(200, None)], 3: [(200, None)]})

    counts = downloader.download_paper(cfg, ARXIV_ID, opts, logger, metrics_logger)

    assert counts == {"ok": 3, "failed": 0, "skipped": 0}
    summary = metrics.summarize_stage_times(events(), "offline", "download")
    assert summary["started_operation_count"] == 3
    assert summary["successful_operation_count"] == 3
    assert summary["unfinished_operation_count"] == 0
    versions = sorted(e["version"] for e in events()
                      if e["stage"] == "download" and e["event"] == "finished")
    assert versions == [1, 2, 3]
    assert all(e["arxiv_id"] == ARXIV_ID for e in events() if e["stage"] == "download")


def test_skipped_version_is_not_measured(env):
    cfg, run, logger, metrics_logger, opts, install, events, received = env
    write_json_atomic(run / "papers" / KEY / "versions" / "v2" / "manifest.json",
                      {"download_status": "ok"})
    install({1: [(200, None)], 3: [(200, None)]})

    counts = downloader.download_paper(cfg, ARXIV_ID, opts, logger, metrics_logger)

    assert counts == {"ok": 2, "failed": 0, "skipped": 1}
    assert [v for v, _ in received] == [1, 3]
    summary = metrics.summarize_stage_times(events(), "offline", "download")
    assert summary["started_operation_count"] == 2
    assert 2 not in {e.get("version") for e in events()}


def test_failed_version_is_logged_failed_and_the_next_one_still_runs(env):
    cfg, run, logger, metrics_logger, opts, install, events, _ = env
    install({1: [(200, None)], 2: [(404, NOT_FOUND)], 3: [(200, None)]})

    counts = downloader.download_paper(cfg, ARXIV_ID, opts, logger, metrics_logger)

    assert counts == {"ok": 2, "failed": 1, "skipped": 0}
    summary = metrics.summarize_stage_times(events(), "offline", "download")
    assert summary["successful_operation_count"] == 2
    assert summary["failed_operation_count"] == 1
    failed = [e for e in events()
              if e["stage"] == "download" and e["event"] == "finished" and e["status"] == "failed"]
    assert [e["version"] for e in failed] == [2]
    assert failed[0]["error_type"] == "CrawlError"
    manifest = read_json(run / "papers" / KEY / "versions" / "v2" / "manifest.json")
    assert manifest["download_status"] == "failed"
    assert manifest["error_code"] == NOT_FOUND


def test_unexpected_exception_is_measured_as_failed_too(env, monkeypatch):
    cfg, _, logger, metrics_logger, opts, _, events, _ = env

    def broken(url, target, on_attempt=None, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(downloader, "download_file", broken)

    counts = downloader.download_paper(cfg, ARXIV_ID, opts, logger, metrics_logger)

    assert counts == {"ok": 0, "failed": 3, "skipped": 0}
    summary = metrics.summarize_stage_times(events(), "offline", "download")
    assert summary["failed_operation_count"] == 3


def test_every_version_has_its_own_request_id_and_attempts(env):
    cfg, _, logger, metrics_logger, opts, install, events, _ = env
    install({1: [(503, "HTTP_5XX"), (200, None)], 2: [(200, None)], 3: [(429, "HTTP_429"), (200, None)]})

    downloader.download_paper(cfg, ARXIV_ID, opts, logger, metrics_logger)

    # would raise "Duplicate request attempt" if a hook were shared between versions
    requests = metrics.summarize_request_attempts(events(), "offline")
    assert requests["observed_request_count"] == 3
    assert requests["observed_attempt_count"] == 5
    assert requests["observed_retry_attempt_count"] == 2
    assert requests["error_code_counts"] == {"HTTP_5XX": 1, "HTTP_429": 1}
    assert requests["requests_with_missing_attempts"] == 0
    attempts = [e for e in events() if e["event"] == "request_attempt_finished"]
    assert {e["stage"] for e in attempts} == {"download"}
    assert {e["version"] for e in attempts} == {1, 2, 3}


def test_shared_opts_are_not_modified(env):
    cfg, _, logger, metrics_logger, opts, install, _, received = env
    install({1: [(200, None)], 2: [(200, None)], 3: [(200, None)]})

    downloader.download_paper(cfg, ARXIV_ID, opts, logger, metrics_logger)

    assert opts["on_attempt"] is None
    assert all(hook is not None for _, hook in received)
    assert len({id(hook) for _, hook in received}) == 3


def test_without_metrics_logger_nothing_is_measured(env):
    cfg, run, logger, _, opts, install, _, received = env
    install({1: [(200, None)], 2: [(200, None)], 3: [(200, None)]})

    counts = downloader.download_paper(cfg, ARXIV_ID, opts, logger)

    assert counts == {"ok": 3, "failed": 0, "skipped": 0}
    assert all(hook is None for _, hook in received)
    assert not (run / "metrics_events.jsonl").exists()