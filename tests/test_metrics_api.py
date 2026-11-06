"""Metric Library API tests — list/CRUD + generation wizard.

Handlers are tested directly (no HTTP server): pg access is mocked with a
fake pool, Doris access is mocked at the DBConnector level.
"""

import pytest

from wren_agent.api.metrics import (
    create_metric,
    delete_metric,
    generate_metrics,
    list_metrics,
    update_metric,
    wizard_columns,
    wizard_databases,
    wizard_sources,
    wizard_tables,
)
from wren_agent.api.metrics import (
    MetricCreateRequest,
    MetricGenerateRequest,
    MetricUpdateRequest,
)
from fastapi import HTTPException


class _Pool:
    """Configurable fake PG pool.

    fetch() returns rows from a per-call sequence (or the same rows every
    call when only one batch is configured).
    """

    def __init__(self, fetch_rows=None, fetch_sequence=None, fetchval=None,
                 execute_result=None):
        self.fetch_rows = fetch_rows or []
        self.fetch_sequence = fetch_sequence  # list of row-batches, popped per call
        self.fetchval_result = fetchval
        self.execute_result = execute_result or "UPDATE 0"
        self.executed = []

    def acquire(self):
        return _Conn(self)


class _Tx:
    """Async context manager faking asyncpg's conn.transaction()."""

    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_a):
        return False


class _Conn:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None

    def transaction(self):
        return _Tx(self)

    async def fetch(self, _sql, *_args):
        self.pool.executed.append(("fetch", _sql, _args))
        if self.pool.fetch_sequence:
            return self.pool.fetch_sequence.pop(0)
        return self.pool.fetch_rows

    async def fetchrow(self, _sql, *_args):
        self.pool.executed.append(("fetchrow", _sql, _args))
        if self.pool.fetch_sequence:
            return self.pool.fetch_sequence.pop(0)
        return self.pool.fetch_rows[0] if self.pool.fetch_rows else None

    async def fetchval(self, _sql, *_args):
        return self.pool.fetchval_result

    async def execute(self, sql, *args):
        self.pool.executed.append(("execute", sql, args))
        return self.pool.execute_result

    async def executemany(self, sql, rows):
        self.pool.executed.append(("executemany", sql, rows))
        return None


class _FakeConnector:
    """Fake DBConnector returning canned information_schema rows."""

    def __init__(self, tables=None, columns=None):
        self.tables = tables or []
        self.columns = columns or []
        self.closed = False

    async def query(self, sql, params=()):
        if "information_schema.tables" in sql:
            return self.tables
        return self.columns

    async def aclose(self):
        self.closed = True


def _metric_row(**overrides):
    row = {
        "metric_id": "m1", "metric_name": "应收金额", "metric_type": "atomic",
        "definition_sql": "SUM(`amount_receivable`)",
        "source_table": "db_dws.dws_x", "grain": "stat_month",
        "parent_metrics": [], "tags": ["dws", "warehouse-bootstrap"],
        "active": True, "created_at": None, "updated_at": None,
        "full_count": 1,
    }
    row.update(overrides)
    return row


# ── list / CRUD ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_metrics_paginates(monkeypatch):
    from wren_agent.tools import pg_store
    pool = _Pool(fetch_rows=[
        _metric_row(full_count=2),
        _metric_row(metric_name="支付率", full_count=2),
    ])
    monkeypatch.setattr(pg_store, "get_pg_pool", lambda: pool)

    result = await list_metrics(search="应收", page=1, page_size=20)
    assert result["total"] == 2
    assert len(result["items"]) == 2
    assert result["items"][0]["metric_name"] == "应收金额"
    # filters reached the SQL
    assert "ILIKE" in pool.executed[0][1]


@pytest.mark.asyncio
async def test_create_metric_inserts(monkeypatch):
    from wren_agent.tools import pg_store
    pool = _Pool(fetch_rows=[{"metric_id": "new-1"}])
    monkeypatch.setattr(pg_store, "get_pg_pool", lambda: pool)

    result = await create_metric(MetricCreateRequest(
        metric_name="未收金额", metric_type="derived",
        definition_sql="a - b", source_table="db_dws.dws_x",
        grain="stat_month", parent_metrics=["应收金额"],
        tags=["auto"],
    ))
    assert result["metric_id"] == "new-1"
    sql, args = pool.executed[0][1], pool.executed[0][2]
    assert "INSERT INTO metric_registry" in sql
    assert args[0] == "未收金额"
    assert args[3] == "db_dws.dws_x"


@pytest.mark.asyncio
async def test_update_metric_partial(monkeypatch):
    from wren_agent.tools import pg_store
    pool = _Pool(execute_result="UPDATE 1")
    monkeypatch.setattr(pg_store, "get_pg_pool", lambda: pool)

    result = await update_metric("m1", MetricUpdateRequest(grain="org_id"))
    assert result["updated"] is True
    sql, args = pool.executed[0][1], pool.executed[0][2]
    assert "SET grain = $1" in sql
    assert args[0] == "org_id"


@pytest.mark.asyncio
async def test_update_metric_404(monkeypatch):
    from wren_agent.tools import pg_store
    pool = _Pool(execute_result="UPDATE 0")
    monkeypatch.setattr(pg_store, "get_pg_pool", lambda: pool)

    with pytest.raises(HTTPException) as exc:
        await update_metric("missing", MetricUpdateRequest(grain="x"))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_metric_soft(monkeypatch):
    from wren_agent.tools import pg_store
    pool = _Pool(execute_result="UPDATE 1")
    monkeypatch.setattr(pg_store, "get_pg_pool", lambda: pool)

    result = await delete_metric("m1")
    assert result["deleted"] is True
    sql, args = pool.executed[0][1], pool.executed[0][2]
    assert "active = FALSE" in sql


# ── wizard ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wizard_sources_lists_registry_with_layers_flag(monkeypatch):
    from wren_agent.tools import pg_store

    async def fake_list(**kwargs):
        return [
            {"id": "s1", "name": "wh", "db_type": "doris", "host": "h",
             "port": 9030, "database_name": "", "tags": [],
             "connection_params": {"layers": {"dws": ["db_dws"]}}},
            {"id": "s2", "name": "mysql1", "db_type": "mysql", "host": "h2",
             "port": 3306, "database_name": "biz", "tags": [],
             "connection_params": {}},
        ]

    monkeypatch.setattr(pg_store, "list_source_dbs", fake_list)
    result = await wizard_sources()
    assert result["sources"][0]["has_layers"] is True
    assert result["sources"][1]["has_layers"] is False


@pytest.mark.asyncio
async def test_wizard_databases_merges_layer_dbs(monkeypatch):
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
    result = await wizard_databases(source_id="s1")
    by_name = {d["name"]: d["layers"] for d in result["databases"]}
    assert by_name["wren_dw"] == ["dim", "dws"]  # deduped, layers merged
    assert by_name["db_dws"] == ["dws"]


@pytest.mark.asyncio
async def test_wizard_databases_404_for_unknown_source(monkeypatch):
    from wren_agent.tools import pg_store

    async def fake_creds(_id):
        return None

    monkeypatch.setattr(pg_store, "get_source_db_credentials", fake_creds)
    with pytest.raises(HTTPException) as exc:
        await wizard_databases(source_id="missing")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_wizard_tables_returns_comments(monkeypatch):
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
        ]))
    result = await wizard_tables(source_id="s1", database="db_dws")
    assert result["tables"][0]["table_name"] == "dws_x"
    assert result["tables"][0]["table_comment"] == "月度应收汇总表"


@pytest.mark.asyncio
async def test_wizard_columns_marks_metric_candidates(monkeypatch):
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
            {"COLUMN_NAME": "amount_receivable", "COLUMN_TYPE": "DECIMAL(19,4)",
             "COLUMN_COMMENT": "应收金额", "COLUMN_KEY": ""},
            {"COLUMN_NAME": "source_record_id", "COLUMN_TYPE": "BIGINT",
             "COLUMN_COMMENT": "源记录ID", "COLUMN_KEY": "PRI"},
            {"COLUMN_NAME": "stat_month", "COLUMN_TYPE": "VARCHAR(7)",
             "COLUMN_COMMENT": "月份", "COLUMN_KEY": ""},
        ]))
    result = await wizard_columns(source_id="s1", database="db_dws", table="dws_x")
    flags = {c["column_name"]: c["is_metric_candidate"] for c in result["columns"]}
    assert flags["amount_receivable"] is True
    assert flags["source_record_id"] is False   # id column
    assert flags["stat_month"] is False          # non-numeric
    assert result["columns"][1]["is_primary_key"] is True


# ── generate ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_metrics_creates_and_skips_duplicates(monkeypatch):
    from wren_agent.tools import pg_store
    from wren_agent.tools import db_explorer

    async def fake_creds(_id):
        return {"host": "h", "port": 9030, "database": "db_dws",
                "user": "root", "password": "x", "db_type": "doris",
                "connection_params": {}}

    monkeypatch.setattr(pg_store, "get_source_db_credentials", fake_creds)
    connector = _FakeConnector(columns=[
        {"COLUMN_NAME": "amount_receivable", "COLUMN_TYPE": "DECIMAL(19,4)",
         "COLUMN_COMMENT": "应收金额", "COLUMN_KEY": ""},
        {"COLUMN_NAME": "receipt_count", "COLUMN_TYPE": "BIGINT",
         "COLUMN_COMMENT": "收款笔数", "COLUMN_KEY": ""},
        {"COLUMN_NAME": "stat_month", "COLUMN_TYPE": "VARCHAR(7)",
         "COLUMN_COMMENT": "月份", "COLUMN_KEY": "PRI"},
    ])
    monkeypatch.setattr(db_explorer, "DBConnector", lambda **kw: connector)

    # registry already has 应收金额 on the same table → it will be skipped;
    # the per-row INSERT RETURNING yields one new id (one fetch for the
    # existing set, one fetchrow per fresh insert)
    pool = _Pool(fetch_sequence=[
        [{"source_table": "db_dws.dws_x", "metric_name": "应收金额"}],
        {"metric_id": "new-2"},
    ])
    monkeypatch.setattr(pg_store, "get_pg_pool", lambda: pool)

    result = await generate_metrics(MetricGenerateRequest(
        source_id="s1", database="db_dws", tables=["dws_x"],
    ))
    assert result["generated"] == 1       # 收款笔数
    assert result["skipped"] == 1         # 应收金额 duplicate
    created = [m for m in result["metrics"] if m["status"] == "created"]
    skipped = [m for m in result["metrics"] if m["status"] == "skipped"]
    assert created[0]["metric_name"] == "收款笔数"
    assert skipped[0]["reason"] == "duplicate"
    assert connector.closed is True
