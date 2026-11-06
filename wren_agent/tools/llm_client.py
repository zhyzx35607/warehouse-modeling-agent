"""LLM client factory for the Wren Modeling Agent.

Creates LangChain ChatModel instances based on config.
Supports OpenAI-compatible APIs (DashScope, DeepSeek, etc.).

Usage:
    client = get_llm_client()
    response = await client.ainvoke(messages)
"""

from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel

from wren_agent.config import settings
from wren_agent.tools.json_utils import extract_json_from_llm_response

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _create_chat_model() -> BaseChatModel:
    """Create the LangChain ChatModel based on config provider.

    Returns a cached singleton — call get_llm_client() instead.
    """
    provider = settings.llm.provider

    if provider in ("openai", "deepseek", "custom"):
        from langchain_openai import ChatOpenAI
        model = ChatOpenAI(
            model=settings.llm.model,
            api_key=settings.llm.api_key,
            base_url=settings.llm.api_base,
            temperature=settings.llm.temperature,
            max_tokens=settings.llm.max_tokens,
            timeout=settings.llm.request_timeout,
            max_retries=settings.llm.max_retries,
        )
        logger.info(f"LLM: {provider}/{settings.llm.model} @ {settings.llm.api_base}")
        return model

    elif provider == "ollama":
        from langchain_ollama import ChatOllama
        model = ChatOllama(
            model=settings.llm.model,
            base_url=settings.llm.api_base or "http://localhost:11434",
            temperature=settings.llm.temperature,
        )
        logger.info(f"LLM: ollama/{settings.llm.model}")
        return model

    else:
        raise ValueError(f"Unsupported LLM provider: {provider}")


class LLMClient:
    """Async-friendly wrapper around LangChain ChatModel.

    Provides typed methods for structured generation and plain chat.
    """

    def __init__(self):
        self._model = _create_chat_model()

    @property
    def model(self) -> BaseChatModel:
        return self._model

    async def chat(
        self,
        system_prompt: str,
        user_message: str,
        *,
        token_kind: str | None = None,
        **kwargs,
    ) -> str:
        """Send a system + user message and get a text response.

        ``token_kind`` selects a per-call max_tokens budget from LLMSettings
        (design/review/grill/report/semantic/repair) unless ``max_tokens`` is
        already provided in kwargs.
        """
        if "max_tokens" not in kwargs and token_kind:
            kwargs["max_tokens"] = settings.llm.tokens_for(token_kind)
        t0 = time.monotonic()
        messages: list = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_message),
        ]
        response = await self._ainvoke_stream(messages, **kwargs)
        duration_ms = (time.monotonic() - t0) * 1000

        # Emit thought_stream event for real-time UI
        self._emit_thought(
            prompt_summary=user_message[:300],
            response_summary=response[:300],
            duration_ms=duration_ms,
        )

        # Persist the full conversation for skill optimization / eval corpus
        self._persist_trace(
            system_prompt=system_prompt,
            user_message=user_message,
            response=response,
            duration_ms=duration_ms,
        )

        return response

    async def _ainvoke_stream(self, messages: list, **kwargs) -> str:
        """Invoke the model, streaming when the endpoint supports it.

        Streaming turns the per-request timeout into a per-chunk read
        timeout: a slow-but-progressing generation (large design JSON) no
        longer dies at `request_timeout` seconds and burns retries (task
        a659788b: a 5-table batch took ~9 min; previously every such call hit
        the 300s ceiling and the 15-min timeout-retry loop). Falls back to a
        plain invoke when the endpoint rejects streaming.
        """
        chunks: list[str] = []
        try:
            async for chunk in self._model.astream(messages, **kwargs):
                content = getattr(chunk, "content", None)
                if content:
                    chunks.append(str(content))
        except Exception:
            if chunks:
                raise  # mid-stream failure — let the caller retry
            # The endpoint rejected the request before producing any chunk.
            # One common cause is an unsupported `response_format` (e.g. a
            # proxy that does not implement json_object mode) — retry the
            # stream once without it before falling back to a plain invoke.
            if "response_format" in kwargs:
                kwargs.pop("response_format")
                try:
                    async for chunk in self._model.astream(messages, **kwargs):
                        content = getattr(chunk, "content", None)
                        if content:
                            chunks.append(str(content))
                    if chunks:
                        return "".join(chunks)
                except Exception:
                    pass
            response = await self._model.ainvoke(messages, **kwargs)
            return str(response.content)
        return "".join(chunks)

    async def chat_with_messages(
        self,
        messages: list[BaseMessage],
        **kwargs,
    ) -> str:
        """Send a list of messages and get a text response."""
        t0 = time.monotonic()
        response = await self._ainvoke_stream(messages, **kwargs)
        duration_ms = (time.monotonic() - t0) * 1000

        # Emit thought_stream event
        last_user = ""
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                last_user = str(msg.content)[:300]
                break
        self._emit_thought(
            prompt_summary=last_user,
            response_summary=response[:300],
            duration_ms=duration_ms,
        )

        # Persist the full conversation — system prompt separated, the rest
        # serialized as role-tagged turns
        system_parts = [str(m.content) for m in messages if isinstance(m, SystemMessage)]
        turns = [
            {"role": m.__class__.__name__.replace("Message", "").lower(), "content": str(m.content)}
            for m in messages if not isinstance(m, SystemMessage)
        ]
        import json as _json
        self._persist_trace(
            system_prompt="\n\n".join(system_parts),
            user_message=_json.dumps(turns, ensure_ascii=False),
            response=response,
            duration_ms=duration_ms,
        )

        return response

    async def generate_structured(
        self,
        system_prompt: str,
        user_message: str,
        output_format_desc: str,
        *,
        token_kind: str | None = None,
        **kwargs,
    ) -> str:
        """Generate a response in a specific format (JSON, SQL, YAML, etc.).

        The system_prompt should include format instructions.
        Returns raw text — caller parses with the appropriate parser.
        """
        full_system = f"{system_prompt}\n\n{output_format_desc}"
        return await self.chat(
            full_system, user_message, token_kind=token_kind, **kwargs,
        )

    async def generate_sql(
        self,
        system_prompt: str,
        user_message: str,
        *,
        token_kind: str | None = "design",
        **kwargs,
    ) -> str:
        """Generate SQL with formatting guard.

        Extracts SQL from markdown code blocks if present.
        """
        response = await self.generate_structured(
            system_prompt=system_prompt,
            user_message=user_message,
            output_format_desc="""OUTPUT FORMAT:
Return ONLY the SQL statement(s), wrapped in ```sql code blocks.
Each SQL statement must be valid and runnable.
Example:
```sql
CREATE TABLE IF NOT EXISTS ...
```""",
            token_kind=token_kind,
            **kwargs,
        )
        return response

    async def generate_json(
        self,
        system_prompt: str,
        user_message: str,
        *,
        token_kind: str | None = None,
        **kwargs,
    ) -> str:
        """Generate JSON output.

        When ``settings.llm.json_object_mode`` is on, the request carries
        ``response_format={"type": "json_object"}`` — the endpoint then
        guarantees a parseable JSON object instead of prose (which the callers'
        regex extraction silently tolerated until a failure elsewhere). Endpoints
        that reject the format are handled by ``_ainvoke_stream``'s fallback.
        """
        if settings.llm.json_object_mode and "response_format" not in kwargs:
            kwargs["response_format"] = {"type": "json_object"}
        return await self.generate_structured(
            system_prompt=system_prompt,
            user_message=user_message,
            output_format_desc="""OUTPUT FORMAT:
Return a valid JSON object. No markdown, no explanation.
The response must be parseable by json.loads().""",
            token_kind=token_kind,
            **kwargs,
        )

    async def generate_typed_json(
        self,
        system_prompt: str,
        user_message: str,
        schema: type[BaseModel],
        *,
        token_kind: str | None = None,
        regenerate: bool = True,
        **kwargs,
    ) -> BaseModel:
        """Generate JSON validated against a pydantic ``schema``.

        The response is extracted and validated with ``schema.model_validate``.
        When the first response fails extraction or schema validation, ONE
        cheap regeneration is attempted with the validation errors appended to
        the user message (a single extra call — orders of magnitude cheaper
        than a full ``design_revise`` round). Pass ``regenerate=False`` for
        strict single-shot behavior.
        """
        response = await self.generate_json(
            system_prompt, user_message, token_kind=token_kind, **kwargs,
        )
        try:
            return schema.model_validate(
                extract_json_from_llm_response(response)
            )
        except Exception as first_err:
            if not regenerate:
                raise
            logger.warning(
                "generate_typed_json: response failed validation — "
                "regenerating once with error feedback: %s", str(first_err)[:300],
            )
            feedback = (
                f"\n\nIMPORTANT: your previous response failed validation and "
                f"was NOT accepted. Return ONLY valid JSON matching the "
                f"required schema exactly.\nValidation errors: {first_err}"
            )
            response = await self.generate_json(
                system_prompt,
                user_message + feedback,
                token_kind=token_kind,
                **kwargs,
            )
            return schema.model_validate(
                extract_json_from_llm_response(response)
            )


    @staticmethod
    def _persist_trace(
        system_prompt: str,
        user_message: str,
        response: str,
        duration_ms: float = 0,
    ) -> None:
        """Persist the full LLM conversation to PG (fire-and-forget).

        Uses contextvars to discover task_id, node, and injected skills.
        Never blocks or breaks the agent.
        """
        try:
            import asyncio

            from wren_agent.tools.llm_context import (
                get_current_node,
                get_current_skills,
                get_current_task_id,
            )
            from wren_agent.tools.pg_store import persist_llm_trace

            task_id = get_current_task_id()
            if not task_id:
                return

            loop = asyncio.get_running_loop()
            loop.create_task(persist_llm_trace(
                task_id=task_id,
                node=get_current_node(),
                skills=get_current_skills(),
                system_prompt=system_prompt,
                user_message=user_message,
                response=response,
                duration_ms=round(duration_ms, 1),
            ))
        except Exception:
            pass

    @staticmethod
    def _emit_thought(
        prompt_summary: str,
        response_summary: str,
        duration_ms: float = 0,
    ) -> None:
        """Emit a thought_stream event via WebSocket broadcast.

        Uses contextvars to discover task_id and node_name.
        Fire-and-forget — never break the agent for tracing.
        """
        try:
            from wren_agent.tools.llm_context import (
                get_current_node,
                get_current_task_id,
            )

            task_id = get_current_task_id()
            node = get_current_node()

            if task_id:
                _broadcast_thought(task_id, node, prompt_summary, response_summary, duration_ms)
        except Exception:
            pass


# ── Thought broadcast helper ──────────────────────────────────────


def _broadcast_thought(
    task_id: str,
    node: str,
    prompt_summary: str,
    response_summary: str,
    duration_ms: float = 0,
) -> None:
    """Broadcast thought_stream event directly to WebSocket."""
    import asyncio

    payload = {
        "node": node,
        "prompt_summary": prompt_summary[:300],
        "response_summary": response_summary[:300],
        "duration_ms": round(duration_ms, 1),
    }

    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():

            async def _send():
                try:
                    from wren_agent.api.websocket import broadcast_to_task
                    await broadcast_to_task(task_id, "thought_stream", payload)
                except Exception:
                    pass

            loop.create_task(_send())
    except RuntimeError:
        pass


# ── Singleton ───────────────────────────────────────────────────

_llm_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """Get or create the LLM client singleton."""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client


# ── Prompt helpers ──────────────────────────────────────────────


def load_skill_prompt(skill_name: str) -> str:
    """Load a skill's content as a system prompt fragment."""
    from wren_agent.skills import get_skill_prompt
    return get_skill_prompt(skill_name)


def build_system_prompt(skill_names: list[str], extra_context: str = "") -> str:
    """Build a composite system prompt from multiple skills.

    Args:
        skill_names: List of skill file names (without .md extension)
        extra_context: Additional context to prepend (task state, etc.)
    """
    parts = []

    if extra_context:
        parts.append(extra_context)

    for name in skill_names:
        prompt = load_skill_prompt(name)
        if prompt:
            parts.append(prompt)

    # Record which skills shaped this prompt — attributed in llm_trace
    try:
        from wren_agent.tools.llm_context import set_current_skills
        set_current_skills(skill_names)
    except Exception:
        pass

    return "\n\n".join(parts)
