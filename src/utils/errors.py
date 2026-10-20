class CrawlError(Exception):
    def __init__(self, message, error_code, status_code=None):
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code

class NetworkError(CrawlError):
    def __init__(self, message="Network connection failed"):
        super().__init__(message, "NETWORK_ERROR")


class RequestTimeoutError(CrawlError):
    def __init__(self, message="Request timed out"):
        super().__init__(message, "TIMEOUT")


class RateLimitError(CrawlError):
    def __init__(self, message="API rate limit exceeded"):
        super().__init__(message, "HTTP_429", 429)


class ServerError(CrawlError):
    def __init__(self, status_code):
        super().__init__(
            f"Server returned HTTP {status_code}",
            "HTTP_5XX",
            status_code
        )


class ClientError(CrawlError):
    def __init__(self, status_code):
        code = "NOT_FOUND" if status_code == 404 else "HTTP_4XX"

        super().__init__(
            f"Client error: HTTP {status_code}",
            code,
            status_code
        )


class InvalidResponseError(CrawlError):
    def __init__(self, message="Invalid API response"):
        super().__init__(message, "INVALID_RESPONSE")


class RequestError(CrawlError):
    def __init__(self, message="Unexpected HTTP request error"):
        super().__init__(message, "REQUEST_ERROR")