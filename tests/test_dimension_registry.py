"""dimension_registry storage-layer tests (pg_store functions).

Pure function-level tests with a mocked pool — no real PG. Covers the
functions added for the dimension library CRUD + the pre-existing
register_dimension conflict semantics.
"""

import pytest

from wren_agent.tools.pg_store import (
    paginate_dimension_registry,
    register_dimension,
    soft_delete_dimension,
    update_dimension,
)


class _Pool:
    """Configurable fake PG pool recording every call."""

    def __init__(self, fetch_rows=None, fetchrow=None, fetchrow_sequence=None,
                 execute_result="UPDATE 1"):
        self.fetch_rows = fetch_rows or []
        self.fetchrow_result = fetchrow
        self.fetchrow_sequence = fetchrow_sequence  # popped per fetchrow call
        self.execute_result = execute_result
        self.executed = []

    def acquire(self):
        return _Conn(self)


class _Conn:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None

    async def fetch(self, sql, *_args):
        self.pool.executed.append(("fetch", sql, _args))
        return self.pool.fetch_rows

    async def fetchrow(self, sql, *_args):
        self.pool.executed.append(("fetchrow", sql, _args))
        if self.pool.fetchrow_sequence:
            return self.pool.fetchrow_sequence.pop(0)
        return self.pool.fetchrow_result

    async def execute(self, sql, *args):
        self.pool.executed.append(("execute", sql, args))
        return self.pool.execute_result


def _dim_row(**overrides):
    row = {
        "dim_id": "d1", "dim_name": "商品维", "dim_table": "dim_room",
        "db_name": "db_dim", "owner_task_id": "t1",
        "natural_keys": ["source_room_id"],
        "grain": "source_room_id", "fields": [{"name": "room_id"}],
        "variant_of": None, "version": 1, "active": True,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_register_dimension_created(monkeypatch):
    """No existing row → INSERT, returns created with dim_id + db_name."""
    # first fetchrow (conflict check) → None; second (INSERT RETURNING) → row
    monkeypatch.setattr(
        "wren_agent.tools.pg_store.get_pg_pool",
        lambda: _Pool(fetchrow_sequence=[None, {"dim_id": "d-new", "version": 1}]),
    )
    result = await register_dimension(
        dim_name="商品维", dim_table="dim_room", owner_task_id="t1",
        natural_keys=["source_room_id"], grain="source_room_id",
        fields=[{"name": "room_id"}], db_name="db_dim",
    )
    assert result["status"] == "created"
    assert result["dim_id"] == "d-new"
    assert result["version"] == 1


@pytest.mark.asyncio
async def test_register_dimension_conflict_by_dim_table(monkeypatch):
    """Same dim_table AND db_name already active → conflict, no insert."""
    pool = _Pool(fetchrow={
        "dim_name": "商品维", "version": 1, "owner_task_id": "t-old"})
    monkeypatch.setattr(
        "wren_agent.tools.pg_store.get_pg_pool", lambda: pool)
    result = await register_dimension(
        dim_name="商品维", dim_table="dim_room", owner_task_id="t2",
        natural_keys=["source_room_id"], grain="source_room_id",
        fields=[], db_name="db_dim",
    )
    assert result["status"] == "conflict"
    assert result["existing_dim"]["owner_task_id"] == "t-old"
    sql, args = pool.executed[0][1], pool.executed[0][2]
    assert "dimension_registry WHERE dim_table = $1 AND db_name = $2 AND active = TRUE" in sql
    assert tuple(args) == ("dim_room", "db_dim")
    # conflict path must not run an INSERT
    assert not any("INSERT INTO dimension_registry" in e[1] for e in pool.executed)


@pytest.mark.asyncio
async def test_register_dimension_same_table_different_db_not_conflict(monkeypatch):
    """Same dim_table in a DIFFERENT database must not collide on dedupe."""
    pool = _Pool(fetchrow_sequence=[None, {"dim_id": "d-new", "version": 1}])
    monkeypatch.setattr(
        "wren_agent.tools.pg_store.get_pg_pool", lambda: pool)
    result = await register_dimension(
        dim_name="公司维", dim_table="dim_company", owner_task_id="t2",
        natural_keys=["company_id"], grain="company_id",
        fields=[], db_name="db_other",
    )
    assert result["status"] == "created"
    sql, args = pool.executed[0][1], pool.executed[0][2]
    assert "AND db_name = $2" in sql
    assert tuple(args) == ("dim_company", "db_other")


@pytest.mark.asyncio
async def test_paginate_dimension_registry_filters(monkeypatch):
    """ILIKE search filters + COUNT(*) OVER + LIMIT/OFFSET, parameterized."""
    pool = _Pool(fetch_rows=[
        dict(_dim_row(), full_count=1),
    ])
    monkeypatch.setattr(
        "wren_agent.tools.pg_store.get_pg_pool", lambda: pool)
    result = await paginate_dimension_registry(
        search="商品", dim_table_like="dim_room", page=2, page_size=10)
    assert result["total"] == 1
    assert len(result["items"]) == 1
    assert result["items"][0]["dim_name"] == "商品维"
    sql, args = pool.executed[0][1], pool.executed[0][2]
    assert "COUNT(*) OVER()" in sql
    assert "dim_name ILIKE $1" in sql
    assert "dim_table ILIKE $2" in sql
    assert tuple(args) == ("%商品%", "%dim_room%", 10, 10)  # page 2 → offset 10


@pytest.mark.asyncio
async def test_paginate_dimension_registry_db_filter(monkeypatch):
    """db_name filter narrows the WHERE clause and binds the parameter."""
    pool = _Pool(fetch_rows=[
        dict(_dim_row(), full_count=1),
    ])
    monkeypatch.setattr(
        "wren_agent.tools.pg_store.get_pg_pool", lambda: pool)
    result = await paginate_dimension_registry(db_name="db_dim", page=1, page_size=20)
    assert result["total"] == 1
    sql, args = pool.executed[0][1], pool.executed[0][2]
    assert "db_name = $1" in sql
    assert tuple(args) == ("db_dim", 20, 0)


@pytest.mark.asyncio
async def test_update_dimension_whitelist(monkeypatch):
    """Non-None whitelisted fields update; disallowed/None fields skipped."""
    pool = _Pool()
    monkeypatch.setattr(
        "wren_agent.tools.pg_store.get_pg_pool", lambda: pool)
    ok = await update_dimension(
        "d1",
        dim_name="商品维2",
        db_name="db_dim",
        natural_keys=["room_id"],
        evil_column="DROP TABLE dimension_registry",
        version=None,
    )
    assert ok is True
    sql, args = pool.executed[0][1], pool.executed[0][2]
    assert "dim_name = $1" in sql
    assert "db_name = $2" in sql
    assert "natural_keys = $3" in sql
    assert "evil_column" not in sql
    assert "version" not in sql
    assert "updated_at = NOW()" in sql
    assert "WHERE dim_id = $4 AND active = TRUE" in sql
    assert tuple(args) == ("商品维2", "db_dim", ["room_id"], "d1")


@pytest.mark.asyncio
async def test_update_dimension_no_fields_returns_false(monkeypatch):
    pool = _Pool()
    monkeypatch.setattr(
        "wren_agent.tools.pg_store.get_pg_pool", lambda: pool)
    ok = await update_dimension("d1", version=None)
    assert ok is False
    assert pool.executed == []


@pytest.mark.asyncio
async def test_soft_delete_dimension(monkeypatch):
    pool = _Pool()
    monkeypatch.setattr(
        "wren_agent.tools.pg_store.get_pg_pool", lambda: pool)
    ok = await soft_delete_dimension("d1")
    assert ok is True
    sql, args = pool.executed[0][1], pool.executed[0][2]
    assert "active = FALSE" in sql
    assert "WHERE dim_id = $1 AND active = TRUE" in sql
    assert args == ("d1",)


@pytest.mark.asyncio
async def test_soft_delete_dimension_missing(monkeypatch):
    pool = _Pool(execute_result="UPDATE 0")
    monkeypatch.setattr(
        "wren_agent.tools.pg_store.get_pg_pool", lambda: pool)
    ok = await soft_delete_dimension("d1")
    assert ok is False
