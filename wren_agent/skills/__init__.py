"""Skill loader for the Wren Modeling Agent.

Loads Claude Code format skills (YAML frontmatter + Markdown body)
from the skills/ directory and injects them into LangGraph nodes.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).parent


def parse_yaml_frontmatter(content: str) -> tuple[dict[str, str], str]:
    """Parse YAML frontmatter from a skill file."""
    # Match `---\n...\n---\n`
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return {}, content

    frontmatter = match.group(1)
    body = content[match.end():]

    # Simple YAML parser (key: value only, no nested)
    metadata = {}
    for line in frontmatter.split("\n"):
        line = line.strip()
        if ":" in line and not line.startswith("#"):
            key, _, value = line.partition(":")
            metadata[key.strip()] = value.strip()

    return metadata, body


@lru_cache(maxsize=32)
def _load_skill_cached(name: str) -> tuple[tuple[tuple[str, str], ...], str, str] | None:
    """Cached skill load — returns (metadata items, body, path) or None."""
    skill_path = SKILLS_DIR / f"{name}.md"
    if not skill_path.exists():
        return None

    content = skill_path.read_text(encoding="utf-8")
    metadata, body = parse_yaml_frontmatter(content)
    return tuple(sorted(metadata.items())), body, str(skill_path)


def load_skill(name: str) -> dict[str, Any]:
    """Load a skill by name. Returns {metadata, body}. Cached in-process."""
    cached = _load_skill_cached(name)
    if cached is None:
        logger.warning(f"Skill not found: {name}")
        return {}

    metadata_items, body, path = cached
    metadata = dict(metadata_items)
    return {
        "name": metadata.get("name", name),
        "description": metadata.get("description", ""),
        "content": body,
        "path": path,
    }


def load_all_skills() -> dict[str, dict[str, Any]]:
    """Load all available skills."""
    skills = {}
    for skill_path in SKILLS_DIR.glob("*.md"):
        name = skill_path.stem
        skill = load_skill(name)
        if skill:
            skills[name] = skill
    return skills


def get_skill_prompt(name: str) -> str:
    """Get the skill content formatted as a system prompt for LLM injection."""
    skill = load_skill(name)
    if not skill:
        return ""
    return f"## Skill: {skill['name']}\n\n{skill['content']}"


def clear_skill_cache() -> None:
    """Clear the in-process skill cache (tests / hot-reload)."""
    _load_skill_cached.cache_clear()
