"""Generic Database MCP Server.

Exposes database exploration tools via MCP protocol.
Translates MCP tool calls to actual DB queries.
Supports: MySQL (pymysql), PostgreSQL (psycopg2/pg8000),
         Oracle (cx_Oracle), DM(达梦), Kingbase(人大金仓).

Each MCP tool receives only a registered source database identifier. The
server resolves credentials internally so passwords never enter MCP tool
schemas, model-visible arguments, or protocol traces.

Architecture:
  Agent (LangGraph) → MCP Client → MCP Server (stdio) → DB Driver → DB
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from mcp import types as mcp_types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from wren_agent.tools.db_explorer import (
    DBConnector,
    map_type_to_doris,
    detect_sensitive_field,
    take_schema_snapshot,
)

logger = logging.getLogger(__name__)

_ALLOWED_QUERY_STARTS = {"SELECT", "SHOW", "DESC", "DESCRIBE", "EXPLAIN", "WITH"}
_FORBIDDEN_QUERY_TOKENS = {
    "INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE", "UPSERT",
    "CREATE", "ALTER", "DROP", "TRUNCATE", "RENAME",
    "GRANT", "REVOKE", "CALL", "EXEC", "EXECUTE", "DO",
    "COPY", "LOAD", "UNLOAD", "SET", "USE", "LOCK", "UNLOCK",
    "VACUUM", "ANALYZE", "INTO", "OUTFILE", "DUMPFILE",
}
_FORBIDDEN_QUERY_FUNCTIONS = {
    "SLEEP", "PG_SLEEP", "BENCHMARK",
    "GET_LOCK", "RELEASE_LOCK", "PG_ADVISORY_LOCK", "PG_ADVISORY_XACT_LOCK",
    "NEXTVAL", "SETVAL",
    "LOAD_FILE", "PG_READ_FILE", "PG_READ_BINARY_FILE", "PG_LS_DIR",
    "LO_IMPORT", "LO_EXPORT",
}
_MAX_QUERY_RESULT_ROWS = 500


def _visible_sql(sql: str) -> str:
    """Return SQL code with strings, quoted identifiers, and comments hidden."""
    output: list[str] = []
    i = 0
    length = len(sql)

    while i < length:
        char = sql[i]
        nxt = sql[i + 1] if i + 1 < length else ""

        if char == "-" and nxt == "-":
            end = sql.find("\n", i + 2)
            i = length if end < 0 else end + 1
            output.append(" ")
            continue
        if char == "#":
            end = sql.find("\n", i + 1)
            i = length if end < 0 else end + 1
            output.append(" ")
            continue
        if char == "/" and nxt == "*":
            end = sql.find("*/", i + 2)
            if end < 0:
                raise ValueError("Unterminated SQL block comment")
            i = end + 2
            output.append(" ")
            continue
        if char in ("'", '"', "`"):
            quote = char
            i += 1
            while i < length:
                if sql[i] == "\\" and quote == "'" and i + 1 < length:
                    i += 2
                    continue
                if sql[i] == quote:
                    if i + 1 < length and sql[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            else:
                raise ValueError("Unterminated SQL quoted value")
            output.append(" ")
            continue
        if char == "$":
            match = re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$", sql[i:])
            if match:
                delimiter = match.group(0)
                end = sql.find(delimiter, i + len(delimiter))
                if end < 0:
                    raise ValueError("Unterminated PostgreSQL dollar-quoted value")
                i = end + len(delimiter)
                output.append(" ")
                continue

        output.append(char)
        i += 1

    return "".join(output)


def validate_read_only_sql(sql: str) -> tuple[bool, str]:
    """Validate one conservative, read-only SQL statement without executing it."""
    if not isinstance(sql, str) or not sql.strip():
        return False, "SQL must not be empty"

    try:
        visible = _visible_sql(sql)
    except ValueError as exc:
        return False, str(exc)

    statements = [part.strip() for part in visible.split(";") if part.strip()]
    if len(statements) != 1:
        return False, "Exactly one SQL statement is allowed"

    statement = statements[0]
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_$]*", statement.upper())
    if not tokens or tokens[0] not in _ALLOWED_QUERY_STARTS:
        return False, "Only SELECT/SHOW/DESCRIBE/EXPLAIN/WITH queries are allowed"

    tokens_for_forbidden_check = list(tokens)
    if tokens[:2] == ["SHOW", "CREATE"]:
        tokens_for_forbidden_check.remove("CREATE")
    forbidden = sorted(set(tokens_for_forbidden_check) & _FORBIDDEN_QUERY_TOKENS)
    if forbidden:
        return False, f"Forbidden SQL keyword: {forbidden[0]}"

    dangerous_function = sorted(set(tokens) & _FORBIDDEN_QUERY_FUNCTIONS)
    if dangerous_function:
        return False, f"Forbidden SQL function: {dangerous_function[0]}"

    if tokens[0] == "WITH" and "SELECT" not in tokens:
        return False, "WITH queries must end in a SELECT"
    if tokens[0] == "EXPLAIN" and not any(
        token in {"SELECT", "SHOW", "DESC", "DESCRIBE", "WITH"}
        for token in tokens[1:]
    ):
        return False, "EXPLAIN is only allowed for read-only queries"

    if re.search(r"\bFOR\s+(?:UPDATE|SHARE)\b", statement, re.IGNORECASE):
        return False, "Locking SELECT statements are not allowed"
    if re.search(
        r"\bSELECT\s+(?:ALL\s+|DISTINCT\s+)?(?:[A-Za-z_][A-Za-z0-9_$]*\s*\.\s*)?\*",
        statement,
        re.IGNORECASE,
    ):
        return False, "SELECT * is not allowed; request explicit non-sensitive columns"

    return True, ""


def _redact_query_rows(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """Redact values whose result-column names indicate sensitive content."""
    from wren_agent.tools.db_explorer import detect_sensitive_field

    redacted_fields: set[str] = set()
    redacted_rows: list[dict] = []
    sensitive_name = re.compile(
        r"(?:^|_)(?:phone|mobile|tel|id_card|identity|sfz|cert_no|bank_card|"
        r"bank_account|email|e_mail|address|addr|real_name|password|passwd|pwd|"
        r"secret|token|credential|real_name|user_name|employee_name|customer_name|"
        r"owner_name|tenant_name|contact_name|client_ip|server_ip|remote_ip)(?:_|$)",
        re.IGNORECASE,
    )

    for row in rows:
        safe_row = dict(row)
        for field_name in list(safe_row):
            name = str(field_name)
            if detect_sensitive_field("query_result", name) or sensitive_name.search(name):
                safe_row[field_name] = "[REDACTED]"
                redacted_fields.add(name)
        redacted_rows.append(safe_row)
    return redacted_rows, sorted(redacted_fields)


async def list_tools() -> list[Tool]:
    """List all DB exploration MCP tools."""
    source_order = {
        "source_db_id": {
            "type": "string",
            "description": "Registered source database ID or name",
        },
        "database": {
            "type": "string",
            "description": "Optional registered layer database override",
        },
    }

    def schema(properties: dict[str, Any] | None = None, required: list[str] | None = None):
        return {
            "type": "object",
            "properties": {**source_order, **(properties or {})},
            "required": ["source_db_id", *(required or [])],
        }

    return [
        Tool(
            name="db_list_tables",
            description="List all tables in a database",
            inputSchema=schema(),
        ),
        Tool(
            name="db_describe_table",
            description="Get table schema: columns, types, keys, comments, Doris type mapping",
            inputSchema=schema(
                {"table_name": {"type": "string"}},
                ["table_name"],
            ),
        ),
        Tool(
            name="db_sample_data",
            description="Sample N rows from non-sensitive columns in a table",
            inputSchema=schema(
                {
                    "table_name": {"type": "string"},
                    "limit": {"type": "integer", "default": 100, "description": "Number of rows to sample"},
                },
                ["table_name"],
            ),
        ),
        Tool(
            name="db_execute_query",
            description=(
                "Execute one validated read-only SQL query using explicit columns; "
                "sensitive result columns are redacted"
            ),
            inputSchema=schema(
                {
                    "sql": {"type": "string", "description": "SELECT query only"},
                },
                ["sql"],
            ),
        ),
        Tool(
            name="db_get_foreign_keys",
            description="Discover foreign key relationships",
            inputSchema=schema(),
        ),
        Tool(
            name="db_count_distinct",
            description="Get COUNT(DISTINCT) for a column",
            inputSchema=schema(
                {
                    "table_name": {"type": "string"},
                    "column_name": {"type": "string"},
                },
                ["table_name", "column_name"],
            ),
        ),
        Tool(
            name="db_null_ratio",
            description="Get NULL ratio for a column",
            inputSchema=schema(
                {
                    "table_name": {"type": "string"},
                    "column_name": {"type": "string"},
                },
                ["table_name", "column_name"],
            ),
        ),
        Tool(
            name="db_verify_join",
            description="Verify JOIN cardinality between two tables",
            inputSchema=schema(
                {
                    "source_table": {"type": "string"},
                    "source_column": {"type": "string"},
                    "target_table": {"type": "string"},
                    "target_column": {"type": "string"},
                },
                ["source_table", "source_column", "target_table", "target_column"],
            ),
        ),
        Tool(
            name="db_sensitive_scan",
            description="Scan all columns in a database for sensitive field patterns",
            inputSchema=schema(),
        ),
        Tool(
            name="db_explore_schema",
            description="Full light exploration: all tables, columns, types, samples, FKs. Returns complete SchemaExploration.",
            inputSchema=schema(),
        ),
    ]


_INLINE_CREDENTIAL_KEYS = {"host", "port", "user", "password", "db_type"}


def _registered_databases(creds: dict) -> set[str]:
    """Return databases explicitly registered for this source."""
    databases = {str(creds.get("database", "") or "")}
    layers = (creds.get("connection_params") or {}).get("layers") or {}
    for value in layers.values():
        if isinstance(value, str):
            databases.add(value)
        elif isinstance(value, list):
            databases.update(str(item) for item in value if item)
    return {database for database in databases if database}


async def _resolve_connection_args(args: dict) -> dict:
    """Resolve a registered connection without accepting inline credentials."""
    forbidden = sorted(_INLINE_CREDENTIAL_KEYS & set(args))
    if forbidden:
        raise ValueError(
            f"Inline connection parameters are not accepted: {', '.join(forbidden)}"
        )

    source_db_id = str(args.get("source_db_id", "") or "").strip()
    if not source_db_id:
        raise ValueError("source_db_id is required")

    from wren_agent.tools.secret_manager import get_credential_provider
    creds = dict(await get_credential_provider().get_credentials(source_db_id))

    if not creds.get("connection_params"):
        from wren_agent.tools.pg_store import get_source_db, parse_connection_params
        row = await get_source_db(source_db_id)
        if row:
            creds["connection_params"] = parse_connection_params(
                row.get("connection_params")
            )

    database = str(args.get("database", "") or creds.get("database", "") or "")
    allowed_databases = _registered_databases(creds)
    if database not in allowed_databases:
        raise ValueError("Requested database is not registered for this source")

    creds["database"] = database
    return creds


def _get_connector(args: dict) -> DBConnector:
    """Build a DBConnector from server-resolved credentials."""
    return DBConnector(
        host=args["host"],
        port=args["port"],
        user=args["user"],
        password=args["password"],
        database=args["database"],
        db_type=args.get("db_type", "mysql"),
        connection_params=args.get("connection_params") or {},
    )


async def _resolve_connector(args: dict) -> DBConnector:
    return _get_connector(await _resolve_connection_args(args))


async def _safe_call(name: str, fn) -> list[TextContent]:
    """Execute a tool handler with error wrapping."""
    try:
        result = await fn()
        if hasattr(result, "model_dump"):
            result = result.model_dump()
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]
    except Exception as e:
        from wren_agent.tools.redaction import redact_text
        message = redact_text(str(e))[:500]
        logger.error("Tool %s failed: %s", name, message)
        return [TextContent(type="text", text=json.dumps({
            "error": True, "tool": name, "message": message
        }, ensure_ascii=False))]


async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Route MCP tool calls to handlers."""

    if name == "db_list_tables":
        async def _fn():
            connector = await _resolve_connector(arguments)
            return {"tables": await connector.list_tables()}
        return await _safe_call(name, _fn)

    elif name == "db_describe_table":
        async def _fn():
            connector = await _resolve_connector(arguments)
            schema = await connector.get_table_info(arguments["table_name"])
            return {
                "table_name": schema.name,
                "row_estimate": schema.row_count_estimate,
                "fields": [
                    {
                        "name": f.name, "data_type": f.data_type,
                        "doris_type": f.doris_type, "nullable": f.nullable,
                        "primary_key": f.primary_key, "comment": f.comment,
                        "is_sensitive": f.is_sensitive,
                    }
                    for f in schema.fields
                ],
                "primary_keys": schema.primary_keys,
            }
        return await _safe_call(name, _fn)

    elif name == "db_sample_data":
        async def _fn():
            connector = await _resolve_connector(arguments)
            schema = await connector.get_table_info(arguments["table_name"])
            from wren_agent.tools.db_explorer import safe_sample_columns
            safe_columns = safe_sample_columns(schema)
            rows = await connector.sample_data(
                arguments["table_name"],
                arguments.get("limit", 100),
                columns=safe_columns,
            )
            redacted_fields = [f.name for f in schema.fields if f.is_sensitive]
            return {
                "rows": rows,
                "count": len(rows),
                "redacted_fields": redacted_fields,
            }
        return await _safe_call(name, _fn)

    elif name == "db_execute_query":
        async def _fn():
            sql = arguments["sql"]
            valid, message = validate_read_only_sql(sql)
            if not valid:
                return {"error": True, "message": message}
            connector = await _resolve_connector(arguments)
            rows, truncated = await connector.query_read_only(
                sql, max_rows=_MAX_QUERY_RESULT_ROWS,
            )
            safe_rows, redacted_fields = _redact_query_rows(rows)
            return {
                "rows": safe_rows,
                "count": len(safe_rows),
                "truncated": truncated,
                "redacted_fields": redacted_fields,
            }
        return await _safe_call(name, _fn)

    elif name == "db_get_foreign_keys":
        async def _fn():
            connector = await _resolve_connector(arguments)
            fks = await connector.get_foreign_keys()
            return {"foreign_keys": [
                {"source_table": f.source_table, "source_column": f.source_column,
                 "target_table": f.target_table, "target_column": f.target_column}
                for f in fks
            ]}
        return await _safe_call(name, _fn)

    elif name == "db_count_distinct":
        async def _fn():
            connector = await _resolve_connector(arguments)
            cnt = await connector.count_distinct(
                arguments["table_name"], arguments["column_name"]
            )
            return {"count_distinct": cnt}
        return await _safe_call(name, _fn)

    elif name == "db_null_ratio":
        async def _fn():
            connector = await _resolve_connector(arguments)
            ratio = await connector.null_ratio(
                arguments["table_name"], arguments["column_name"]
            )
            return {"null_ratio": ratio}
        return await _safe_call(name, _fn)

    elif name == "db_verify_join":
        async def _fn():
            connector = await _resolve_connector(arguments)
            ratio = await connector.verify_join_cardinality(
                arguments["source_table"], arguments["source_column"],
                arguments["target_table"], arguments["target_column"],
            )
            return {"cardinality_ratio": ratio}
        return await _safe_call(name, _fn)

    elif name == "db_sensitive_scan":
        async def _fn():
            connector = await _resolve_connector(arguments)
            tables = await connector.list_tables()
            results = []
            for table_name in tables[:50]:  # Limit to avoid timeout on large DBs
                try:
                    schema = await connector.get_table_info(table_name)
                    for f in schema.fields:
                        result = detect_sensitive_field(table_name, f.name)
                        if result:
                            results.append({
                                "table": table_name, "field": f.name,
                                "type": result.sensor_type.value,
                                "confidence": result.confidence,
                            })
                except Exception:
                    pass
            return {"sensitive_fields": results, "count": len(results)}
        return await _safe_call(name, _fn)

    elif name == "db_explore_schema":
        async def _fn():
            connector = await _resolve_connector(arguments)
            from wren_agent.models.schema import SchemaExploration
            exploration = await connector.explore_schema_light(arguments["source_db_id"])
            return {
                "source_db_id": exploration.source_db_id,
                "tables": [
                    {
                        "name": t.name,
                        "row_estimate": t.row_count_estimate,
                        "fields": [
                            {
                                "name": f.name, "data_type": f.data_type,
                                "doris_type": f.doris_type, "nullable": f.nullable,
                                "primary_key": f.primary_key, "comment": f.comment,
                                "is_sensitive": f.is_sensitive,
                                "sample_values": f.sample_values[:3] if f.sample_values else [],
                            }
                            for f in t.fields
                        ],
                        "primary_keys": t.primary_keys,
                    }
                    for t in exploration.tables
                ],
                "relationships": [
                    {
                        "source_table": r.source_table, "source_column": r.source_column,
                        "target_table": r.target_table, "target_column": r.target_column,
                        "relationship_type": r.relationship_type.value,
                    }
                    for r in exploration.relationships
                ],
                "table_count": len(exploration.tables),
                "relationship_count": len(exploration.relationships),
            }
        return await _safe_call(name, _fn)

    else:
        return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


async def _on_list_tools(_context, _params) -> mcp_types.ListToolsResult:
    """Adapter for the current low-level MCP Server callback API."""
    return mcp_types.ListToolsResult(tools=await list_tools())


async def _on_call_tool(
    _context,
    params: mcp_types.CallToolRequestParams,
) -> mcp_types.CallToolResult:
    """Adapter from MCP request parameters to the existing tool router."""
    content = await call_tool(params.name, params.arguments or {})
    is_error = False
    if content and isinstance(content[0], TextContent):
        try:
            payload = json.loads(content[0].text)
            is_error = bool(payload.get("error")) if isinstance(payload, dict) else False
        except (TypeError, ValueError):
            pass
    return mcp_types.CallToolResult(content=content, isError=is_error)


app = Server("warehouse-db-explorer-mcp")
app.list_tools()(list_tools)


@app.call_tool()
async def registered_call_tool(name: str, arguments: dict | None):
    """Use the public MCP 1.x decorator API; retain explicit tool error status."""
    return await _on_call_tool(
        None, mcp_types.CallToolRequestParams(name=name, arguments=arguments or {})
    )


# ── Entry point ─────────────────────────────────────────────────


def run():
    """Run the DB MCP server (stdio transport)."""
    import asyncio
    logger.info("Starting Wren DB Explorer MCP Server")
    asyncio.run(_run_stdio())


async def _run_stdio():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    run()
