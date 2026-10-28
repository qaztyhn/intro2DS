"""HTTP GET and file download helpers with throttling, retries, and crawler errors."""

import logging
import math
import time
import requests
import os
import tempfile

from pathlib import Path
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .errors import (
    ClientError,
    InvalidResponseError,
    NetworkError,
    RateLimitError,
    RequestError,
    RequestTimeoutError,
    ServerError,
)

# HTTP statuses eligible for retry while attempts remain.
RETRY_STATUS_CODES = {408, 429, 500, 502, 503, 504}
MAX_DELAY = 30  # Maximum Retry-After or exponential backoff delay, in seconds.
DEFAULT_HEADERS = {"User-Agent": "Intro2DS-crawler/1.0 (student project)"}

logger = logging.getLogger(__name__)


class Throttle:
    """Space sequential calls to wait() by at least min_interval seconds."""

    def __init__(self, min_interval):
        self.min_interval = min_interval
        self._last = None

    def wait(self):
        if self._last is not None:
            # A monotonic clock keeps elapsed time independent of clock changes.
            remaining = self._last + self.min_interval - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
        self._last = time.monotonic()

    def mark(self):
        # Record a request made elsewhere as the latest one.
        self._last = time.monotonic()


ARXIV = Throttle(3.0)
S2 = Throttle(1.0 if os.getenv("S2_API_KEY") else 3.0)


def get_retry_delay(response, attempt, base_delay=1.0):
    """Use Retry-After when valid; otherwise use capped exponential backoff.

    The attempt index starts at zero for the first retry. Delays are in seconds
    and capped at MAX_DELAY; Retry-After dates in the past produce no delay.
    """
    if response is not None:
        retry_after = (response.headers.get("Retry-After") or "").strip()

        if retry_after:
            # Retry-After accepts integer seconds or a timezone-aware HTTP date.
            if retry_after.isdecimal():
                return min(float(retry_after), MAX_DELAY)

            try:
                retry_date = parsedate_to_datetime(retry_after)

                # Only compare dates that include timezone information.
                if retry_date.tzinfo is not None:
                    now = datetime.now(timezone.utc)
                    delay = (retry_date - now).total_seconds()

                    if math.isfinite(delay):
                        return min(max(0, delay), MAX_DELAY)

            except (TypeError, ValueError, OverflowError):
                # Fall back to exponential backoff for malformed dates.
                pass

    # Missing or unusable Retry-After values fall back to exponential backoff.
    return min(base_delay * 2**attempt, MAX_DELAY)


def wait_before_retry(response, attempt, max_retries, base_delay, reason):
    """Log the retry number and reason, then wait for the calculated delay."""
    delay = get_retry_delay(response, attempt, base_delay)
    logger.warning(
        "Retry %d/%d after %.1fs (%s)", attempt + 1, max_retries, delay, reason
    )
    time.sleep(delay)


def _emit(on_attempt, attempt, started, status=None, error_code=None, message=None):
    """Call on_attempt with keyword arguments and log any callback exception."""
    if on_attempt is None:
        return

    try:
        on_attempt(
                attempt=attempt + 1,
                http_status=status,
                error_code=error_code,
                error_message=message,
                # Exclude throttle waits and retry delays from attempt duration.
                duration=time.monotonic() - started,
        )
    except Exception:
        logger.exception("on_attempt hook failed")


def _bind_request_hook(on_attempt):
    """Opt-in factory: allocate once per logical request, never per retry.

    Look only at explicitly attached attributes, so ordinary callbacks and
    mocks with dynamic attribute access keep their existing callback contract.
    Instrumentation failure must not send another HTTP request.
    """
    factory = getattr(on_attempt, "__dict__", {}).get("for_request")
    if factory is None:
        return on_attempt
    try:
        return factory()
    except Exception:
        logger.exception("on_attempt request factory failed")
        return None


def request_with_retry(
    url,
    params=None,
    headers=None,
    timeout=3,
    max_retries=3,
    base_delay=1.0,
    throttle=None,
    on_attempt=None,
):
    """Send a GET request, retry transient failures, and return the response.

    max_retries counts additional attempts after the initial request.
    timeout is a finite, positive connect/read timeout in seconds, not a total
    time limit for the request or its retries.
    Request failures and HTTP errors are mapped to application-specific errors.
    throttle.wait() runs before every attempt, including retries.
    on_attempt is called with keyword arguments: attempt (one-based),
    http_status, error_code, error_message, and duration (seconds). Unavailable
    status or error details are None. Duration excludes throttle and retry waits;
    callback exceptions are logged without aborting the request.
    """
    # Reject invalid retry and timeout settings before sending a request.
    if (
        not isinstance(max_retries, int)
        or isinstance(max_retries, bool)
        or max_retries < 0
    ):
        raise ValueError("max_retries must be a non-negative integer")

    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
        raise ValueError("timeout must be a number")

    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")

    if not isinstance(base_delay, (int, float)) or isinstance(base_delay, bool):
        raise ValueError("base_delay must be a number")

    if not math.isfinite(base_delay) or base_delay < 0:
        raise ValueError("base_delay must be finite and non-negative")

    headers = {**DEFAULT_HEADERS, **(headers or {})}
    on_attempt = _bind_request_hook(on_attempt)

    for attempt in range(max_retries + 1):
        if throttle is not None:
            throttle.wait()
        try:
            started = time.monotonic()
            response = requests.get(
                url=url, params=params, headers=headers, timeout=timeout
            )
        except requests.exceptions.SSLError as e:
            # TLS failures are not retried.
            _emit(
                on_attempt, attempt, started, error_code="NETWORK_ERROR", message=str(e)
            )
            raise NetworkError() from e

        except requests.exceptions.ChunkedEncodingError as e:
            _emit(
                on_attempt, attempt, started, error_code="NETWORK_ERROR", message=str(e)
            )
            if attempt == max_retries:
                raise NetworkError() from e
            wait_before_retry(None, attempt, max_retries, base_delay, str(e))
            continue

        except requests.exceptions.Timeout as e:
            _emit(on_attempt, attempt, started, error_code="TIMEOUT", message=str(e))
            if attempt == max_retries:
                raise RequestTimeoutError() from e
            wait_before_retry(None, attempt, max_retries, base_delay, str(e))
            continue

        except requests.exceptions.ConnectionError as e:
            _emit(
                on_attempt, attempt, started, error_code="NETWORK_ERROR", message=str(e)
            )
            if attempt == max_retries:
                raise NetworkError() from e
            wait_before_retry(None, attempt, max_retries, base_delay, str(e))
            continue

        except requests.exceptions.RequestException as e:
            _emit(
                on_attempt, attempt, started, error_code="REQUEST_ERROR", message=str(e)
            )
            raise RequestError(str(e)) from e

        status = response.status_code

        error = None
        # Classify the failure separately from deciding whether it is retryable.
        if status == 408:
            error = RequestTimeoutError()

        elif status == 429:
            error = RateLimitError()

        elif 400 <= status < 500:
            error = ClientError(status)

        elif status >= 500:
            error = ServerError(status)

        _emit(
            on_attempt,
            attempt,
            started,
            status=status,
            error_code=error.error_code if error else None,
        )

        if status in RETRY_STATUS_CODES and attempt < max_retries:
            wait_before_retry(
                response, attempt, max_retries, base_delay, f"status={status}"
            )
            continue

        if error is not None:
            raise error

        return response

    raise RuntimeError("Unexpected retry state")


def request_json(*args, **kwargs):
    """Return parsed JSON, raising InvalidResponseError if decoding fails."""
    response = request_with_retry(*args, **kwargs)

    try:
        return response.json()
    except requests.exceptions.JSONDecodeError as e:
        raise InvalidResponseError() from e


def request_text(*args, **kwargs):
    """Return decoded response text via request_with_retry."""
    return request_with_retry(*args, **kwargs).text


def request_bytes(*args, **kwargs):
    """Return response body bytes via request_with_retry."""
    return request_with_retry(*args, **kwargs).content



def download_file(
    url,
    destination,
    params=None,
    headers=None,
    timeout=30,
    max_retries=3,
    base_delay=1.0,
    throttle=None,
    on_attempt=None,
    chunk_size=65536,
):
    """Download a file in chunks, retrying transient HTTP and stream failures.

    max_retries counts additional attempts; each retry restarts the download.
    timeout, throttle, and on_attempt follow the request_with_retry conventions.
    Successful attempt duration includes streaming and replacing the file.

    Write to a temporary file beside destination, then atomically replace it
    after the stream finishes. Return destination as a Path. Failed attempts
    leave an existing destination untouched and remove their temporary files.
    """

    # Validate retry, timeout, and chunk settings before filesystem or network I/O.
    if (
        type(max_retries) is not int
        or max_retries < 0
    ):
        raise ValueError("max_retries must be a non-negative integer")

    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("timeout must be finite and positive")

    if (
        isinstance(base_delay, bool)
        or not isinstance(base_delay, (int, float))
        or not math.isfinite(base_delay)
        or base_delay < 0
    ):
        raise ValueError("base_delay must be finite and non-negative")

    if type(chunk_size) is not int or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    headers = {**DEFAULT_HEADERS, **(headers or {})}
    on_attempt = _bind_request_hook(on_attempt)

    for attempt in range(max_retries + 1):
        response = None
        temp_path = None

        # Apply rate limiting to every attempt; exclude this wait from timing.
        if throttle is not None:
            throttle.wait()

        started = time.monotonic()

        try:
            # Stream the response so the entire file is not buffered in memory.
            response = requests.get(
                url=url,
                params=params,
                headers=headers,
                timeout=timeout,
                stream=True,
            )

            status = response.status_code

            # Classify HTTP status before reading the body.
            error = None

            if status == 408:
                error = RequestTimeoutError()
            elif status == 429:
                error = RateLimitError()
            elif 400 <= status < 500:
                error = ClientError(status)
            elif status >= 500:
                error = ServerError(status)

            if error is not None:
                _emit(
                    on_attempt,
                    attempt,
                    started,
                    status=status,
                    error_code=error.error_code,
                )

                # Release the connection before waiting to retry transient errors.
                if status in RETRY_STATUS_CODES and attempt < max_retries:
                    response.close()
                    wait_before_retry(
                        response,
                        attempt,
                        max_retries,
                        base_delay,
                        f"status={status}",
                    )
                    continue

                raise error

            # Keep the temporary file on the same filesystem for atomic replacement.
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=destination.name + ".",
                suffix=".part",
                delete=False,
            ) as file:
                temp_path = Path(file.name)

                # Ignore empty chunks yielded by the response iterator.
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        file.write(chunk)

            # Atomically replace the destination after the temporary file is closed.
            os.replace(temp_path, destination)
            temp_path = None

            _emit(
                on_attempt,
                attempt,
                started,
                status=status,
            )

            return destination

        # TLS failures are reported immediately without retrying.
        except requests.exceptions.SSLError as e:
            _emit(
                on_attempt,
                attempt,
                started,
                error_code="NETWORK_ERROR",
                message=str(e),
            )
            raise NetworkError() from e

        # Retry timeouts from either the initial request or streaming the body.
        except requests.exceptions.Timeout as e:
            _emit(
                on_attempt,
                attempt,
                started,
                error_code="TIMEOUT",
                message=str(e),
            )

            if attempt == max_retries:
                raise RequestTimeoutError() from e

            wait_before_retry(
                None, attempt, max_retries, base_delay, str(e)
            )
            continue

        # Retry interrupted connections and invalid or incomplete response streams.
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ContentDecodingError,
        ) as e:
            _emit(
                on_attempt,
                attempt,
                started,
                error_code="NETWORK_ERROR",
                message=str(e),
            )

            if attempt == max_retries:
                raise NetworkError() from e

            wait_before_retry(
                None, attempt, max_retries, base_delay, str(e)
            )
            continue

        # Other requests exceptions are mapped without retrying.
        except requests.exceptions.RequestException as e:
            _emit(
                on_attempt,
                attempt,
                started,
                error_code="REQUEST_ERROR",
                message=str(e),
            )
            raise RequestError(str(e)) from e

        finally:
            # Release resources on success, failure, and before the next attempt.
            if response is not None:
                response.close()

            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    raise RuntimeError("Unexpected download retry state")
