"""Output rendering for the DolphinScheduler CLI.

Every command routes its result through :class:`OutputWriter` so the ``--json``
flag has a single, consistent implementation. In JSON mode we emit a stable
envelope (``{"success", "data"}`` or ``{"success", "error", ...}``) that agents
can parse without scraping human text. In human mode we defer to the branded
:class:`ReplSkin` for colored, aligned output.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Optional

from .repl_skin import ReplSkin


class OutputWriter:
    """Render command results in either JSON or human-readable form.

    A single writer is created per CLI invocation and threaded through the
    command context, so the ``--json`` choice is made exactly once.
    """

    def __init__(self, json_mode: bool, skin: Optional[ReplSkin] = None):
        self._json = json_mode
        self._skin = skin or ReplSkin("dolphinscheduler", version="1.0.0")

    @property
    def json_mode(self) -> bool:
        return self._json

    @property
    def skin(self) -> ReplSkin:
        return self._skin

    # ── Success paths ────────────────────────────────────────────────────

    def success(self, message: str, data: Any = None) -> None:
        """Report a successful operation.

        In JSON mode the ``data`` payload is included verbatim; in human mode we
        print a green check plus the message.
        """
        if self._json:
            self._emit({"success": True, "message": message, "data": data})
        else:
            self._skin.success(message)

    def data(self, data: Any) -> None:
        """Emit a bare data payload (e.g. a query result).

        JSON mode prints the stable envelope; human mode is left to the caller,
        which typically renders a table or status block.
        """
        if self._json:
            self._emit({"success": True, "data": data})

    def table(self, headers: list[str], rows: list[list[Any]], data: Any = None) -> None:
        """Render a table in human mode, or the raw ``data`` in JSON mode."""
        if self._json:
            self._emit({"success": True, "data": data if data is not None else rows})
        else:
            str_rows = [[_cell(c) for c in row] for row in rows]
            self._skin.table(headers, str_rows)

    def status_block(self, items: dict[str, Any], title: str = "", data: Any = None) -> None:
        """Render a key/value block in human mode, or ``data`` in JSON mode."""
        if self._json:
            self._emit({"success": True, "data": data if data is not None else items})
        else:
            self._skin.status_block({k: _cell(v) for k, v in items.items()}, title=title)

    def info(self, message: str) -> None:
        """A non-fatal informational note (suppressed in JSON mode)."""
        if not self._json:
            self._skin.info(message)

    def warning(self, message: str) -> None:
        if self._json:
            self._emit({"success": True, "warning": message})
        else:
            self._skin.warning(message)

    # ── Failure path ─────────────────────────────────────────────────────

    def error(self, payload: dict[str, Any]) -> None:
        """Report a failure. ``payload`` is a typed error's ``to_dict()``."""
        if self._json:
            body = {"success": False}
            body.update(payload)
            self._emit(body, stream=sys.stderr)
        else:
            self._skin.error(payload.get("message", "Unknown error"))

    # ── Internals ────────────────────────────────────────────────────────

    def _emit(self, body: dict[str, Any], stream: Any = None) -> None:
        json.dump(body, stream or sys.stdout, indent=2, default=str)
        (stream or sys.stdout).write("\n")


def _cell(value: Any) -> str:
    """Coerce a table/status value into a compact string for display."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)
