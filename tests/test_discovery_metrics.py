"""Discovery stage metrics: one operation per discovered paper, one request hook per paper.

Offline: arxiv_api._query (arxiv package) and arxiv_api.request_text (abs page) are replaced
by fakes, so no arXiv request is made.
"""

import re
from collections import namedtuple
from datetime import datetime, timezone

import pytest

from src import metrics
from src.common import EventLogger, read_json
from src.discovery import arxiv_api as discovery
from src.utils.errors import HTTP_5XX, NETWORK_ERROR, CrawlError

Author = namedtuple("Author", "name")
HISTORY = (
    '<div class="submission-history">'
    "<strong>[v1]</strong> Mon, 1 Jan 2025 00:00:00 UTC (1 KB)<br/>"
    "<strong>[v2]</strong> Tue, 2 Jan 2025 00:00:00 UTC (1 KB)</div>"
)
NEW_V1 = "2507.01659"        # new-style id, 1 version: no extra request, revised_dates == []
NEW_V2 = "2507.01660"        # new-style id, 2 versions: dates come with the first API answer
NEW_V3 = "2507.01661"        # new-style id, 3 versions: needs a second query, then the abs page
OLD_A = "hep-th/9901001"     # old-style ids: dates come from the abs page only
OLD_B = "hep-th/9901002"
LATEST = {NEW_V1: 1, NEW_V2: 2, NEW_V3: 3, OLD_A: 2, OLD_B: 2}


class FakeResult:
    def __init__(self, short_id):
        self._short_id = short_id
        self.title = "A  title"
        self.authors = [Author("An")]
        self.published = datetime(2025, 1, 1, tzinfo=timezone.utc)
        self.updated = datetime(2025, 1, 2, tzinfo=timezone.utc)
        self.journal_ref = None

    def get_short_id(self):
        return self._short_id


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = {"output_root": tmp_path, "run_id": "offline"}
    run = tmp_path / "offline"
    logger = EventLogger(run / "events.jsonl", "offline")
    metrics_logger = metrics.EventLogger(run / "metrics_events.jsonl", "offline")
    opts = {"headers": {}, "timeout": 5, "max_retries": 2, "base_delay": 0,
            "throttle": None, "on_attempt": None}
    state = {"queries": [], "abs_calls": [], "query_error": None, "abs_attempts": [(200, None)]}

    def fake_query(client, throttle, ids):
        state["queries"].append(list(ids))
        if state["query_error"] is not None and len(state["queries"]) > 1:
            raise state["query_error"]
        found = []
        for item in ids:
            if re.search(r"v\d+$", item):
                found.append(FakeResult(item))
            elif item in LATEST:
                found.append(FakeResult(f"{item}v{LATEST[item]}"))
        return found

    def fake_request_text(url, on_attempt=None, **kwargs):
        state["abs_calls"].append(on_attempt)
        last_status, last_code = None, None
        for number, (status, code) in enumerate(state["abs_attempts"], start=1):
            last_status, last_code = status, code
            if on_attempt is not None:
                on_attempt(attempt=number, http_status=status, error_code=code,
                           duration=0.01, error_message=None)
        if last_code is not None:
            raise CrawlError("abs page failed", last_code, last_status)
        return HISTORY

    monkeypatch.setattr(discovery, "_query", fake_query)
    monkeypatch.setattr(discovery, "request_text", fake_request_text)

    def events():
        return metrics.read_events(run / "metrics_events.jsonl")

    def metadata(arxiv_id):
        return read_json(discovery.paper_dir(cfg, arxiv_id) / "metadata.json")

    return cfg, run, logger, metrics_logger, opts, state, events, metadata


def discover(env, arxiv_id, with_metrics=True):
    cfg, _, logger, metrics_logger, opts, _, _, _ = env
    return discovery.discover_paper(cfg, arxiv_id, None, opts, None, logger,
                                    metrics_logger if with_metrics else None)


def test_each_discovered_paper_is_one_measured_operation(env):
    assert [discover(env, i) for i in (NEW_V1, NEW_V2)] == ["ok", "ok"]

    events = env[6]()
    summary = metrics.summarize_stage_times(events, "offline", "discovery")
    assert summary["started_operation_count"] == 2
    assert summary["successful_operation_count"] == 2
    assert summary["unfinished_operation_count"] == 0
    finished = [e for e in events if e["event"] == "finished"]
    assert sorted(e["arxiv_id"] for e in finished) == [NEW_V1, NEW_V2]
    assert all(e.get("version") is None for e in finished)


def test_never_revised_paper_has_empty_list_and_needs_no_request(env):
    assert discover(env, NEW_V1) == "ok"

    record = env[7](NEW_V1)
    assert record["latest_version"] == 1
    assert record["revised_dates"] == []
    assert env[5]["abs_calls"] == []
    assert [e for e in env[6]() if e["event"] == "request_attempt_finished"] == []


def test_skipped_paper_is_not_measured(env):
    assert discover(env, NEW_V1) == "ok"
    assert discover(env, NEW_V1) == "skipped"

    summary = metrics.summarize_stage_times(env[6](), "offline", "discovery")
    assert summary["started_operation_count"] == 1


def test_paper_without_arxiv_entry_is_failed_and_the_next_one_still_runs(env):
    assert discover(env, "9999.99999") == "failed"
    assert discover(env, NEW_V2) == "ok"

    summary = metrics.summarize_stage_times(env[6](), "offline", "discovery")
    assert summary["successful_operation_count"] == 1
    assert summary["failed_operation_count"] == 1
    failed = [e for e in env[6]() if e["event"] == "finished" and e["status"] == "failed"]
    assert [e["arxiv_id"] for e in failed] == ["9999.99999"]
    assert env[7]("9999.99999")["status"] == "failed"
    assert env[7]("9999.99999")["error_code"] == "NOT_FOUND"


def test_unexpected_exception_is_measured_as_failed_too(env, monkeypatch):
    def broken(client, throttle, ids):
        raise RuntimeError("bug")

    monkeypatch.setattr(discovery, "_query", broken)

    assert discover(env, NEW_V1) == "failed"

    summary = metrics.summarize_stage_times(env[6](), "offline", "discovery")
    assert summary["failed_operation_count"] == 1
    assert env[7](NEW_V1)["status"] == "failed"


def test_abs_page_attempts_are_logged_with_one_request_id_per_paper(env):
    env[5]["abs_attempts"] = [(503, HTTP_5XX), (200, None)]

    assert [discover(env, i) for i in (OLD_A, OLD_B)] == ["ok", "ok"]

    # would raise "Duplicate request attempt" if one hook were shared between papers
    requests = metrics.summarize_request_attempts(env[6](), "offline")
    assert requests["observed_request_count"] == 2
    assert requests["observed_attempt_count"] == 4
    assert requests["observed_retry_attempt_count"] == 2
    assert requests["error_code_counts"] == {HTTP_5XX: 2}
    assert requests["requests_with_missing_attempts"] == 0
    attempts = [e for e in env[6]() if e["event"] == "request_attempt_finished"]
    assert {e["stage"] for e in attempts} == {"discovery"}
    assert {e["arxiv_id"] for e in attempts} == {OLD_A, OLD_B}
    assert env[7](OLD_A)["revised_dates"] == ["2025-01-02T00:00:00+00:00"]


def test_dates_unavailable_is_still_ok_for_timing_but_keeps_revised_dates_null(env):
    env[5]["query_error"] = CrawlError("second query failed", HTTP_5XX, 503)
    env[5]["abs_attempts"] = [(None, NETWORK_ERROR)]

    assert discover(env, NEW_V3) == "ok"  # version list is known, so the paper is still ok

    record = env[7](NEW_V3)
    assert record["status"] == "ok"
    assert record["latest_version"] == 3
    assert record["revised_dates"] is None
    summary = metrics.summarize_stage_times(env[6](), "offline", "discovery")
    assert summary["successful_operation_count"] == 1  # "ok" only means no exception escaped
    requests = metrics.summarize_request_attempts(env[6](), "offline")
    assert requests["error_code_counts"] == {NETWORK_ERROR: 1}


def test_shared_opts_are_not_modified(env):
    env[5]["abs_attempts"] = [(200, None)]

    discover(env, OLD_A)
    discover(env, OLD_B)

    assert env[4]["on_attempt"] is None
    hooks = env[5]["abs_calls"]
    assert len(hooks) == 2 and all(hook is not None for hook in hooks)
    assert len({id(hook) for hook in hooks}) == 2


def test_without_metrics_logger_nothing_is_measured(env):
    assert discover(env, OLD_A, with_metrics=False) == "ok"

    assert env[5]["abs_calls"] == [None]
    assert not (env[1] / "metrics_events.jsonl").exists()


def test_run_discovery_passes_the_metrics_logger_on(env, tmp_path):
    cfg, run, logger, metrics_logger = env[0], env[1], env[2], env[3]
    ids_file = tmp_path / "ids.txt"
    ids_file.write_text(f"{NEW_V1}\n{NEW_V2}\n9999.99999\n", encoding="utf-8")
    cfg.update(ids_file=ids_file, arxiv_delay_seconds=0, max_retries=0, timeout=5,
               user_agent="test-agent")

    counts = discovery.run_discovery(cfg, logger, metrics_logger)

    assert counts == {"ok": 2, "failed": 1, "skipped": 0}
    summary = metrics.summarize_stage_times(env[6](), "offline", "discovery")
    assert summary["started_operation_count"] == 3
    assert summary["failed_operation_count"] == 1