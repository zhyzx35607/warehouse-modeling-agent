"""Tests for tool modules — SM4, error classification, type mapping, events."""

import pytest
import json

from wren_agent.models.task import ErrorClassification, ErrorType


class TestSM4Crypto:
    def test_encrypt_decrypt_roundtrip(self):
        from wren_agent.tools.sm4_crypto import sm4_encrypt, sm4_decrypt
        original = "MySecretPassword123!"
        encrypted = sm4_encrypt(original)
        decrypted = sm4_decrypt(encrypted)
        assert decrypted == original

    def test_encrypt_produces_different_ciphertext(self):
        from wren_agent.tools.sm4_crypto import sm4_encrypt
        c1 = sm4_encrypt("password")
        c2 = sm4_encrypt("password")
        # Different IVs produce different ciphertexts even for same plaintext
        assert c1 != c2

    def test_encrypt_empty_string(self):
        from wren_agent.tools.sm4_crypto import sm4_encrypt, sm4_decrypt
        encrypted = sm4_encrypt("")
        assert encrypted == ""
        assert sm4_decrypt("") == ""

    def test_decrypt_empty_string(self):
        from wren_agent.tools.sm4_crypto import sm4_decrypt
        assert sm4_decrypt("") == ""

    def test_decrypt_invalid_data_raises(self):
        from wren_agent.tools.sm4_crypto import sm4_decrypt
        with pytest.raises(ValueError, match="Decryption failed"):
            sm4_decrypt("invalid-base64!!")

    def test_chinese_chars(self):
        from wren_agent.tools.sm4_crypto import sm4_encrypt, sm4_decrypt
        original = "我的密码123!@#"
        encrypted = sm4_encrypt(original)
        decrypted = sm4_decrypt(encrypted)
        assert decrypted == original


class TestErrorClassification:
    def test_transient_error(self):
        from wren_agent.tools.dolphin_scheduler import classify_execution_error
        result = classify_execution_error(
            "ti-1", "Connection refused, timeout after 30s"
        )
        assert result.error_type == ErrorType.TRANSIENT
        assert result.action == "retry"

    def test_resource_error(self):
        from wren_agent.tools.dolphin_scheduler import classify_execution_error
        result = classify_execution_error(
            "ti-2", "Disk full: no space left on device"
        )
        assert result.error_type == ErrorType.RESOURCE
        assert result.action == "wait"

    def test_room_table_name_not_misclassified_as_resource(self):
        """Bare "oom" is a substring of table names (dws_order_subscription_monthly).
        A log mentioning product-level tables must NOT classify as RESOURCE —
        task 55f47744 hung in an infinite redesign loop on this false positive."""
        from wren_agent.tools.dolphin_scheduler import classify_execution_error
        log = (
            "execute sql error: errCode = 2, detailMessage = "
            "(127.0.0.1)[INVALID_ARGUMENT]parse number fail, string: ''\n"
            "INSERT INTO dws_order_subscription_monthly (room_id, snapshot_month, "
            "room_name, monthly_amount_receivable) SELECT ... FROM dwd_room_base"
        )
        result = classify_execution_error("ti-2b", log)
        # "parse number fail" wins (data quality) — never RESOURCE from "oom"
        assert result.error_type == ErrorType.LOGIC
        assert "NULLIF" in result.summary

    def test_data_error(self):
        from wren_agent.tools.dolphin_scheduler import classify_execution_error
        result = classify_execution_error(
            "ti-3", "Duplicate entry '123' for key 'PRIMARY'"
        )
        assert result.error_type == ErrorType.DATA
        assert result.action == "escalate"

    def test_config_error(self):
        from wren_agent.tools.dolphin_scheduler import classify_execution_error
        result = classify_execution_error(
            "ti-4", "Access denied for user 'root'"
        )
        assert result.error_type == ErrorType.CONFIG
        assert result.action == "escalate"

    def test_unknown_column_is_agent_fixable_logic_error(self):
        """Unknown column = the agent's SQL referenced a non-existent column —
        LOGIC (design_revise repairs it), not an upstream schema change."""
        from wren_agent.tools.dolphin_scheduler import classify_execution_error
        result = classify_execution_error(
            "ti-5", "Unknown column 'deleted_at' in 'field list'"
        )
        assert result.error_type == ErrorType.LOGIC
        assert result.action == "escalate"

    def test_dependency_error(self):
        from wren_agent.tools.dolphin_scheduler import classify_execution_error
        result = classify_execution_error(
            "ti-5b", "Table 'dw.fct_gone' not found"
        )
        assert result.error_type == ErrorType.DEPENDENCY
        assert result.action == "escalate"

    def test_logic_error_default(self):
        from wren_agent.tools.dolphin_scheduler import classify_execution_error
        result = classify_execution_error(
            "ti-6", "Something completely unexpected happened here"
        )
        assert result.error_type == ErrorType.LOGIC  # Default catch-all
        assert result.action == "escalate"

    def test_transient_retry_count_sets_action(self):
        from wren_agent.tools.dolphin_scheduler import classify_execution_error
        result = classify_execution_error(
            "ti-1", "connection timeout", retry_count=3
        )
        assert result.error_type == ErrorType.TRANSIENT
        # retry_count >= 3 should escalate
        assert result.action == "escalate"


class TestTypeMapping:
    def test_mysql_types(self):
        from wren_agent.tools.db_explorer import map_type_to_doris
        tests = [
            ("bigint(20)", "BIGINT"),
            ("varchar(255)", "VARCHAR"),
            ("decimal(19,4)", "DECIMAL"),
            ("int(11)", "INT"),
            ("datetime", "DATETIME"),
            ("text", "STRING"),
            ("tinyint(1)", "TINYINT"),
            ("geometry", None),
        ]
        for src, expected in tests:
            doris_type, unsupported = map_type_to_doris(src, "mysql")
            assert doris_type == expected, f"{src} -> {doris_type} != {expected}"

    def test_postgresql_types(self):
        from wren_agent.tools.db_explorer import map_type_to_doris
        tests = [
            ("INTEGER", "INT"),
            ("BIGINT", "BIGINT"),
            ("VARCHAR", "VARCHAR"),
            ("TEXT", "STRING"),
            ("BOOLEAN", "BOOLEAN"),
            ("UUID", "VARCHAR(36)"),
            ("JSONB", "JSON"),
        ]
        for src, expected in tests:
            doris_type, _ = map_type_to_doris(src, "postgresql")
            assert doris_type == expected, f"{src} -> {doris_type} != {expected}"

    def test_unknown_type_defaults_to_string(self):
        from wren_agent.tools.db_explorer import map_type_to_doris
        doris_type, unsupported = map_type_to_doris("MYSTERY_TYPE", "mysql")
        assert doris_type == "STRING"

    def test_dm_types(self):
        from wren_agent.tools.db_explorer import map_type_to_doris
        doris_type, _ = map_type_to_doris("INT", "dm")
        assert doris_type == "INT"


class TestEventEmitter:
    def test_events_constants(self):
        from wren_agent.tools.event_emitter import Events
        assert Events.STATUS_CHANGE == "task_status_change"
        assert Events.PHASE_CHANGE == "phase_change"
        assert Events.CLARIFICATION == "clarification_added"
        assert Events.APPROVAL == "approval_added"
        assert Events.VALIDATION == "validation_result"
        assert Events.ERROR == "error"
        assert Events.WARNING == "warning"

    def test_emit_without_loop_does_not_crash(self):
        """emit() should not crash when no event loop is running."""
        from wren_agent.state import create_initial_state
        from wren_agent.tools.event_emitter import emit, emit_phase, emit_status
        state = create_initial_state("t1", "goal", [], {})
        # Should not raise
        emit(state, "test", {"key": "value"})
        emit_phase(state, "init")
        emit_status(state, "new", "old")

    def test_emit_creates_background_tasks(self):
        """emit() with running loop should create background tasks."""
        import asyncio
        from wren_agent.state import create_initial_state
        from wren_agent.tools.event_emitter import emit

        async def _test():
            state = create_initial_state("t1", "goal", [], {})
            emit(state, "test_event", {"key": "val"})
            # Give the background task a moment
            await asyncio.sleep(0.01)
            # No assertion needed — just that it doesn't crash

        asyncio.run(_test())


class TestSkillLoader:
    def test_load_grill_skill(self):
        from wren_agent.skills import load_skill
        skill = load_skill("grill-expert")
        assert skill["name"] == "grill-expert"
        assert "human experts" in skill["content"].lower()

    def test_load_all_skills(self):
        from wren_agent.skills import load_all_skills
        skills = load_all_skills()
        assert len(skills) >= 8  # At least 8 skills defined

    def test_get_skill_prompt_format(self):
        from wren_agent.skills import get_skill_prompt
        prompt = get_skill_prompt("explore-schema")
        assert prompt.startswith("## Skill: explore-schema")
        assert "Phase 1" in prompt

    def test_load_nonexistent_skill(self):
        from wren_agent.skills import load_skill, get_skill_prompt
        assert load_skill("nonexistent") == {}
        assert get_skill_prompt("nonexistent") == ""


class TestLLMClient:
    def test_get_llm_client_is_singleton(self):
        from wren_agent.tools.llm_client import get_llm_client
        c1 = get_llm_client()
        c2 = get_llm_client()
        assert c1 is c2


class TestLLMContext:
    def test_skills_context_roundtrip(self):
        from wren_agent.tools.llm_context import get_current_skills, set_current_skills
        set_current_skills(["grill-expert", "explore-schema"])
        assert get_current_skills() == ("grill-expert", "explore-schema")
        set_current_skills(())
        assert get_current_skills() == ()

    def test_build_system_prompt_records_skills(self):
        """build_system_prompt must attribute injected skills for llm_trace."""
        from wren_agent.tools.llm_client import build_system_prompt
        from wren_agent.tools.llm_context import get_current_skills
        prompt = build_system_prompt(["grill-expert"], extra_context="ctx")
        assert prompt.startswith("ctx")
        assert get_current_skills() == ("grill-expert",)

    def test_persist_trace_without_task_id_is_noop(self):
        """No task context (e.g. unit tests, CLI) → trace persister must not blow up."""
        from wren_agent.tools.llm_client import LLMClient
        from wren_agent.tools.llm_context import set_current_task_id
        set_current_task_id("")
        # Must not raise even without a running loop or PG pool
        LLMClient._persist_trace("sys", "user", "resp", 12.5)
