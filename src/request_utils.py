"""Shared request utility: timeout, retry and exponential backoff live here only.

Other modules call ``RequestClient.get`` and catch ``PipelineError``; they never
touch ``requests`` directly, so this file can be replaced (e.g. by Chau's version)
as long as the ``get`` interface and the error codes stay the same.
"""

import time
from typing import Optional

import requests

from .common import (
    HTTP_429,
    HTTP_5XX,
    INVALID_RESPONSE,
    NETWORK_ERROR,
    NOT_FOUND,
    TIMEOUT,
    PipelineError,
)

MAX_BACKOFF_SECONDS = 60.0


class RequestClient:
    def __init__(
        self,
        user_agent: str,
        timeout: float,
        max_retries: int,
        backoff_base_seconds: float = 2.0,
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})

    def _sleep_before_retry(self, attempt: int, retry_after: Optional[str]) -> None:
        delay = self.backoff_base_seconds * (2 ** (attempt - 1))
        if retry_after and retry_after.isdigit():
            delay = max(delay, float(retry_after))
        time.sleep(min(delay, MAX_BACKOFF_SECONDS))

    def get(self, url: str, stream: bool = False) -> requests.Response:
        """GET ``url``. Returns a 200 response or raises ``PipelineError``.

        Retried (up to ``max_retries`` extra attempts): timeout, network error,
        HTTP 429 (honours Retry-After), HTTP 5xx.
        Not retried: 404 (NOT_FOUND) and any other 4xx (INVALID_RESPONSE).
        """
        total_attempts = self.max_retries + 1
        error: Optional[PipelineError] = None

        for attempt in range(1, total_attempts + 1):
            retry_after = None
            try:
                response = self.session.get(url, timeout=self.timeout, stream=stream)
            except requests.Timeout as exc:
                error = PipelineError(TIMEOUT, str(exc), attempts=attempt)
            except requests.RequestException as exc:
                error = PipelineError(NETWORK_ERROR, str(exc), attempts=attempt)
            else:
                status = response.status_code
                if status == 200:
                    return response
                retry_after = response.headers.get("Retry-After")
                response.close()
                if status == 404:
                    raise PipelineError(NOT_FOUND, f"404 for {url}", http_status=status, attempts=attempt)
                if status == 429:
                    error = PipelineError(HTTP_429, f"429 for {url}", http_status=status, attempts=attempt)
                elif 500 <= status < 600:
                    error = PipelineError(HTTP_5XX, f"{status} for {url}", http_status=status, attempts=attempt)
                else:
                    raise PipelineError(
                        INVALID_RESPONSE, f"unexpected HTTP {status} for {url}", http_status=status, attempts=attempt
                    )

            if attempt < total_attempts:
                self._sleep_before_retry(attempt, retry_after)

        assert error is not None
        raise error
