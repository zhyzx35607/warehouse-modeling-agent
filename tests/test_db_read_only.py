"""Read-only and sensitive-output guards for the database MCP server."""

import json

import pytest

from mcp import types as mcp_types

from wren_agent.mcp_servers.db_server import (
    app,
    _resolve_connection_args,
    call_tool,
    list_tools,
    validate_read_only_sql,
)


@pytest.mark.parametrize("sql", [
    "SELECT id, status FROM orders WHERE status = 'DROP TABLE x'",
    "SELECT COUNT(*) AS cnt FROM orders",
    "WITH recent AS (SELECT id FROM orders) SELECT id FROM recent",
    "SHOW TABLES",
    "SHOW CREATE TABLE orders",
    "DESCRIBE orders",
    "EXPLAIN SELECT id FROM orders",
    "-- comment\nSELECT id FROM orders;",
    "SELECT $$DELETE FROM orders$$ AS sample_text",
])
def test_read_only_validator_allows_safe_queries(sql):
    valid, message = validate_read_only_sql(sql)
    assert valid is True, message


@pytest.mark.parametrize("sql", [
    "",
    "SELECT id FROM orders; DROP TABLE orders",
    "SELECT id FROM orders FOR UPDATE",
    "SELECT * FROM orders",
    "SELECT orders.* FROM orders",
    "WITH changed AS (DELETE FROM orders RETURNING id) SELECT id FROM changed",
    "EXPLAIN ANALYZE SELECT id FROM orders",
    "SELECT id INTO backup_orders FROM orders",
    "SELECT pg_sleep(10)",
    "SELECT nextval('order_seq')",
    "SELECT pg_read_file('/etc/passwd')",
    "/* unterminated",
])
def test_read_only_validator_rejects_unsafe_queries(sql):
    valid, message = validate_read_only_sql(sql)
    assert valid is False
    assert message


@pytest.mark.asyncio
async def test_rejected_query_is_never_executed(monkeypatch):
    class Connector:
        async def query_read_only(self, _sql, max_rows=500):
            pytest.fail("unsafe SQL reached the database connector")

    async def resolve_connector(_arguments):
        return Connector()

    monkeypatch.setattr(
        "wren_agent.mcp_servers.db_server._resolve_connector",
        resolve_connector,
    )

    result = await call_tool("db_execute_query", {
        "sql": "SELECT id FROM orders; DELETE FROM orders",
    })
    payload = json.loads(result[0].text)

    assert payload["error"] is True


@pytest.mark.asyncio
async def test_query_results_are_capped_and_sensitive_columns_redacted(monkeypatch):
    class Connector:
        async def query_read_only(self, sql, max_rows=500):
            assert sql == "SELECT id, customer_phone, status FROM orders"
            assert max_rows == 500
            rows = [
                {"id": i, "customer_phone": "13800000000", "status": "ok"}
                for i in range(500)
            ]
            return rows, True

    async def resolve_connector(_arguments):
        return Connector()

    monkeypatch.setattr(
        "wren_agent.mcp_servers.db_server._resolve_connector",
        resolve_connector,
    )

    result = await call_tool("db_execute_query", {
        "sql": "SELECT id, customer_phone, status FROM orders",
    })
    payload = json.loads(result[0].text)

    assert payload["count"] == 500
    assert payload["truncated"] is True
    assert payload["redacted_fields"] == ["customer_phone"]
    assert all(row["customer_phone"] == "[REDACTED]" for row in payload["rows"])
    assert all(row["status"] == "ok" for row in payload["rows"])


@pytest.mark.asyncio
async def test_current_mcp_callback_api_is_wired(monkeypatch):
    tools_result = (await app.request_handlers[mcp_types.ListToolsRequest](
        mcp_types.ListToolsRequest()
    )).root
    assert any(tool.name == "db_execute_query" for tool in tools_result.tools)

    class Connector:
        async def query_read_only(self, _sql, max_rows=500):
            return [{"id": 1}], False

    async def resolve_connector(_arguments):
        return Connector()

    monkeypatch.setattr(
        "wren_agent.mcp_servers.db_server._resolve_connector",
        resolve_connector,
    )
    handler = app.request_handlers[mcp_types.CallToolRequest]
    result = (await handler(mcp_types.CallToolRequest(
        params=mcp_types.CallToolRequestParams(
            name="db_execute_query",
            arguments={"source_db_id": "source-1", "sql": "SELECT id FROM orders"},
        ),
    ))).root

    assert result.isError is False
    assert json.loads(result.content[0].text)["rows"] == [{"id": 1}]
    rejected = (await handler(mcp_types.CallToolRequest(
        params=mcp_types.CallToolRequestParams(
            name="db_execute_query",
            arguments={"source_db_id": "source-1", "sql": "DELETE FROM orders"},
        ),
    ))).root
    assert rejected.isError is True


@pytest.mark.asyncio
async def test_mcp_tool_schemas_never_expose_inline_credentials():
    forbidden = {"host", "port", "user", "password", "db_type"}

    for tool in await list_tools():
        properties = set(tool.inputSchema["properties"])
        assert "source_db_id" in properties
        assert "source_db_id" in tool.inputSchema["required"]
        assert properties.isdisjoint(forbidden)


@pytest.mark.asyncio
async def test_inline_credentials_are_rejected_before_lookup(monkeypatch):
    def fail_provider():
        pytest.fail("credential provider must not run for inline credentials")

    monkeypatch.setattr(
        "wren_agent.tools.secret_manager.get_credential_provider",
        fail_provider,
    )

    with pytest.raises(ValueError, match="Inline connection parameters"):
        await _resolve_connection_args({
            "source_db_id": "source-1",
            "password": "inline-password",
        })


@pytest.mark.asyncio
async def test_registered_layer_database_override_is_allowed(monkeypatch):
    class Provider:
        async def get_credentials(self, source_db_id):
            assert source_db_id == "source-1"
            return {
                "host": "db.internal",
                "port": 9030,
                "user": "reader",
                "password": "resolved-password",
                "database": "default_db",
                "db_type": "doris",
                "connection_params": {
                    "layers": {"dwd": ["dwd_a", "dwd_b"]},
                },
            }

    monkeypatch.setattr(
        "wren_agent.tools.secret_manager.get_credential_provider",
        lambda: Provider(),
    )

    resolved = await _resolve_connection_args({
        "source_db_id": "source-1",
        "database": "dwd_b",
    })
    assert resolved["database"] == "dwd_b"
    assert resolved["password"] == "resolved-password"

    with pytest.raises(ValueError, match="not registered"):
        await _resolve_connection_args({
            "source_db_id": "source-1",
            "database": "unregistered_db",
        })


@pytest.mark.asyncio
async def test_credential_lookup_errors_are_redacted(monkeypatch, caplog):
    class Provider:
        async def get_credentials(self, _source_db_id):
            raise ValueError("password=resolved-password")

    monkeypatch.setattr(
        "wren_agent.tools.secret_manager.get_credential_provider",
        lambda: Provider(),
    )

    result = await call_tool("db_list_tables", {"source_db_id": "source-1"})
    payload = json.loads(result[0].text)

    assert payload["error"] is True
    assert "resolved-password" not in payload["message"]
    assert "resolved-password" not in caplog.text


@pytest.mark.asyncio
async def test_mcp_client_sends_only_source_reference(monkeypatch):
    from wren_agent.mcp_servers.db_client import DbMCPClient

    captured = {}
    client = DbMCPClient()

    async def capture(name, **kwargs):
        captured.update({"name": name, **kwargs})
        return {"tables": []}

    monkeypatch.setattr(client, "_call_tool", capture)
    await client.list_tables("source-1", database="dwd_b")

    assert captured == {
        "name": "db_list_tables",
        "source_db_id": "source-1",
        "database": "dwd_b",
    }


@pytest.mark.asyncio
async def test_connector_enforces_read_only_transaction_and_driver_row_cap(monkeypatch):
    from wren_agent.tools.db_explorer import DBConnector

    executed = []

    class Cursor:
        description = [("id",)]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, params=()):
            executed.append((sql, params))

        def fetchmany(self, count):
            assert count == 3
            return [(1,), (2,), (3,)]

    class Connection:
        def cursor(self):
            return Cursor()

        def rollback(self):
            executed.append(("ROLLBACK", ()))

        def close(self):
            executed.append(("CLOSE", ()))

    connector = DBConnector(
        host="unused", port=3306, user="reader", password="unused",
        database="db", db_type="mysql",
    )
    monkeypatch.setattr(connector, "_connect", lambda: Connection())

    rows, truncated = await connector.query_read_only(
        "SELECT id FROM orders", max_rows=2,
    )

    assert executed[0] == ("SET TRANSACTION READ ONLY", ())
    assert executed[1] == ("SELECT id FROM orders", ())
    assert rows == [{"id": 1}, {"id": 2}]
    assert truncated is True
    assert executed[-2:] == [("ROLLBACK", ()), ("CLOSE", ())]
