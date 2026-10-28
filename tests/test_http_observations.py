"""HTTP observation coverage and ambiguity, without benchmark-tool dependencies."""

from types import SimpleNamespace
from unittest.mock import Mock

import arxiv

from src import metrics
from src.utils import http


def response(status=200, payload=None):
    return SimpleNamespace(status_code=status, headers={}, json=lambda: payload)


def test_arxiv_internal_retry_and_timeout_are_outside_shared_hook(monkeypatch):
    client = arxiv.Client(num_retries=1, delay_seconds=0)
    get = Mock(side_effect=[SimpleNamespace(status_code=503),
                           SimpleNamespace(status_code=200, content=b"synthetic feed")])
    monkeypatch.setattr(client._session, "get", get)
    monkeypatch.setattr(arxiv._feed, "parse", lambda _: SimpleNamespace(results=[object()], malformed=False))
    client._parse_feed("https://export.arxiv.org/api/query?id_list=synthetic")
    assert get.call_count == 2
    assert all("timeout" not in c.kwargs for c in get.call_args_list)
    # The known transport activity in this test supplies no shared metric events.
    report = metrics.summarize_http_observations([], "run")
    assert report["identity_status"] == "unobserved"
    for field in ("observed_request_count", "observed_retry_attempt_count", "observed_http_429_attempt_count",
                  "total_http_request_count", "total_http_retry_count", "total_http_429_count"):
        assert report[field] is None


def test_ambiguous_group_does_not_discard_other_valid_groups(tmp_path):
    logger = metrics.EventLogger(tmp_path / "events.jsonl", "run")
    for identity, attempt, status in [("legacy-pages", 1, 200), ("valid", 1, 429),
                                      ("legacy-pages", 1, 200), ("valid", 2, 200)]:
        logger.log("references", "request_attempt_finished", request_id=identity, attempt=attempt,
                   duration_s=0.1, http_status=status, error_code="HTTP_429" if status == 429 else None)
    rows = list(reversed(metrics.read_events(logger.path)))
    report = metrics.summarize_http_observations(rows, "run")
    assert report["logged_attempt_record_count"] == 4
    assert report["identity_status"] == "ambiguous"
    assert report["observed_request_count"] is None
    assert report["observed_retry_attempt_count"] is None
    assert report["observed_http_429_attempt_count"] is None
    assert report["validated_subset"]["observed_request_count"] == 1
    assert report["validated_subset"]["observed_retry_attempt_count"] == 1


def test_redirect_hops_are_not_counted_as_observed_logical_requests(tmp_path, monkeypatch):
    result = response(200)
    result.history = [response(302), response(301)]
    monkeypatch.setattr(http.requests, "get", lambda **_: result)
    logger = metrics.EventLogger(tmp_path / "events.jsonl", "run")
    http.request_with_retry("https://example.test", on_attempt=metrics.make_request_hook(logger, "download"))
    report = metrics.summarize_http_observations(metrics.read_events(logger.path), "run")
    assert report["observed_request_count"] == 1
    assert report["observed_http_429_attempt_count"] == 0
    assert report["total_http_request_count"] is None
