"""Asset Catalog API tests — sources → databases → tables → columns → rows.

Handlers are tested directly (no HTTP server): pg access is mocked with a
fake pool, Doris access is mocked at the DBConnector level — same pattern as
test_metrics_api.py (the catalog reuses the wizard's resolver/connector).
"""

import pytest

from wren_agent.api.catalog import (
    catalog_columns,
    catalog_databases,
    catalog_rows,
    catalog_sources,
    catalog_tables,
)
from fastapi import HTTPException


class _FakeConnector:
    """Fake DBConnector returning canned information_schema / data rows.

    Dispatches on SQL shape: tables / columns / COUNT / paged data. Every
    executed statement is recorded on `executed` for dialect assertions.
    """

    def __init__(self, tables=None, columns=None, rows=None, count=0):
        self.tables = tables or []
        self.columns = columns or []
        self.rows = rows or []
        self.count = count
        self.executed = []
        self.closed = False

    async def query(self, sql, params=()):
        self.executed.append((sql, params))
        if "information_schema.tables" in sql:
            return self.tables
        if "information_schema.columns" in sql:
            return self.columns
        if "FROM" in sql:
            return self.rows
        return []

    async def query_value(self, sql, params=()):
        self.executed.append((sql, params))
        return self.count

    def _quote_identifier(self, ident):
        return f'"{ident}"'

    async def aclose(self):
        self.closed = True


# ── sources ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_catalog_sources_lists_all_with_layers_flag(monkeypatch):
    from wren_agent.tools import pg_store

    async def fake_list(**kwargs):
        return [
            {"id": "s1", "name": "wh", "db_type": "doris", "host": "h",
             "port": 9030, "database_name": "", "tags": [],
             "table_count": 42,
             "connection_params": {"layers": {"dws": ["db_dws"]}}},
            {"id": "s2", "name": "mysql1", "db_type": "mysql", "host": "h2",
             "port": 3306, "database_name": "biz", "tags": ["财务"],
             "connection_params": {}},
        ]

    monkeypatch.setattr(pg_store, "list_source_dbs", fake_list)
    result = await catalog_sources()
    assert result["sources"][0]["has_layers"] is True
    assert result["sources"][0]["table_count"] == 42
    assert result["sources"][1]["has_layers"] is False
    assert result["sources"][1]["database_name"] == "biz"


# ── databases ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_catalog_databases_merges_layer_dbs(monkeypatch):
    from wren_agent.tools import pg_store

    async def fake_creds(_id):
        return {
            "id": "s1", "name": "wh", "db_type": "doris",
            "host": "h", "port": 9030, "database": "",
            "user": "root", "password": "x",
            "connection_params": {
                "layers": {"dim": ["wren_dw"], "dws": ["wren_dw", "db_dws"]},
            },
        }

    monkeypatch.setattr(pg_store, "get_source_db_credentials", fake_creds)
    result = await catalog_databases(source_id="s1")
    by_name = {d["name"]: d["layers"] for d in result["databases"]}
    assert by_name["wren_dw"] == ["dim", "dws"]  # deduped, layers merged
    assert by_name["db_dws"] == ["dws"]


@pytest.mark.asyncio
async def test_catalog_databases_single_db_for_non_warehouse(monkeypatch):
    from wren_agent.tools import pg_store

    async def fake_creds(_id):
        return {
            "id": "s2", "name": "mysql1", "db_type": "mysql",
            "host": "h2", "port": 3306, "database": "biz",
            "user": "root", "password": "x",
            "connection_params": {},
        }

    monkeypatch.setattr(pg_store, "get_source_db_credentials", fake_creds)
    result = await catalog_databases(source_id="s2")
    assert result["databases"] == [{"name": "biz", "layers": []}]


@pytest.mark.asyncio
async def test_catalog_databases_404_for_unknown_source(monkeypatch):
    from wren_agent.tools import pg_store

    async def fake_creds(_id):
        return None

    monkeypatch.setattr(pg_store, "get_source_db_credentials", fake_creds)
    with pytest.raises(HTTPException) as exc:
        await catalog_databases(source_id="missing")
    assert exc.value.status_code == 404


# ── tables ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_catalog_tables_returns_comments(monkeypatch):
    from wren_agent.tools import pg_store
    from wren_agent.tools import db_explorer

    async def fake_creds(_id):
        return {"host": "h", "port": 9030, "database": "db_dws",
                "user": "root", "password": "x", "db_type": "doris",
                "connection_params": {}}

    monkeypatch.setattr(pg_store, "get_source_db_credentials", fake_creds)
    monkeypatch.setattr(
        db_explorer, "DBConnector",
        lambda **kw: _FakeConnector(tables=[
            {"TABLE_NAME": "dws_x", "TABLE_COMMENT": "月度应收汇总表"},
            {"TABLE_NAME": "dws_y", "TABLE_COMMENT": "机构余额表"},
        ]))
    result = await catalog_tables(source_id="s1", database="db_dws")
    assert [t["table_name"] for t in result["tables"]] == ["dws_x", "dws_y"]
    assert result["tables"][0]["table_comment"] == "月度应收汇总表"


# ── columns ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_catalog_columns_returns_name_type_comment(monkeypatch):
    from wren_agent.tools import pg_store
    from wren_agent.tools import db_explorer

    async def fake_creds(_id):
        return {"host": "h", "port": 9030, "database": "db_dws",
                "user": "root", "password": "x", "db_type": "doris",
                "connection_params": {}}

    monkeypatch.setattr(pg_store, "get_source_db_credentials", fake_creds)
    monkeypatch.setattr(
        db_explorer, "DBConnector",
        lambda **kw: _FakeConnector(columns=[
            {"COLUMN_NAME": "stat_month", "COLUMN_TYPE": "VARCHAR(7)",
             "COLUMN_COMMENT": "统计月份"},
            {"COLUMN_NAME": "amount_receivable", "COLUMN_TYPE": "DECIMAL(19,4)",
             "COLUMN_COMMENT": "应收金额"},
        ]))
    result = await catalog_columns(source_id="s1", database="db_dws", table="dws_x")
    assert result["columns"][0] == {
        "column_name": "stat_month",
        "data_type": "VARCHAR(7)",
        "column_comment": "统计月份",
    }
    assert result["columns"][1]["column_comment"] == "应收金额"


# ── rows (paged data) ──────────────────────────────────────────


def _rows_columns():
    return [
        {"COLUMN_NAME": "stat_month", "COLUMN_TYPE": "VARCHAR(7)",
         "COLUMN_COMMENT": "统计月份"},
        {"COLUMN_NAME": "amount_receivable", "COLUMN_TYPE": "DECIMAL(19,4)",
         "COLUMN_COMMENT": "应收金额"},
        {"COLUMN_NAME": "id_card_no", "COLUMN_TYPE": "VARCHAR(18)",
         "COLUMN_COMMENT": "身份证号"},
    ]


def _rows_data():
    return [
        {"stat_month": "2026-07", "amount_receivable": "123.4500",
         "id_card_no": "110101199001011234"},
        {"stat_month": "2026-08", "amount_receivable": "456.7800",
         "id_card_no": "110101199002022345"},
    ]


@pytest.mark.asyncio
async def test_catalog_rows_mysql_doris_dialect(monkeypatch):
    from wren_agent.tools import pg_store
    from wren_agent.tools import db_explorer

    async def fake_creds(_id):
        return {"host": "h", "port": 9030, "database": "db_dws",
                "user": "root", "password": "x", "db_type": "doris",
                "connection_params": {}}

    connector = _FakeConnector(columns=_rows_columns(), rows=_rows_data(), count=120)
    monkeypatch.setattr(pg_store, "get_source_db_credentials", fake_creds)
    monkeypatch.setattr(db_explorer, "DBConnector", lambda **kw: connector)

    result = await catalog_rows(source_id="s1", database="db_dws",
                                table="dws_x", page=2, page_size=50)
    assert result["total"] == 120
    assert result["page"] == 2
    assert len(result["rows"]) == 2
    assert [c["column_name"] for c in result["columns"]] == [
        "stat_month", "amount_receivable", "id_card_no"]

    # Dialect: mysql/doris LIMIT offset, count (page 2, 50 → offset 50)
    data_sql = [s for s, _ in connector.executed if "stat_month" in s][0]
    assert data_sql.endswith("LIMIT 50, 50")
    # COUNT statement runs first
    assert any("COUNT(*)" in s for s, _ in connector.executed)
    # Column query pinned the header order
    assert any("information_schema.columns" in s for s, _ in connector.executed)


@pytest.mark.asyncio
async def test_catalog_rows_pg_kingbase_dm_dialect(monkeypatch):
    from wren_agent.tools import pg_store
    from wren_agent.tools import db_explorer

    async def fake_creds(_id):
        return {"host": "h", "port": 5432, "database": "db_dws",
                "user": "root", "password": "x", "db_type": "postgresql",
                "connection_params": {}}

    connector = _FakeConnector(columns=_rows_columns(), rows=_rows_data(), count=3)
    monkeypatch.setattr(pg_store, "get_source_db_credentials", fake_creds)
    monkeypatch.setattr(db_explorer, "DBConnector", lambda **kw: connector)

    result = await catalog_rows(source_id="s1", database="db_dws",
                                table="dws_x", page=1, page_size=50)
    assert result["total"] == 3
    data_sql = [s for s, _ in connector.executed if "stat_month" in s][0]
    assert data_sql.endswith("LIMIT 50 OFFSET 0")


@pytest.mark.asyncio
async def test_catalog_rows_oracle_rownum_dialect(monkeypatch):
    from wren_agent.tools import pg_store
    from wren_agent.tools import db_explorer

    async def fake_creds(_id):
        return {"host": "h", "port": 1521, "database": "ORCL",
                "user": "root", "password": "x", "db_type": "oracle",
                "connection_params": {}}

    connector = _FakeConnector(columns=_rows_columns(), rows=_rows_data(), count=7)
    monkeypatch.setattr(pg_store, "get_source_db_credentials", fake_creds)
    monkeypatch.setattr(db_explorer, "DBConnector", lambda **kw: connector)

    result = await catalog_rows(source_id="s1", database="ORCL",
                                table="dws_x", page=2, page_size=50)
    assert result["total"] == 7
    data_sql = [s for s, _ in connector.executed if "stat_month" in s][0]
    assert "ROWNUM" in data_sql
    assert "ROWNUM <= 100" in data_sql   # page 2, 50 → end = 100
    assert "rn > 50" in data_sql


@pytest.mark.asyncio
async def test_catalog_rows_redacts_sensitive_fields(monkeypatch):
    from wren_agent.tools import pg_store
    from wren_agent.tools import db_explorer

    async def fake_creds(_id):
        return {"host": "h", "port": 9030, "database": "db_dws",
                "user": "root", "password": "x", "db_type": "doris",
                "connection_params": {}}

    connector = _FakeConnector(columns=_rows_columns(), rows=_rows_data(), count=2)
    monkeypatch.setattr(pg_store, "get_source_db_credentials", fake_creds)
    monkeypatch.setattr(db_explorer, "DBConnector", lambda **kw: connector)

    result = await catalog_rows(source_id="s1", database="db_dws",
                                table="dws_x", page=1, page_size=50)
    # id_card_no matches SENSITIVE_PATTERNS → masked, column kept
    assert result["redacted_fields"] == ["id_card_no"]
    assert result["rows"][0]["id_card_no"] == "[REDACTED]"
    assert result["rows"][0]["stat_month"] == "2026-07"
    assert result["rows"][1]["amount_receivable"] == "456.7800"


@pytest.mark.asyncio
async def test_catalog_rows_unsupported_mongodb(monkeypatch):
    from wren_agent.tools import pg_store

    async def fake_creds(_id):
        return {"host": "h", "port": 27017, "database": "biz",
                "user": "root", "password": "x", "db_type": "mongodb",
                "connection_params": {}}

    monkeypatch.setattr(pg_store, "get_source_db_credentials", fake_creds)
    with pytest.raises(HTTPException) as exc:
        await catalog_rows(source_id="s1", database="biz", table="c", page=1)
    assert exc.value.status_code == 400
    assert "mongodb" in exc.value.detail
