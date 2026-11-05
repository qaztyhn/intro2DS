"""Per-client connect/read timeout for the arxiv 4.x Requests transport.

The library owns rate limiting, pagination and retries. Its private _session
is replaced only while constructing a new client; Requests is never patched.
"""

from math import isfinite
from numbers import Real

import requests


class TimeoutSession(requests.Session):
    """Supply the YAML timeout when Session.request forwards timeout=None.

    A scalar Requests timeout applies to connect and individual read waits;
    it is not a deadline for the response body, retry loop or pipeline.
    """

    def __init__(self, timeout):
        if isinstance(timeout, bool) or not isinstance(timeout, Real):
            raise TypeError("arXiv timeout must be a real number")
        if not isfinite(timeout) or timeout <= 0:
            raise ValueError("arXiv timeout must be finite and positive")
        super().__init__()
        self.default_timeout = float(timeout)

    def send(self, request, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self.default_timeout
        return super().send(request, **kwargs)


def configure_arxiv_timeout(client, timeout):
    """Bind only the known fresh arxiv 4.x Session; fail on API drift.

    Current arxiv.Client.__init__ creates a plain, unused requests.Session.
    Replacing an unexpected/custom session could discard authentication or
    adapter state, so do not silently accept one.
    """
    previous = getattr(client, "_session", None)
    if type(previous) is not requests.Session:
        raise RuntimeError("Unsupported arxiv.Client transport: expected a fresh requests.Session")
    replacement = TimeoutSession(timeout)
    previous.close()
    client._session = replacement
    return client
