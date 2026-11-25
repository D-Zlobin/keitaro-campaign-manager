"""Typed Five API integration failures."""


class FiveApiError(Exception):
    """Base class for Five API integration failures."""


class FiveApiTimeoutError(FiveApiError):
    pass


class FiveApiConnectionError(FiveApiError):
    pass


class FiveApiResponseError(FiveApiError):
    """Five API returned an unsuccessful status."""

    def __init__(self, operation: str, status_code: int, snippet: str) -> None:
        super().__init__(f"Five API {operation} failed with status {status_code}")
        self.operation = operation
        self.status_code = status_code
        self.snippet = snippet


class FiveApiInvalidResponseError(FiveApiError):
    """Five API returned a success status with malformed or incomplete data."""
