"""Sensitive trace persistence and API output redaction tests."""

import importlib
import json

import pytest
from fastapi import HTTPException

from wren_agent.tools.redaction import REDACTED, redact_text, sanitize_for_output


def test_recursive_output_redaction_masks_keys_and_pii(monkeypatch):
    monkeypatch.setattr(
        "wren_agent.tools.redaction._configured_secret_values",
        lambda: ["configured-secret-value"],
    )
    payload = sanitize_for_output({
        "password": "plain-password",
        "connection_string": "postgresql://reader:db-password@db:5432/wren",
        "nested": {
            "api_key": "configured-secret-value",
            "authorization": "Bearer abcdefghijklmnop",
            "message": (
                "Authorization: Bearer abcdefghijklmnop; "
                "email=user@example.com phone=13800000000"
            ),
        },
    })

    assert payload["password"] == REDACTED
    assert payload["connection_string"] == REDACTED
    assert payload["nested"]["api_key"] == REDACTED
    assert payload["nested"]["authorization"] == REDACTED
    serialized = json.dumps(payload)
    assert "configured-secret-value" not in serialized
    assert "abcdefghijklmnop" not in serialized
    assert "user@example.com" not in serialized
    assert "13800000000" not in serialized


def test_text_redaction_masks_dsn_and_assignments(monkeypatch):
    monkeypatch.setattr(
        "wren_agent.tools.redaction._configured_secret_values", lambda: [],
    )
    text = redact_text(
        "postgresql://reader:db-password@db:5432/wren "
        "password=plain-secret token:token-value"
    )

    assert "db-password" not in text
    assert "plain-secret" not in text
    assert "token-value" not in text


def test_long_number_runs_keep_prefix_not_bank_card_label(monkeypatch):
    """16-19 digit runs (bank cards AND 19-digit snowflake IDs) must not be
    replaced with a '[REDACTED_BANK_CARD]' label — that label hid the real
    value shape and misled the LLM classifier (task 55f47744). Keep the
    first 4 digits as a hint and mask the rest."""
    monkeypatch.setattr(
        "wren_agent.tools.redaction._configured_secret_values", lambda: [],
    )
    text = redact_text(
        "parse number fail, string: '2013918595533959170,2013918941366906882' "
        "card=6222021234567890123"
    )

    assert "[REDACTED_BANK_CARD]" not in text
    assert "2013****" in text        # snowflake ID keeps its prefix
    assert "2013918595533959170" not in text  # full value still masked
    assert "6222****" in text        # bank card keeps its prefix too
    assert "6222021234567890123" not in text


@pytest.mark.asyncio
async def test_llm_trace_is_redacted_before_pg_persistence(monkeypatch):
    from wren_agent.tools.pg_store import persist_llm_trace

    captured = []

    class Acquire:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _sql, *args):
            captured.extend(args)

    class Pool:
        def acquire(self):
            return Acquire()

    monkeypatch.setattr(
        "wren_agent.tools.pg_store.get_pg_pool", lambda: Pool(),
    )
    monkeypatch.setattr(
        "wren_agent.tools.redaction._configured_secret_values",
        lambda: ["configured-secret-value"],
    )

    await persist_llm_trace(
        "task-1", "node", [],
        "api_key=configured-secret-value",
        "phone=13800000000",
        "Bearer abcdefghijklmnop",
    )

    persisted = " ".join(str(value) for value in captured)
    assert "configured-secret-value" not in persisted
    assert "13800000000" not in persisted
    assert "abcdefghijklmnop" not in persisted


@pytest.mark.asyncio
async def test_full_llm_trace_access_is_rejected_before_db_access():
    from wren_agent.api.routes import get_llm_traces

    with pytest.raises(HTTPException) as exc:
        await get_llm_traces("task-1", full=True)

    assert exc.value.status_code == 403


def test_wildcard_cors_disables_credentials(monkeypatch):
    import wren_agent.api.app as app_module
    from wren_agent.config import settings

    monkeypatch.setattr(settings.server, "cors_origins", ["*"])
    app_module = importlib.reload(app_module)

    cors = next(
        middleware
        for middleware in app_module.app.user_middleware
        if middleware.cls.__name__ == "CORSMiddleware"
    )
    assert cors.kwargs["allow_origins"] == ["*"]
    assert cors.kwargs["allow_credentials"] is False
