"""Real shared HTTP/references code with mocked transport; no API calls."""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from src import metrics
from src.stages import references
from src.utils import http


def response(status=200, payload=None):
    result = Mock(status_code=status, headers={})
    result.json.return_value = payload
    result.iter_content.return_value = iter([b"synthetic bytes"])
    return result


@pytest.fixture
def transport(monkeypatch):
    get = Mock()
    monkeypatch.setattr(http.requests, "get", get)
    monkeypatch.setattr(http.time, "sleep", lambda _: None)
    monkeypatch.setattr(references, "S2", http.Throttle(0))
    monkeypatch.delenv("S2_API_KEY", raising=False)
    return get


def test_two_reference_pages_with_retry_have_separate_request_ids(tmp_path, transport):
    # Every page restarts its attempt counter. Its retries must retain its ID.
    transport.side_effect = [
        response(200, {"offset": 0, "data": [{"citedPaper": {"paperId": "first"}}], "next": 7}),
        response(429), response(200, {"offset": 7, "data": [{"citedPaper": {"paperId": "second"}}]}),
    ]
    logger = metrics.EventLogger(tmp_path / "metrics.jsonl", "pagination")
    hook = metrics.make_request_hook(logger, "references", arxiv_id="2505.16300")
    result = references.crawl_references("2505.16300", on_attempt=hook, max_retries=1)
    assert result["status"] == "ok"
    assert len(result["references"]) == 2
    events = metrics.read_events(logger.path)
    assert len({e["request_id"] for e in events}) == 2
    assert events[1]["request_id"] == events[2]["request_id"]
    assert [e["attempt"] for e in events] == [1, 1, 2]
    summary = metrics.summarize_request_attempts(events, "pagination")
    assert summary["observed_request_count"] == 2
    assert summary["observed_retry_attempt_count"] == 1
    assert summary["error_code_counts"] == {"HTTP_429": 1}


def test_later_page_404_keeps_attempts_but_discards_partial_content(tmp_path, transport):
    transport.side_effect = [
        response(200, {"offset": 0, "data": [{"citedPaper": {"paperId": "first"}}], "next": 1}),
        response(404),
    ]
    logger = metrics.EventLogger(tmp_path / "metrics.jsonl", "pagination")
    result = references.crawl_references("2505.16300", max_retries=3,
        on_attempt=metrics.make_request_hook(logger, "references", arxiv_id="2505.16300"))
    assert result["status"] == "failed"
    assert result["error_code"] == "NOT_FOUND"
    assert result["references"] == []
    summary = metrics.summarize_request_attempts(metrics.read_events(logger.path), "pagination")
    assert summary["observed_request_count"] == 2
    assert summary["observed_attempt_count"] == 2
    assert summary["error_code_counts"] == {"NOT_FOUND": 1}
    assert transport.call_count == 2


def test_reused_hook_gets_new_identity_for_download_calls(tmp_path, transport):
    transport.side_effect = [response(), response(503), response()]
    logger = metrics.EventLogger(tmp_path / "metrics.jsonl", "downloads")
    hook = metrics.make_request_hook(logger, "download", arxiv_id="2505.16300", version=1)
    http.download_file("https://example.test/a", tmp_path / "a", on_attempt=hook)
    http.download_file("https://example.test/b", tmp_path / "b", on_attempt=hook)
    summary = metrics.summarize_request_attempts(metrics.read_events(logger.path), "downloads")
    assert summary["observed_request_count"] == 2
    assert summary["observed_retry_attempt_count"] == 1


def test_concurrent_reuse_does_not_infer_scope_from_attempt_order(tmp_path, transport):
    transport.side_effect = lambda **_: response()
    logger = metrics.EventLogger(tmp_path / "metrics.jsonl", "threads")
    hook = metrics.make_request_hook(logger, "references")
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(http.request_with_retry, "https://example.test", on_attempt=hook)
                   for _ in range(12)]
        for future in futures:
            future.result()
    events = metrics.read_events(logger.path)
    assert len({e["request_id"] for e in events}) == 12
    assert metrics.summarize_request_attempts(events, "threads")["observed_retry_attempt_count"] == 0


def test_direct_hook_id_and_parent_group_are_preserved(tmp_path, transport):
    transport.return_value = response()
    logger = metrics.EventLogger(tmp_path / "events.jsonl", "run")
    hook = metrics.make_request_hook(logger, "references", request_id="explicit-parent")
    hook(attempt=1, http_status=200, error_code=None, duration=0.1, error_message=None)
    http.request_with_retry("https://example.test", on_attempt=hook)
    rows = metrics.read_events(logger.path)
    assert rows[0]["request_id"] == "explicit-parent"
    assert rows[1]["request_id"] != "explicit-parent"
    assert rows[1]["request_group_id"] == "explicit-parent"


def test_factory_failure_never_causes_an_extra_http_request(transport):
    transport.return_value = response()
    def callback(**_):
        raise AssertionError("factory failed; no callback should be used")
    def bad_factory():
        raise RuntimeError("synthetic factory failure")
    callback.for_request = bad_factory
    assert http.request_with_retry("https://example.test", on_attempt=callback).status_code == 200
    assert transport.call_count == 1
