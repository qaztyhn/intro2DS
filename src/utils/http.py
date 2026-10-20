"""HTTP GET helpers with bounded retries and application-specific errors."""

import logging
import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

from .errors import (
    ClientError,
    InvalidResponseError,
    NetworkError,
    RateLimitError,
    RequestError,
    RequestTimeoutError,
    ServerError,
)

# Retry transient HTTP failures and cap all retry delays in seconds.
RETRY_STATUS_CODES = {408, 429, 500, 502, 503, 504}
MAX_DELAY = 30

logger = logging.getLogger(__name__)


def get_retry_delay(response, attempt, base_delay=1.0):
    """Use Retry-After when valid; otherwise use capped exponential backoff.

    The attempt index starts at zero for the first retry.
    """
    if response is not None:
        retry_after = (response.headers.get("Retry-After") or "").strip()

        if retry_after:
            # Retry-After may contain a number of seconds or an HTTP date.
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

    # Scale without constructing a huge integer for large attempt indices.
    return min(base_delay * 2**attempt, MAX_DELAY)


def wait_before_retry(response, attempt, max_retries, base_delay, reason):
    """Log the retry number and reason, then wait for the calculated delay."""
    delay = get_retry_delay(response, attempt, base_delay)
    logger.warning(
        "Retry %d/%d after %.1fs (%s)", attempt + 1, max_retries, delay, reason
    )
    time.sleep(delay)


def request_with_retry(
    url,
    params=None,
    headers=None,
    timeout=3,
    max_retries=3,
    base_delay=1.0,
):
    """Send a GET request, retry transient failures, and return the response.

    max_retries counts additional attempts after the initial request.
    timeout must be a finite, positive number of seconds.
    Request failures and HTTP errors are mapped to application-specific errors.
    """
    # Validate retry settings before sending any requests.
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

    # Include the initial request in addition to the allowed retries.
    for attempt in range(max_retries + 1):
        try:
            response = requests.get(
                url=url, params=params, headers=headers, timeout=timeout
            )
        except requests.exceptions.SSLError as e:
            # TLS failures are not retried.
            raise NetworkError() from e

        except requests.exceptions.ChunkedEncodingError as e:
            if attempt == max_retries:
                raise NetworkError() from e
            wait_before_retry(None, attempt, max_retries, base_delay, str(e))
            continue

        except requests.exceptions.Timeout as e:
            if attempt == max_retries:
                raise RequestTimeoutError() from e
            wait_before_retry(None, attempt, max_retries, base_delay, str(e))
            continue

        except requests.exceptions.ConnectionError as e:
            if attempt == max_retries:
                raise NetworkError() from e
            wait_before_retry(None, attempt, max_retries, base_delay, str(e))
            continue

        except requests.exceptions.RequestException as e:
            raise RequestError(str(e)) from e

        status = response.status_code

        if status in RETRY_STATUS_CODES and attempt < max_retries:
            wait_before_retry(
                response, attempt, max_retries, base_delay, f"status={status}"
            )
            continue

        # Map HTTP failures after any permitted retries have been exhausted.
        if status == 429:
            raise RateLimitError()

        if 400 <= status < 500:
            raise ClientError(status)

        if status >= 500:
            raise ServerError(status)

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
    """Return response text using the same request and retry settings."""
    return request_with_retry(*args, **kwargs).text
