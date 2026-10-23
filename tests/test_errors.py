from src.utils.errors import HTTP_4XX, NOT_FOUND, TIMEOUT, ClientError, CrawlError
from src.utils.http import Throttle


def test_http_status_is_an_alias_of_status_code():
    error = CrawlError("boom", TIMEOUT, 503)
    assert error.status_code == error.http_status == 503


def test_http_status_is_none_without_a_status():
    assert CrawlError("boom", TIMEOUT).http_status is None


def test_client_error_codes():
    assert ClientError(404).error_code == NOT_FOUND
    assert ClientError(403).error_code == HTTP_4XX


def test_mark_counts_as_a_request_for_the_next_wait(monkeypatch):
    slept = []
    monkeypatch.setattr("src.utils.http.time.sleep", slept.append)
    throttle = Throttle(3)
    throttle.mark()
    throttle.wait()
    assert len(slept) == 1 and 0 < slept[0] <= 3