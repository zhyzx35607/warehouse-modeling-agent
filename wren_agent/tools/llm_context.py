"""LLM call context via contextvars — thread-safe task/node tracking.

Allows LLM client methods to discover the current task_id and node_name
without threading them through every function signature.

Set by node_tracer.py in each node wrapper, read by llm_client.py
when emitting thought_stream events.
"""

from __future__ import annotations

import contextvars

_current_task_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_task_id", default=""
)
_current_node: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_node", default=""
)
_current_skills: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "current_skills", default=()
)


def set_current_task_id(task_id: str) -> None:
    """Set the current task_id for the async context."""
    _current_task_id.set(task_id)


def get_current_task_id() -> str:
    """Get the current task_id from the async context."""
    return _current_task_id.get()


def set_current_node(node_name: str) -> None:
    """Set the current node name for the async context."""
    _current_node.set(node_name)


def get_current_node() -> str:
    """Get the current node name from the async context."""
    return _current_node.get()


def set_current_skills(skill_names: list[str] | tuple[str, ...]) -> None:
    """Set the skill names injected into the current system prompt.

    Called by build_system_prompt, read by the LLM trace persister so each
    llm_trace row can be attributed to the skills that shaped the prompt.
    """
    _current_skills.set(tuple(skill_names))


def get_current_skills() -> tuple[str, ...]:
    """Get the skill names for the async context."""
    return _current_skills.get()
