"""Typed errors for the DolphinScheduler CLI harness.

Every failure path raises one of these so the CLI layer can translate it into a
clear, machine-readable message. Agents rely on unambiguous errors to
self-correct, so we never swallow a failure silently.
"""

from __future__ import annotations

from typing import Any, Optional


class DolphinSchedulerError(Exception):
    """Base class for all harness errors."""

    #: Stable, machine-readable identifier surfaced in ``--json`` error output.
    code: str = "dolphinscheduler_error"

    def __init__(self, message: str, *, detail: Any = None):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


class ConfigError(DolphinSchedulerError):
    """Raised when connection configuration is missing or invalid."""

    code = "config_error"


class AuthError(DolphinSchedulerError):
    """Raised when authentication fails or no credentials are available."""

    code = "auth_error"


class APIError(DolphinSchedulerError):
    """Raised when the DolphinScheduler API returns a non-success envelope.

    DolphinScheduler always responds with ``{"code", "msg", "data"}`` and uses
    ``code == 0`` for success. A non-zero code is surfaced here with the
    server-provided message so the caller sees exactly why the call failed.
    """

    code = "api_error"

    def __init__(
        self,
        message: str,
        *,
        api_code: Optional[int] = None,
        http_status: Optional[int] = None,
        detail: Any = None,
    ):
        super().__init__(message, detail=detail)
        self.api_code = api_code
        self.http_status = http_status

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        if self.api_code is not None:
            payload["api_code"] = self.api_code
        if self.http_status is not None:
            payload["http_status"] = self.http_status
        return payload


class NetworkError(DolphinSchedulerError):
    """Raised when the API server is unreachable (DNS, refused, timeout)."""

    code = "network_error"


class NotFoundError(DolphinSchedulerError):
    """Raised when a requested resource cannot be located by name."""

    code = "not_found"
