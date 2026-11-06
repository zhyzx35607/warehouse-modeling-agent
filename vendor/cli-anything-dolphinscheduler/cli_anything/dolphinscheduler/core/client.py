"""HTTP client for the DolphinScheduler REST API.

This module is the single point of contact with the *real* DolphinScheduler
API server. Every higher-level module (projects, workflows, executors, ...)
issues its calls through :class:`DolphinSchedulerClient` so that auth, the
``Result`` envelope, and error handling live in exactly one place.

Authentication follows the server's own model:

* **Token auth (preferred)** — a DolphinScheduler access token sent in the
  literal ``token`` HTTP header. This is stateless and ideal for a CLI.
* **Session auth (fallback)** — ``POST /login`` with username/password, which
  sets a ``sessionId`` cookie reused for later calls.

DolphinScheduler binds almost every endpoint with ``@RequestParam`` (i.e.
form-encoded fields), even when a field value is itself JSON. A few endpoints
take raw JSON bodies or multipart uploads; those are handled explicitly by the
callers via :meth:`request` options.
"""

from __future__ import annotations

from typing import Any, Optional, Union

import requests

from .config import ClientConfig
from .errors import APIError, AuthError, NetworkError

# DolphinScheduler's Result envelope uses code 0 for success, regardless of
# the HTTP status line.
_SUCCESS_CODE = 0

# The header the server reads the access token from (LoginHandlerInterceptor).
_TOKEN_HEADER = "token"

JSONValue = Union[dict, list, str, int, float, bool, None]


class DolphinSchedulerClient:
    """A thin, well-behaved client over the DolphinScheduler REST API.

    The client owns a :class:`requests.Session` for connection reuse and cookie
    persistence. It never interprets domain payloads — it only unwraps the
    ``Result`` envelope and surfaces failures as typed exceptions.
    """

    def __init__(self, config: ClientConfig, *, session: Optional[requests.Session] = None):
        self._config = config
        self._session = session or requests.Session()
        if config.token:
            self._session.headers[_TOKEN_HEADER] = config.token
        self._logged_in = False

    # ── Properties ──────────────────────────────────────────────────────

    @property
    def config(self) -> ClientConfig:
        return self._config

    @property
    def has_token(self) -> bool:
        return bool(self._config.token)

    # ── Authentication ──────────────────────────────────────────────────

    def ensure_authenticated(self) -> None:
        """Make sure the client can make authorized calls.

        A token needs no round trip. Otherwise, credentials must be present and
        we log in once to obtain a session cookie.
        """
        if self.has_token:
            return
        if self._logged_in:
            return
        if not (self._config.user and self._config.password):
            raise AuthError(
                "No credentials configured. Provide an access token (--token / "
                "DS_TOKEN) or a username and password (--user/--password or "
                "DS_USER/DS_PASSWORD)."
            )
        self.login(self._config.user, self._config.password)

    def login(self, user: str, password: str) -> dict[str, Any]:
        """Authenticate with username/password and store the session cookie.

        Returns the login payload (which includes ``sessionId``). Raises
        :class:`AuthError` on invalid credentials.
        """
        # /login binds userName/userPassword as form params at the context root.
        payload = self.request(
            "POST",
            "/login",
            data={"userName": user, "userPassword": password},
            authenticated=False,
        )
        self._logged_in = True
        return payload if isinstance(payload, dict) else {}

    # ── Core request plumbing ───────────────────────────────────────────

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
        json_body: JSONValue = None,
        files: Optional[dict[str, Any]] = None,
        authenticated: bool = True,
        raw: bool = False,
    ) -> Any:
        """Issue a request and return the unwrapped ``data`` field.

        Args:
            method: HTTP verb (GET/POST/PUT/DELETE).
            path: Path relative to the base URL, e.g. ``/projects``.
            params: Query-string parameters.
            data: Form-encoded body fields (the common case).
            json_body: Raw JSON body, for the few endpoints that expect one.
            files: Multipart file uploads.
            authenticated: When True, ensure auth before sending.
            raw: When True, return the full envelope dict instead of ``data``.

        Returns:
            The ``data`` field of the ``Result`` envelope, or the whole envelope
            when ``raw`` is True.
        """
        if authenticated:
            self.ensure_authenticated()

        url = self._build_url(path)
        clean_params = _drop_none(params)
        clean_data = _drop_none(data)

        try:
            response = self._session.request(
                method.upper(),
                url,
                params=clean_params,
                data=clean_data if json_body is None else None,
                json=json_body,
                files=files,
                timeout=self._config.timeout,
                verify=self._config.verify_tls,
            )
        except requests.RequestException as exc:
            raise NetworkError(
                f"Could not reach DolphinScheduler at {url}: {exc}"
            ) from exc

        return self._handle_response(response, raw=raw)

    def download(
        self,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
        authenticated: bool = True,
    ) -> bytes:
        """Issue a request whose success payload is binary content.

        Resource downloads return an attachment instead of the normal
        DolphinScheduler ``Result`` envelope. Error responses still use the
        envelope, so we parse JSON bodies before returning raw bytes.
        """
        if authenticated:
            self.ensure_authenticated()

        url = self._build_url(path)
        try:
            response = self._session.request(
                "GET",
                url,
                params=_drop_none(params),
                data=_drop_none(data),
                timeout=self._config.timeout,
                verify=self._config.verify_tls,
            )
        except requests.RequestException as exc:
            raise NetworkError(
                f"Could not reach DolphinScheduler at {url}: {exc}"
            ) from exc

        if response.headers.get("Content-Type", "").startswith("application/json"):
            self._handle_response(response, raw=False)
            return b""
        if response.status_code in (401, 403):
            raise AuthError(
                "Authentication failed or token expired (HTTP "
                f"{response.status_code}). Re-check your token/credentials."
            )
        if response.status_code >= 400:
            raise APIError(
                f"HTTP {response.status_code} from {response.url}",
                http_status=response.status_code,
            )
        return response.content

    def _build_url(self, path: str) -> str:
        return f"{self._config.base_url}/{path.lstrip('/')}"

    def _handle_response(self, response: requests.Response, *, raw: bool) -> Any:
        """Validate HTTP status and the DolphinScheduler ``Result`` envelope."""
        if response.status_code in (401, 403):
            raise AuthError(
                "Authentication failed or token expired (HTTP "
                f"{response.status_code}). Re-check your token/credentials."
            )

        envelope = self._parse_envelope(response)

        # Not every response is an envelope (e.g. binary downloads / log text).
        if envelope is None:
            if response.status_code >= 400:
                raise APIError(
                    f"HTTP {response.status_code} from {response.url}",
                    http_status=response.status_code,
                )
            return response.text if not raw else {"data": response.text}

        code = envelope.get("code")
        if code != _SUCCESS_CODE:
            raise APIError(
                envelope.get("msg") or f"Request failed with code {code}",
                api_code=code,
                http_status=response.status_code,
            )

        return envelope if raw else envelope.get("data")

    @staticmethod
    def _parse_envelope(response: requests.Response) -> Optional[dict[str, Any]]:
        """Parse a JSON ``Result`` envelope, or None for non-envelope bodies."""
        content_type = response.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        if isinstance(body, dict) and "code" in body:
            return body
        return None

    # ── Convenience verbs ───────────────────────────────────────────────

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self.request("DELETE", path, **kwargs)

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "DolphinSchedulerClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def _drop_none(mapping: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Remove keys whose value is None so we never send empty params."""
    if mapping is None:
        return None
    return {k: v for k, v in mapping.items() if v is not None}
