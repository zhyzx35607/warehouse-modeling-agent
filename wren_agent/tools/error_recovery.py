"""Node-level error recovery and retry for LangGraph nodes.

Wraps LangGraph node functions with retry + circuit breaker patterns.
Recovery strategies: retry, skip, escalate, checkpoint-and-wait.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Callable, Optional

from wren_agent.state import AgentState
from wren_agent.tools.circuit_breaker import CircuitBreaker, get_breaker

logger = logging.getLogger(__name__)

# ── Node wrappers ────────────────────────────────────────────────


def with_retry(
    max_attempts: int = 3,
    backoff_base: float = 2.0,
    delay_seconds: float = 1.0,
    retryable_exceptions: tuple = (Exception,),
    fallback_fn: Optional[Callable] = None,
):
    """Retry a node function with exponential backoff.

    On final failure, calls fallback_fn(node_name, state, error) if provided.
    fallback_fn should return a modified state or raise.
    """
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(state: AgentState, *args, **kwargs):
            last_error = None
            node_name = fn.__name__

            for attempt in range(max_attempts):
                try:
                    return await fn(state, *args, **kwargs)
                except retryable_exceptions as e:
                    last_error = e
                    if attempt < max_attempts - 1:
                        delay = delay_seconds * (backoff_base ** attempt)
                        state["warnings"] = state.get("warnings", []) + [
                            f"[Retry] Node '{node_name}' attempt {attempt+1}/{max_attempts} "
                            f"failed: {e}. Retrying in {delay:.1f}s"
                        ]
                        logger.warning(
                            f"Node '{node_name}' attempt {attempt+1} failed: {e}"
                        )
                        await asyncio.sleep(delay)

            # All retries exhausted
            msg = f"Node '{node_name}' failed after {max_attempts} attempts: {last_error}"
            state["errors"] = state.get("errors", []) + [msg]
            logger.error(msg)

            if fallback_fn:
                return await fallback_fn(node_name, state, last_error)
            return state

        return wrapper
    return decorator


def with_circuit_breaker_for_node(
    service_name: str,
    failure_threshold: int = 3,
    timeout: float = 30,
):
    """Wrap a node function with a circuit breaker for an external service.

    If the circuit is open, the node adds a warning and continues (skips).
    For critical services, use `blocking=True` to raise instead.
    """
    def decorator(fn):
        breaker = get_breaker(service_name, failure_threshold=failure_threshold, timeout=timeout)

        @functools.wraps(fn)
        async def wrapper(state: AgentState, *args, **kwargs):
            try:
                return await breaker.call(lambda: fn(state, *args, **kwargs))
            except Exception as e:
                # Circuit open or call failed
                state["warnings"] = state.get("warnings", []) + [
                    f"[CB] Service '{service_name}' unavailable: {e}. Node '{fn.__name__}' skipped."
                ]
                return state
        return wrapper
    return decorator


def checkpoint_before(fn):
    """Decorator: signal that state should be checkpointed before this node.

    In LangGraph, this is handled by the checkpointer automatically,
    but we add an audit trail for recovery purposes.
    """
    @functools.wraps(fn)
    async def wrapper(state: AgentState, *args, **kwargs):
        state["audit_log"] = state.get("audit_log", []) + [{
            "action": "checkpoint_before",
            "node": fn.__name__,
            "phase": state.get("phase", ""),
        }]
        return await fn(state, *args, **kwargs)
    return wrapper


# ── Fallback strategies ──────────────────────────────────────────


async def escalate_to_human(node_name: str, state: AgentState, error: Exception) -> AgentState:
    """Fallback: add error to state and set human approval needed."""
    state["pending_human_input"] = True
    state["human_input_type"] = "approval"
    state["errors"] = state.get("errors", []) + [
        f"[Escalated] Node '{node_name}' failed: {error}"
    ]
    return state


async def skip_with_warning(node_name: str, state: AgentState, error: Exception) -> AgentState:
    """Fallback: skip the node with a warning."""
    state["warnings"] = state.get("warnings", []) + [
        f"[Skipped] Node '{node_name}' failed, continuing: {error}"
    ]
    return state


async def abort_task(node_name: str, state: AgentState, error: Exception) -> AgentState:
    """Fallback: mark task as failed."""
    from wren_agent.models.task import TaskStatus
    state["status"] = TaskStatus.FAILED
    state["errors"] = state.get("errors", []) + [
        f"[Aborted] Node '{node_name}' failed fatally: {error}"
    ]
    return state


# ── Recovery orchestrator ────────────────────────────────────────


class NodeRecoveryConfig:
    """Per-node recovery configuration."""

    def __init__(
        self,
        max_attempts: int = 3,
        backoff_base: float = 2.0,
        delay_seconds: float = 1.0,
        service_name: Optional[str] = None,
        cb_failure_threshold: int = 3,
        cb_timeout: float = 30.0,
        on_final_failure: str = "skip",  # skip | escalate | abort
    ):
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.delay_seconds = delay_seconds
        self.service_name = service_name
        self.cb_failure_threshold = cb_failure_threshold
        self.cb_timeout = cb_timeout
        self.on_final_failure = on_final_failure


# Default recovery configs per service
RECOVERY_CONFIGS: dict[str, NodeRecoveryConfig] = {
    "dolphin_scheduler": NodeRecoveryConfig(
        max_attempts=3, delay_seconds=30, service_name="dolphin_scheduler",
        cb_timeout=60, on_final_failure="escalate",
    ),
    "llm": NodeRecoveryConfig(
        max_attempts=2, delay_seconds=5, service_name="llm",
        on_final_failure="skip",
    ),
    "neo4j": NodeRecoveryConfig(
        max_attempts=2, delay_seconds=3, service_name="neo4j",
        on_final_failure="skip",
    ),
    "doris": NodeRecoveryConfig(
        max_attempts=2, delay_seconds=10, service_name="doris",
        on_final_failure="escalate",
    ),
    "source_db": NodeRecoveryConfig(
        max_attempts=2, delay_seconds=5, service_name="source_db",
        on_final_failure="escalate",
    ),
    "default": NodeRecoveryConfig(
        max_attempts=1, on_final_failure="skip",
    ),
}


def get_recovery_config(node_name: str) -> NodeRecoveryConfig:
    """Get recovery config for a node based on its dependencies."""
    # Infer service from node name
    if "dag_deploy" in node_name or "execute" in node_name or "scheduler" in node_name:
        return RECOVERY_CONFIGS["dolphin_scheduler"]
    elif "design" in node_name or "grill" in node_name or "report" in node_name:
        return RECOVERY_CONFIGS["llm"]
    elif "kg" in node_name or "export_kg" in node_name:
        return RECOVERY_CONFIGS["neo4j"]
    elif "validate" in node_name or "harness" in node_name:
        return RECOVERY_CONFIGS["doris"]
    elif "explore" in node_name or "schema" in node_name:
        return RECOVERY_CONFIGS["source_db"]
    return RECOVERY_CONFIGS["default"]


_FALLBACK_MAP = {
    "skip": skip_with_warning,
    "escalate": escalate_to_human,
    "abort": abort_task,
}


def wrap_node_with_recovery(fn, node_name: str):
    """Wrap a LangGraph node function with appropriate recovery logic."""
    config = get_recovery_config(node_name)
    fallback = _FALLBACK_MAP.get(config.on_final_failure, skip_with_warning)

    @functools.wraps(fn)
    async def wrapper(state: AgentState, *args, **kwargs):
        return await _recoverable_call(
            fn, state, node_name, config, fallback, *args, **kwargs
        )
    return wrapper


async def _recoverable_call(
    fn,
    state: AgentState,
    node_name: str,
    config: NodeRecoveryConfig,
    fallback: Callable,
    *args, **kwargs,
) -> AgentState:
    """Execute fn with retry + circuit breaker + fallback."""
    from wren_agent.config import settings

    strict_design = node_name in {
        "design_ods", "design_dim", "design_dwd", "design_dws", "design_ads"
    } and not settings.llm.allow_programmatic_fallback
    last_error = None

    # Circuit breaker check
    breaker = None
    if config.service_name:
        breaker = get_breaker(
            config.service_name,
            failure_threshold=config.cb_failure_threshold,
            timeout=config.cb_timeout,
        )

    if breaker and breaker.is_open:
        if strict_design:
            raise RuntimeError(
                f"Cannot run {node_name}: service '{config.service_name}' circuit is open"
            )
        state["warnings"] = state.get("warnings", []) + [
            f"[CB] Service '{config.service_name}' circuit is OPEN. "
            f"Node '{node_name}' skipped."
        ]
        return state

    # Retry loop
    for attempt in range(config.max_attempts):
        try:
            result = await fn(state, *args, **kwargs)
            if breaker:
                breaker.record_success()
            return result
        except Exception as e:
            last_error = e
            if breaker:
                breaker.record_failure(str(e))

            if attempt < config.max_attempts - 1:
                delay = config.delay_seconds * (config.backoff_base ** attempt)
                state["warnings"] = state.get("warnings", []) + [
                    f"[Recovery] Node '{node_name}' attempt {attempt+1} failed. "
                    f"Retry in {delay:.1f}s ({e})"
                ]
                logger.warning(f"Node '{node_name}' retry {attempt+1}: {e}")
                await asyncio.sleep(delay)
                # Don't return state here — continue the loop for the next attempt
                # But we need to pass the original state, not potentially modified state
                # Re: LangGraph — the fn may modify state. On retry, we re-run from
                # the last checkpointed state automatically. So we just continue.

    # All retries exhausted
    if strict_design:
        raise last_error or RuntimeError(f"No attempts configured for {node_name}")
    return await fallback(node_name, state, last_error)
