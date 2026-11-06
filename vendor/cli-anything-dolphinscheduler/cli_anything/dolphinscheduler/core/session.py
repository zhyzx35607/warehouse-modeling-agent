"""Stateful session for the DolphinScheduler CLI.

The REPL and one-shot commands share a small amount of state: the connection
config and the "current" project so an agent doesn't have to repeat
``--project-code`` on every call. That state is persisted to a JSON session
file so it survives across one-shot invocations.

Session saves use exclusive file locking (``_locked_save_json``) so that
concurrent writers never corrupt the file: we open with ``"r+"`` and only
truncate *after* the lock is held.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

DEFAULT_SESSION_DIR = Path.home() / ".cli-anything-dolphinscheduler"
DEFAULT_SESSION_FILE = DEFAULT_SESSION_DIR / "session.json"

# Bump when the on-disk schema changes in an incompatible way.
_SESSION_VERSION = 1


@dataclass
class Session:
    """Mutable working state for a CLI session.

    Attributes:
        path: Where this session persists to.
        project_code: The currently selected project's code, if any.
        project_name: Human-readable name of the current project (for prompts).
        data: Free-form scratch space for additional state.
    """

    path: Path = DEFAULT_SESSION_FILE
    project_code: Optional[int] = None
    project_name: Optional[str] = None
    data: dict[str, Any] = field(default_factory=dict)

    # ── Selection helpers ────────────────────────────────────────────────

    def select_project(self, code: int, name: Optional[str] = None) -> None:
        """Set the current project. Returns nothing; mutates in place then save."""
        self.project_code = int(code)
        self.project_name = name

    def clear_project(self) -> None:
        self.project_code = None
        self.project_name = None

    @property
    def has_project(self) -> bool:
        return self.project_code is not None

    def require_project(self) -> int:
        """Return the current project code or raise a clear error."""
        if self.project_code is None:
            raise ValueError(
                "No project selected. Run 'project use <name-or-code>' first, "
                "or pass --project-code."
            )
        return self.project_code

    # ── Persistence ──────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": _SESSION_VERSION,
            "project_code": self.project_code,
            "project_name": self.project_name,
            "data": self.data,
        }

    def save(self) -> Path:
        """Persist the session to disk with exclusive file locking."""
        _locked_save_json(self.path, self.to_dict(), indent=2)
        return self.path


def load_session(path: Optional[Path] = None) -> Session:
    """Load a session from disk, returning a fresh one when none exists."""
    target = Path(path) if path else DEFAULT_SESSION_FILE
    if not target.exists():
        return Session(path=target)

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A corrupt session file should never wedge the CLI; start clean but
        # keep the same path so the next save overwrites it.
        return Session(path=target)

    if not isinstance(raw, dict):
        return Session(path=target)

    return Session(
        path=target,
        project_code=raw.get("project_code"),
        project_name=raw.get("project_name"),
        data=raw.get("data") or {},
    )


def _locked_save_json(path: Path, data: Any, **dump_kwargs: Any) -> None:
    """Atomically write JSON with an exclusive lock held during the write.

    ``open("w")`` truncates before any lock can be taken, so we open with
    ``"r+"`` (no truncation), acquire the lock, then truncate inside it. On
    platforms/filesystems without ``fcntl`` we proceed unlocked rather than
    failing the save.
    """
    path = Path(path)
    try:
        handle = open(path, "r+", encoding="utf-8")
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "w", encoding="utf-8")

    with handle:
        locked = False
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
        except (ImportError, OSError):
            pass  # Windows / unsupported filesystem — best-effort save.

        try:
            handle.seek(0)
            handle.truncate()
            json.dump(data, handle, **dump_kwargs)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            if locked:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
