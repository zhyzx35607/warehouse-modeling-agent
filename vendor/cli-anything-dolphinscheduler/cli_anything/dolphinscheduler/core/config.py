"""Connection configuration for the DolphinScheduler CLI.

The CLI is a client to a *running* DolphinScheduler API server. Configuration
resolves from three layers, highest priority first:

1. Explicit values passed on the command line (``--url``, ``--token`` ...).
2. Environment variables (``DS_URL``, ``DS_TOKEN``, ``DS_USER`` ...).
3. A JSON config file (``~/.cli-anything-dolphinscheduler/config.json``).

Nothing here talks to the network; this module only assembles a validated
:class:`ClientConfig`. The actual connection lives in :mod:`core.client`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

from .errors import ConfigError

DEFAULT_URL = "http://localhost:12345/dolphinscheduler"
DEFAULT_TIMEOUT = 30.0

CONFIG_DIR = Path.home() / ".cli-anything-dolphinscheduler"
CONFIG_FILE = CONFIG_DIR / "config.json"

_ENV_URL = "DS_URL"
_ENV_TOKEN = "DS_TOKEN"
_ENV_USER = "DS_USER"
_ENV_PASSWORD = "DS_PASSWORD"
_ENV_TIMEOUT = "DS_TIMEOUT"


@dataclass(frozen=True)
class ClientConfig:
    """Immutable connection settings for the API client.

    Attributes:
        url: Base URL including the ``/dolphinscheduler`` context path.
        token: A DolphinScheduler access token, sent in the ``token`` header.
        user: Username, used when logging in to obtain a session cookie.
        password: Password, paired with ``user`` for cookie-based auth.
        timeout: Per-request timeout in seconds.
        verify_tls: Whether to verify TLS certificates for https endpoints.
    """

    url: str = DEFAULT_URL
    token: Optional[str] = None
    user: Optional[str] = None
    password: Optional[str] = None
    timeout: float = DEFAULT_TIMEOUT
    verify_tls: bool = True

    def with_overrides(self, **overrides: Any) -> "ClientConfig":
        """Return a new config with non-None overrides applied (immutable)."""
        clean = {k: v for k, v in overrides.items() if v is not None}
        return replace(self, **clean) if clean else self

    @property
    def base_url(self) -> str:
        """Base URL with any trailing slash removed for clean joining."""
        return self.url.rstrip("/")

    def redacted(self) -> dict[str, Any]:
        """Config as a dict with secrets masked, for safe display/logging."""
        return {
            "url": self.url,
            "token": _mask(self.token),
            "user": self.user,
            "password": _mask(self.password),
            "timeout": self.timeout,
            "verify_tls": self.verify_tls,
        }


def _mask(secret: Optional[str]) -> Optional[str]:
    """Mask a secret so it can be shown without leaking its value."""
    if not secret:
        return None
    if len(secret) <= 4:
        return "****"
    return f"{secret[:2]}****{secret[-2:]}"


def _load_file(path: Path = CONFIG_FILE) -> dict[str, Any]:
    """Load the JSON config file, returning an empty dict when absent."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ConfigError(f"Failed to read config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Config file {path} must contain a JSON object")
    return data


def _from_env() -> dict[str, Any]:
    """Collect config values present in the environment."""
    env: dict[str, Any] = {}
    if os.environ.get(_ENV_URL):
        env["url"] = os.environ[_ENV_URL]
    if os.environ.get(_ENV_TOKEN):
        env["token"] = os.environ[_ENV_TOKEN]
    if os.environ.get(_ENV_USER):
        env["user"] = os.environ[_ENV_USER]
    if os.environ.get(_ENV_PASSWORD):
        env["password"] = os.environ[_ENV_PASSWORD]
    if os.environ.get(_ENV_TIMEOUT):
        env["timeout"] = _parse_timeout(os.environ[_ENV_TIMEOUT])
    return env


def _parse_timeout(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid timeout value: {value!r}") from exc
    if parsed <= 0:
        raise ConfigError("timeout must be a positive number of seconds")
    return parsed


def load_config(
    *,
    url: Optional[str] = None,
    token: Optional[str] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
    timeout: Optional[float] = None,
    verify_tls: Optional[bool] = None,
    config_file: Optional[Path] = None,
) -> ClientConfig:
    """Resolve the effective client config across file, env, and CLI layers.

    CLI arguments win over environment variables, which win over the config
    file, which falls back to built-in defaults.
    """
    file_values = _load_file(config_file or CONFIG_FILE)
    if "timeout" in file_values:
        file_values["timeout"] = _parse_timeout(file_values["timeout"])

    # Keep only recognised keys from the file to avoid silently accepting typos.
    allowed = {"url", "token", "user", "password", "timeout", "verify_tls"}
    unknown = set(file_values) - allowed
    if unknown:
        raise ConfigError(
            f"Unknown keys in config file: {', '.join(sorted(unknown))}"
        )

    config = ClientConfig().with_overrides(**file_values)
    config = config.with_overrides(**_from_env())
    config = config.with_overrides(
        url=url,
        token=token,
        user=user,
        password=password,
        timeout=timeout,
        verify_tls=verify_tls,
    )
    return config


def save_config(config: ClientConfig, *, config_file: Optional[Path] = None) -> Path:
    """Persist config to disk (including secrets) and return the file path.

    The directory is created if needed. Secrets are stored in plaintext, so the
    file is written with owner-only permissions.
    """
    path = config_file or CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "url": config.url,
        "token": config.token,
        "user": config.user,
        "password": config.password,
        "timeout": config.timeout,
        "verify_tls": config.verify_tls,
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        # Non-POSIX filesystems may not support chmod; best effort only.
        pass
    return path
