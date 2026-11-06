"""Central redaction helpers for persisted traces and API responses."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit


REDACTED = "[REDACTED]"

_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:password|passwd|pwd|secret|token|api_key|apikey|credential|"
    r"authorization|auth_header|private_key|access_key|sm4_key|dsn|"
    r"connection_string)(?:_|$)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_URI_PASSWORD = re.compile(r"(?P<prefix>\b[a-z][a-z0-9+.-]*://[^\s/:@]+:)[^\s@/]+@", re.I)
_ASSIGNMENT_SECRET = re.compile(
    r"(?i)(?P<key>password|passwd|pwd|secret|token|api[_-]?key|credential|"
    r"private[_-]?key|access[_-]?key|sm4[_-]?key)"
    r"(?P<sep>\s*[=:]\s*)(?P<quote>['\"]?)(?P<value>[^\s,;'\"}\]]+)(?P=quote)"
)
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])")
_CN_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_CN_ID = re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)")
# 16-19 digit runs: bank cards AND snowflake IDs (19 digits) both match — do
# NOT replace with a "[REDACTED_BANK_CARD]" label. That label made diagnosis
# impossible and misled the LLM error classifier into "bank card" territory
# when the real value was a comma-joined snowflake ID (task 55f47744). Keep
# the first 4 digits as a hint, mask the rest — real cards stay protected,
# numeric IDs stay recognizable as numbers.
_BANK_CARD = re.compile(r"(?<!\d)(\d{4})\d{12,15}(?!\d)")


def _configured_secret_values() -> list[str]:
    """Collect configured secrets without logging or persisting them."""
    try:
        from wren_agent.config import settings

        candidates = [
            settings.ds.api_key,
            settings.pg.dsn,
            settings.neo4j.password,
            settings.doris.password,
            settings.llm.api_key,
            settings.server.api_key,
            settings.sm4.key,
            settings.secret.vault_token,
        ]
        try:
            parsed_password = urlsplit(settings.pg.dsn).password
            if parsed_password:
                candidates.append(parsed_password)
        except ValueError:
            pass
        return sorted(
            {str(value) for value in candidates if value and len(str(value)) >= 4},
            key=len,
            reverse=True,
        )
    except Exception:
        return []


def redact_text(value: str) -> str:
    """Redact configured secrets, credential assignments, and common PII."""
    text = str(value)
    for secret in _configured_secret_values():
        text = text.replace(secret, REDACTED)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _URI_PASSWORD.sub(r"\g<prefix>[REDACTED]@", text)
    text = _ASSIGNMENT_SECRET.sub(
        lambda match: f"{match.group('key')}{match.group('sep')}{REDACTED}",
        text,
    )
    text = _EMAIL.sub("[REDACTED_EMAIL]", text)
    text = _CN_ID.sub("[REDACTED_ID]", text)
    text = _BANK_CARD.sub(r"\1****", text)
    text = _CN_PHONE.sub("[REDACTED_PHONE]", text)
    return text


def sanitize_for_output(value: Any) -> Any:
    """Recursively produce a JSON-compatible value with secrets removed."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_text(value)
    if hasattr(value, "model_dump"):
        return sanitize_for_output(value.model_dump())
    if hasattr(value, "dict"):
        return sanitize_for_output(value.dict())
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            key_text = str(key)
            sanitized[key] = (
                REDACTED if _SENSITIVE_KEY.search(key_text)
                else sanitize_for_output(item)
            )
        return sanitized
    if isinstance(value, (list, tuple, set)):
        return [sanitize_for_output(item) for item in value]
    return redact_text(str(value))
