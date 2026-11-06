"""DB MCP Client wrapper — LangGraph nodes call this instead of direct DBConnector.

The agent connects to the DB MCP server via the MCP protocol.
This module wraps MCP calls into typed async methods.

Supports: MySQL, PostgreSQL, Oracle, 达梦(DM), 人大金仓(Kingbase), MongoDB, etc.
New DB types are added to the MCP server — the agent code never changes.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

DB_MCP_SERVER_PATH = os.path.join(os.path.dirname(__file__), "db_server.py")


class DbMCPClient:
    """Client for the Database Explorer MCP server.

    Connects to the MCP server via the `mcp` package's client tools.
    Each method corresponds to an MCP tool exposed by db_server.py.
    """

    def __init__(self):
        pass

    async def _call_tool(self, name: str, **kwargs) -> dict[str, Any]:
        """Call a DB MCP tool via the server's call_tool handler directly.

        In production with MCP stdio transport fully working, this would
        use the MCP client session. For now, we call the server handler
        in-process and wrap the response.
        """
        from wren_agent.mcp_servers.db_server import call_tool as server_call_tool
        from mcp.types import TextContent

        result = await server_call_tool(name, kwargs)
        if result and len(result) > 0 and isinstance(result[0], TextContent):
            parsed = json.loads(result[0].text)
            if isinstance(parsed, dict) and parsed.get("error"):
                raise Exception(parsed.get("message", str(parsed)))
            return parsed
        return {}

    # ── Convenience methods (mirrors DBConnector interface) ──

    @staticmethod
    def _source_args(source_db_id: str, database: str = "") -> dict[str, Any]:
        args: dict[str, Any] = {"source_db_id": source_db_id}
        if database:
            args["database"] = database
        return args

    async def list_tables(
        self, source_db_id: str, database: str = "",
    ) -> list[str]:
        result = await self._call_tool(
            "db_list_tables", **self._source_args(source_db_id, database)
        )
        return result.get("tables", [])

    async def describe_table(
        self, source_db_id: str, table_name: str, database: str = "",
    ) -> dict:
        return await self._call_tool("db_describe_table",
            **self._source_args(source_db_id, database), table_name=table_name,
        )

    async def sample_data(
        self, source_db_id: str, table_name: str,
        limit: int = 100, database: str = "",
    ) -> list[dict]:
        result = await self._call_tool("db_sample_data",
            **self._source_args(source_db_id, database),
            table_name=table_name, limit=limit,
        )
        return result.get("rows", [])

    async def execute_query(
        self, source_db_id: str, sql: str, database: str = "",
    ) -> list[dict]:
        result = await self._call_tool("db_execute_query",
            **self._source_args(source_db_id, database), sql=sql,
        )
        return result.get("rows", [])

    async def get_foreign_keys(
        self, source_db_id: str, database: str = "",
    ) -> list[dict]:
        result = await self._call_tool("db_get_foreign_keys",
            **self._source_args(source_db_id, database),
        )
        return result.get("foreign_keys", [])

    async def count_distinct(
        self, source_db_id: str, table_name: str,
        column_name: str, database: str = "",
    ) -> int:
        result = await self._call_tool("db_count_distinct",
            **self._source_args(source_db_id, database),
            table_name=table_name, column_name=column_name,
        )
        return result.get("count_distinct", 0)

    async def null_ratio(
        self, source_db_id: str, table_name: str,
        column_name: str, database: str = "",
    ) -> float:
        result = await self._call_tool("db_null_ratio",
            **self._source_args(source_db_id, database),
            table_name=table_name, column_name=column_name,
        )
        return result.get("null_ratio", 0.0)

    async def verify_join(
        self, source_db_id: str,
        source_table: str, source_column: str,
        target_table: str, target_column: str,
        database: str = "",
    ) -> float:
        result = await self._call_tool("db_verify_join",
            **self._source_args(source_db_id, database),
            source_table=source_table, source_column=source_column,
            target_table=target_table, target_column=target_column,
        )
        return result.get("cardinality_ratio", 0.0)

    async def sensitive_scan(
        self, source_db_id: str, database: str = "",
    ) -> list[dict]:
        result = await self._call_tool("db_sensitive_scan",
            **self._source_args(source_db_id, database),
        )
        return result.get("sensitive_fields", [])

    async def explore_schema(
        self, source_db_id: str, database: str = "",
    ) -> dict:
        """Full light exploration via MCP — returns complete schema."""
        return await self._call_tool("db_explore_schema",
            **self._source_args(source_db_id, database),
        )


# ── Singleton ───────────────────────────────────────────────────

_db_client: DbMCPClient | None = None


def get_db_client() -> DbMCPClient:
    """Get or create the DB MCP client singleton."""
    global _db_client
    if _db_client is None:
        _db_client = DbMCPClient()
    return _db_client
