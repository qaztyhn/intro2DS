"""Offline tests for HTTP retries, response helpers, and safe file downloads."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from src.utils import http


URL = "https://example.test/file"


def response(status=200, chunks=(b"hello", b"", b" world"), headers=None):
    result = Mock(status_code=status, headers=headers or {})
    result.iter_content.return_value = iter(chunks)
    return result


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.get = self.enterContext(patch.object(http.requests, "get"))
        self.sleep = self.enterContext(patch.object(http.time, "sleep"))
        self.enterContext(patch.object(http, "logger"))
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.destination = self.directory / "download.bin"

    def test_backoff_and_retry_after(self):
        for attempt, expected in [(0, 1), (2, 4), (10, http.MAX_DELAY)]:
            with self.subTest(attempt=attempt):
                self.assertEqual(http.get_retry_delay(None, attempt), expected)
        for header, expected in [(" 7 ", 7), ("999", 30), ("bad", 4)]:
            with self.subTest(header=header):
                self.assertEqual(
                    http.get_retry_delay(response(headers={"Retry-After": header}), 2),
                    expected,
                )

    def test_retry_after_http_date(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with patch.object(http, "datetime") as clock:
            clock.now.return_value = now
            for seconds, expected in [(-10, 0), (12, 12), (100, http.MAX_DELAY)]:
                with self.subTest(seconds=seconds):
                    header = format_datetime(now + timedelta(seconds=seconds))
                    self.assertEqual(
                        http.get_retry_delay(response(headers={"Retry-After": header}), 0),
                        expected,
                    )

    def test_throttle_waits_only_for_remaining_interval(self):
        throttle = http.Throttle(3)
        with patch.object(http.time, "monotonic", side_effect=[10, 11, 13, 17, 17]):
            throttle.wait()
            self.sleep.assert_not_called()
            throttle.wait()
            throttle.wait()
        self.sleep.assert_called_once_with(2)

    def test_request_success_and_header_override(self):
        result = response()
        self.get.return_value = result
        throttle = Mock()
        hook = Mock()
        self.assertIs(
            http.request_with_retry(
                URL, params={"q": "test"}, headers={"User-Agent": "custom"},
                timeout=5, throttle=throttle, on_attempt=hook,
            ),
            result,
        )
        self.get.assert_called_once_with(
            url=URL, params={"q": "test"}, headers={"User-Agent": "custom"}, timeout=5,
        )
        throttle.wait.assert_called_once_with()
        event = hook.call_args.args[0]
        self.assertEqual((event["attempt"], event["http_status"]), (1, 200))
        self.assertIsNone(event["error_code"])
        self.assertGreaterEqual(event["duration"], 0)

    def test_invalid_settings_fail_before_network_or_directory_creation(self):
        settings = {
            "max_retries": [-1, True, 1.5],
            "timeout": [0, -1, True, "3", float("nan"), float("inf")],
            "base_delay": [-1, True, "1", float("nan"), float("inf")],
        }
        for name, values in settings.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError):
                        http.request_with_retry(URL, **{name: value})
                    destination = self.directory / "missing" / "file.bin"
                    with self.assertRaises(ValueError):
                        http.download_file(URL, destination, **{name: value})
                    self.assertFalse(destination.parent.exists())
        for value in [0, -1, True, 1.5]:
            with self.subTest(chunk_size=value), self.assertRaises(ValueError):
                http.download_file(URL, self.destination, chunk_size=value)
        self.get.assert_not_called()

    def test_request_retries_transient_statuses(self):
        for status in http.RETRY_STATUS_CODES:
            with self.subTest(status=status):
                self.get.reset_mock()
                self.sleep.reset_mock()
                good = response()
                self.get.side_effect = [response(status), good]
                throttle = Mock()
                self.assertIs(http.request_with_retry(URL, max_retries=1, throttle=throttle), good)
                self.assertEqual(self.get.call_count, 2)
                self.assertEqual(throttle.wait.call_count, 2)
                self.sleep.assert_called_once_with(1)

    def test_http_error_mapping_for_requests_and_downloads(self):
        cases = [(408, http.RequestTimeoutError), (429, http.RateLimitError),
                 (404, http.ClientError), (500, http.ServerError), (501, http.ServerError)]
        for status, error in cases:
            for download in [False, True]:
                with self.subTest(status=status, download=download):
                    self.get.reset_mock()
                    result = response(status)
                    self.get.return_value = result
                    with self.assertRaises(error):
                        if download:
                            http.download_file(URL, self.destination, max_retries=0)
                        else:
                            http.request_with_retry(URL, max_retries=0)
                    self.get.assert_called_once()
                    if download:
                        result.close.assert_called()
                        result.iter_content.assert_not_called()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_client_errors_are_not_retried(self):
        for download in [False, True]:
            with self.subTest(download=download):
                self.get.reset_mock()
                self.get.return_value = response(404)
                with self.assertRaises(http.ClientError):
                    if download:
                        http.download_file(URL, self.destination)
                    else:
                        http.request_with_retry(URL)
                self.get.assert_called_once()
        self.sleep.assert_not_called()

    def test_transient_request_exceptions_retry_until_exhausted(self):
        cases = [(requests.exceptions.Timeout, http.RequestTimeoutError),
                 (requests.exceptions.ConnectionError, http.NetworkError),
                 (requests.exceptions.ChunkedEncodingError, http.NetworkError)]
        for exception, mapped in cases:
            with self.subTest(exception=exception.__name__):
                self.get.reset_mock()
                self.sleep.reset_mock()
                self.get.side_effect = exception("interrupted")
                with self.assertRaises(mapped):
                    http.request_with_retry(URL, max_retries=2)
                self.assertEqual(self.get.call_count, 3)
                self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [1, 2])

    def test_tls_and_other_request_errors_are_not_retried(self):
        cases = [(requests.exceptions.SSLError, http.NetworkError),
                 (requests.exceptions.RequestException, http.RequestError)]
        for exception, mapped in cases:
            for download in [False, True]:
                with self.subTest(exception=exception.__name__, download=download):
                    self.get.reset_mock()
                    self.get.side_effect = exception("failed")
                    with self.assertRaises(mapped):
                        if download:
                            http.download_file(URL, self.destination)
                        else:
                            http.request_with_retry(URL)
                    self.get.assert_called_once()
        self.sleep.assert_not_called()

    def test_response_helpers(self):
        result = response()
        result.json.return_value = {"ok": True}
        result.text = "hello"
        result.content = b"hello"
        self.get.return_value = result
        self.assertEqual(http.request_json(URL), {"ok": True})
        self.assertEqual(http.request_text(URL), "hello")
        self.assertEqual(http.request_bytes(URL), b"hello")
        result.json.side_effect = requests.exceptions.JSONDecodeError("invalid", "x", 0)
        with self.assertRaises(http.InvalidResponseError):
            http.request_json(URL)

    def test_callback_failure_does_not_abort_request_or_download(self):
        self.get.side_effect = [response(), response()]
        hook = Mock(side_effect=RuntimeError("callback failed"))
        self.assertEqual(http.request_with_retry(URL, on_attempt=hook).status_code, 200)
        self.assertEqual(http.download_file(URL, self.destination, on_attempt=hook), self.destination)
        self.assertEqual(self.destination.read_bytes(), b"hello world")
        self.assertEqual(hook.call_count, 2)

    def test_download_streams_chunks_and_creates_parent_directory(self):
        self.destination = self.directory / "nested" / "file.bin"
        result = response()
        self.get.return_value = result
        self.assertEqual(http.download_file(URL, self.destination, chunk_size=4), self.destination)
        self.assertEqual(self.destination.read_bytes(), b"hello world")
        result.iter_content.assert_called_once_with(chunk_size=4)
        self.assertTrue(self.get.call_args.kwargs["stream"])
        result.close.assert_called_once_with()
        self.assertEqual(list(self.destination.parent.iterdir()), [self.destination])

    def test_download_replaces_existing_file_only_after_stream_finishes(self):
        self.destination.write_bytes(b"original")

        def chunks():
            self.assertEqual(self.destination.read_bytes(), b"original")
            yield b"new"
            self.assertEqual(self.destination.read_bytes(), b"original")

        self.get.return_value = response(chunks=chunks())
        http.download_file(URL, self.destination)
        self.assertEqual(self.destination.read_bytes(), b"new")
        self.assertEqual(list(self.directory.iterdir()), [self.destination])

    def test_download_retries_transient_statuses(self):
        for status in http.RETRY_STATUS_CODES:
            with self.subTest(status=status):
                self.get.reset_mock()
                bad, good = response(status), response()
                self.get.side_effect = [bad, good]
                throttle = Mock()
                events = []
                http.download_file(URL, self.destination, max_retries=1,
                                   throttle=throttle, on_attempt=events.append)
                self.assertEqual(self.destination.read_bytes(), b"hello world")
                self.assertEqual(self.get.call_count, 2)
                self.assertEqual(throttle.wait.call_count, 2)
                self.assertEqual([event["attempt"] for event in events], [1, 2])
                self.assertEqual([event["http_status"] for event in events], [status, 200])
                bad.close.assert_called()
                good.close.assert_called_once_with()

    def test_download_restarts_after_partial_stream_failure(self):
        exceptions = [requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                      requests.exceptions.ChunkedEncodingError, requests.exceptions.ContentDecodingError]
        for exception in exceptions:
            with self.subTest(exception=exception.__name__):
                def interrupted():
                    yield b"partial"
                    raise exception("stream failed")

                bad, good = response(chunks=interrupted()), response(chunks=(b"complete",))
                self.get.side_effect = [bad, good]
                http.download_file(URL, self.destination, max_retries=1)
                self.assertEqual(self.destination.read_bytes(), b"complete")
                self.assertEqual(list(self.directory.iterdir()), [self.destination])
                bad.close.assert_called_once_with()
                good.close.assert_called_once_with()

    def test_failed_download_preserves_destination_and_removes_partial_file(self):
        self.destination.write_bytes(b"original")
        for exception, mapped in [(requests.exceptions.Timeout, http.RequestTimeoutError),
                                  (requests.exceptions.ChunkedEncodingError, http.NetworkError)]:
            with self.subTest(exception=exception.__name__):
                def interrupted():
                    yield b"partial"
                    raise exception("stream failed")

                result = response(chunks=interrupted())
                self.get.return_value = result
                with self.assertRaises(mapped):
                    http.download_file(URL, self.destination, max_retries=0)
                self.assertEqual(self.destination.read_bytes(), b"original")
                self.assertEqual(list(self.directory.iterdir()), [self.destination])
                result.close.assert_called_once_with()

    def test_download_connection_failure_stops_at_retry_limit(self):
        self.get.side_effect = requests.exceptions.ConnectionError("offline")
        with self.assertRaises(http.NetworkError):
            http.download_file(URL, self.destination, max_retries=2)
        self.assertEqual(self.get.call_count, 3)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_replace_failure_preserves_destination_and_cleans_up(self):
        self.destination.write_bytes(b"original")
        result = response()
        self.get.return_value = result
        with patch.object(http.os, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                http.download_file(URL, self.destination)
        self.assertEqual(self.destination.read_bytes(), b"original")
        self.assertEqual(list(self.directory.iterdir()), [self.destination])
        result.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
