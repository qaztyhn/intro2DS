from src.utils.errors import HTTP_4XX, NOT_FOUND, TIMEOUT, ClientError, CrawlError
from src.utils.http import Throttle

def test_status_code_is_stored():
    assert CrawlError("boom", TIMEOUT, 503).status_code == 503


def test_status_code_is_none_without_a_status():
    assert CrawlError("boom", TIMEOUT).status_code is None


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