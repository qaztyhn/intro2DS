"""Adapter between the baseline modules and the shared HTTP layer in utils/http.py.

Timeout, retry, backoff, throttling and the per-attempt hook live in utils/http.py
(Chau) only. This file turns its CrawlError into the baseline's PipelineError, so
discovery and download keep a single error type, and counts attempts for the events log.
"""

import pathlib
from typing import Callable, Optional

from .common import PipelineError, Throttle
from .utils.errors import CrawlError
from .utils.http import download_file, request_text


class RequestClient:
    def __init__(
        self,
        user_agent: str,
        timeout: float,
        max_retries: int,
        throttle: Throttle,
        backoff_base_seconds: float = 2.0,
        on_attempt: Optional[Callable[..., None]] = None,
    ):
        self.headers = {"User-Agent": user_agent}
        self.timeout = timeout
        self.max_retries = max_retries
        self.throttle = throttle
        self.backoff_base_seconds = backoff_base_seconds
        self.on_attempt = on_attempt  # keyword hook: attempt, http_status, error_code, error_message, duration

    def _run(self, func, *args):
        attempts = 0

        def hook(**info):
            nonlocal attempts
            attempts = info.get("attempt", attempts)
            if self.on_attempt is not None:
                self.on_attempt(**info)

        try:
            return func(
                *args,
                headers=self.headers,
                timeout=self.timeout,
                max_retries=self.max_retries,
                base_delay=self.backoff_base_seconds,
                throttle=self.throttle,
                on_attempt=hook,
            )
        except CrawlError as exc:
            raise PipelineError(
                exc.error_code, str(exc), http_status=exc.status_code, attempts=attempts or None
            ) from exc

    def get_text(self, url: str) -> str:
        """GET ``url`` and return its text, or raise PipelineError."""
        return self._run(request_text, url)

    def download(self, url: str, destination: pathlib.Path) -> pathlib.Path:
        """Stream ``url`` into ``destination`` atomically, or raise PipelineError."""
        return self._run(download_file, url, destination)