"""Tests for LLM-based error classification and log excerpt extraction.

LLM classification runs first on failed executions; the keyword classifier
stays as the fallback. These tests cover the LLM path (success + every
fallback branch) and the excerpt helper.
"""

import asyncio
import json

import pytest

from wren_agent.models.task import ErrorClassification, ErrorType

_LLM_IMPORT = "wren_agent.tools.llm_client.get_llm_client"


def _fake_llm(payload):
    class FakeLLM:
        async def generate_json(self, system, user, *, token_kind=None, **kwargs):
            return payload

    return FakeLLM()


class TestErrorExcerptExtraction:
    def test_extracts_error_lines(self):
        from wren_agent.tools.dolphin_scheduler import _extract_error_excerpt
        log = (
            "[INFO] Starting task...\n"
            "[INFO] Connecting to Doris\n"
            "ERROR: errCode = 2, detailMessage = parse number fail\n"
            "[INFO] Rolling back\n"
            "Exception: connection reset\n"
        )
        excerpt = _extract_error_excerpt(log)
        assert "errCode = 2" in excerpt
        assert "parse number fail" in excerpt
        assert "connection reset" in excerpt

    def test_handles_empty_log(self):
        from wren_agent.tools.dolphin_scheduler import _extract_error_excerpt
        assert _extract_error_excerpt("") == "(empty log)"
        assert _extract_error_excerpt("   ") == "(empty log)"

    def test_truncates_long_excerpt(self):
        from wren_agent.tools.dolphin_scheduler import _extract_error_excerpt
        long_log = "ERROR: test\n" * 500
        excerpt = _extract_error_excerpt(long_log, max_chars=500)
        assert len(excerpt) <= 550  # ~500 + truncation marker

    def test_no_pattern_match_returns_tail(self):
        from wren_agent.tools.dolphin_scheduler import _extract_error_excerpt
        log = "\n".join(f"[INFO] line {i}" for i in range(100))
        excerpt = _extract_error_excerpt(log)
        assert "[INFO] line 70" in excerpt  # tail lines included


class TestLLMClassification:
    @pytest.mark.asyncio
    async def test_valid_llm_response_produces_classification(self, monkeypatch):
        from wren_agent.tools.dolphin_scheduler import classify_execution_error_with_llm
        monkeypatch.setattr(
            _LLM_IMPORT,
            lambda: _fake_llm(json.dumps({
                "error_type": "logic",
                "summary": "Doris 解析错误：日期列空串",
                "details": "errCode=2, INVALID_ARGUMENT, parse number fail on empty string",
                "fix_suggestion": "加载 SQL 中加 NULLIF(col, '') / WHERE col != '' 防护",
            })),
        )

        result = await classify_execution_error_with_llm(
            "ti-test", "errCode = 2, detailMessage = parse number fail", 0
        )
        assert result is not None
        assert isinstance(result, ErrorClassification)
        assert result.error_type == ErrorType.LOGIC
        assert result.llm_classified is True
        assert "parse number" in result.details
        assert result.fix_suggestion
        assert result.action == "escalate"

    @pytest.mark.asyncio
    async def test_invalid_json_falls_back(self, monkeypatch):
        from wren_agent.tools.dolphin_scheduler import classify_execution_error_with_llm
        monkeypatch.setattr(
            _LLM_IMPORT,
            lambda: _fake_llm("not valid json at all [just text]"),
        )
        assert await classify_execution_error_with_llm("ti-test", "error log") is None

    @pytest.mark.asyncio
    async def test_invalid_error_type_falls_back(self, monkeypatch):
        from wren_agent.tools.dolphin_scheduler import classify_execution_error_with_llm
        monkeypatch.setattr(
            _LLM_IMPORT,
            lambda: _fake_llm(json.dumps({
                "error_type": "nonexistent_type",
                "summary": "test",
                "details": "test",
                "fix_suggestion": "test",
            })),
        )
        assert await classify_execution_error_with_llm("ti-test", "log") is None

    @pytest.mark.asyncio
    async def test_llm_exception_falls_back(self, monkeypatch):
        from wren_agent.tools.dolphin_scheduler import classify_execution_error_with_llm

        class BrokenLLM:
            async def generate_json(self, *args, **kwargs):
                raise RuntimeError("LLM API down")

        monkeypatch.setattr(_LLM_IMPORT, lambda: BrokenLLM())
        assert await classify_execution_error_with_llm("ti-test", "log") is None

    @pytest.mark.asyncio
    async def test_timeout_falls_back(self, monkeypatch):
        from wren_agent.tools.dolphin_scheduler import classify_execution_error_with_llm

        class SlowLLM:
            async def generate_json(self, *args, **kwargs):
                await asyncio.sleep(5)
                return '{"error_type": "logic"}'

        monkeypatch.setattr(_LLM_IMPORT, lambda: SlowLLM())
        result = await classify_execution_error_with_llm(
            "ti-test", "log", timeout_seconds=0.5
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_transient_retry_action_derivation(self, monkeypatch):
        from wren_agent.tools.dolphin_scheduler import classify_execution_error_with_llm

        def respond():
            return _fake_llm(json.dumps({
                "error_type": "transient",
                "summary": "connection timeout",
                "details": "Network timeout after 30s",
                "fix_suggestion": "重试",
            }))

        monkeypatch.setattr(_LLM_IMPORT, lambda: respond())
        result = await classify_execution_error_with_llm("ti", "timeout", retry_count=0)
        assert result.action == "retry"

        result2 = await classify_execution_error_with_llm("ti", "timeout", retry_count=3)
        assert result2.action == "escalate"

    @pytest.mark.asyncio
    async def test_resource_wait_action(self, monkeypatch):
        from wren_agent.tools.dolphin_scheduler import classify_execution_error_with_llm
        monkeypatch.setattr(
            _LLM_IMPORT,
            lambda: _fake_llm(json.dumps({
                "error_type": "resource",
                "summary": "磁盘空间不足",
                "details": "no space left on device",
                "fix_suggestion": "清理磁盘",
            })),
        )
        result = await classify_execution_error_with_llm("ti", "no space left", 0)
        assert result is not None
        assert result.error_type == ErrorType.RESOURCE
        assert result.action == "wait"

    @pytest.mark.asyncio
    async def test_empty_summary_gets_fallback_text(self, monkeypatch):
        from wren_agent.tools.dolphin_scheduler import classify_execution_error_with_llm
        monkeypatch.setattr(
            _LLM_IMPORT,
            lambda: _fake_llm(json.dumps({
                "error_type": "logic",
                "summary": "",
                "details": "",
                "fix_suggestion": "",
            })),
        )
        result = await classify_execution_error_with_llm("ti", "some log")
        assert result is not None
        assert result.summary == "LLM-classified error"
