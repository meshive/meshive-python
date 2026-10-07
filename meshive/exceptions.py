"""Meshive SDK exception hierarchy.

Server error format: {"detail": {"title": ..., "message": ...}}.
HTTP status codes are mapped to exceptions in _client._raise_for_status.
"""
from __future__ import annotations


class MeshiveError(Exception):
    """Base class of every Meshive SDK exception."""


class ConfigurationError(MeshiveError):
    """Client configuration problem such as a missing API key (raised before any request is sent)."""


class WaitTimeoutError(MeshiveError, TimeoutError):
    """wait_for_pod did not reach the target state within the time limit.

    It is also a built-in TimeoutError, so both `except MeshiveError` and `except TimeoutError`
    catch it.
    """


class MeshiveAPIError(MeshiveError):
    """The server returned a 4xx/5xx.

    status_code: HTTP status code
    title/message: the server's detail.title / detail.message (if present)
    raw: the whole parsed response body (dict | str | None)
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        title: str | None = None,
        raw: object = None,
    ) -> None:
        self.status_code = status_code
        self.title = title
        self.message = message
        self.raw = raw
        prefix = f"[{status_code}]"
        if title:
            prefix += f" {title}"
        super().__init__(f"{prefix}: {message}")


class AuthenticationError(MeshiveAPIError):
    """401 — API key missing, invalid or expired, or the account is inactive."""


class PermissionDeniedError(MeshiveAPIError):
    """403 — the key lacks the required scope."""


class NotFoundError(MeshiveAPIError):
    """404 — resource (pod, user, ...) not found."""


class InsufficientCreditError(MeshiveAPIError):
    """402 — the workspace's billing account doesn't have enough credit (when creating/starting pods, storage, tasks)."""


class ConflictError(MeshiveAPIError):
    """409 — conflicts with the current state: no capacity (No Capacity), duplicate name (Name Taken), price over the cap (Price Exceeds Cap),
    storage in use (Storage In Use), a request with the same Idempotency-Key still in progress (Request In Progress), etc.
    Tell them apart by `title`; `raw["detail"]` carries extra info such as availability/pricePerHourUsd."""


class RateLimitError(MeshiveAPIError):
    """429 — per-user rate limit exceeded. Exposes retry_after (seconds) when present."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        title: str | None = None,
        raw: object = None,
        retry_after: float | None = None,
    ) -> None:
        self.retry_after = retry_after
        super().__init__(status_code, message, title=title, raw=raw)
