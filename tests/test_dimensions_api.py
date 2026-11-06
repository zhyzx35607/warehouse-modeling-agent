"""Dimension Library API tests — CRUD + history scan.

Handlers are tested directly (no HTTP server); pg access is mocked with a
fake pool. The scan test mocks the persisted-design-plan source and the
registration layer, asserting dedupe by dim_table.
"""

import pytest

from wren_agent.api.dimensions import (
    create_dimension,
    delete_dimension,
    list_dimensions,
    scan_history_dimensions,
    update_dimension,
)
from wren_agent.api.dimensions import (
    DimensionCreateRequest,
    DimensionScanRequest,
    DimensionUpdateRequest,
)
from wren_agent.models.design import DWDesignPlan
from fastapi import HTTPException


class _Pool:
    """Fake PG pool: fetch() returns canned rows, records executed SQL."""

    def __init__(self, fetch_rows=None):
        self.fetch_rows = fetch_rows or []
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
        return self.pool.fetch_rows[0] if self.pool.fetch_rows else None


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
async def test_list_dimensions(monkeypatch):
    from wren_agent.tools import pg_store

    monkeypatch.setattr(
        "wren_agent.tools.pg_store.get_pg_pool",
        lambda: _Pool(fetch_rows=[dict(_dim_row(), full_count=1)]),
    )
    result = await list_dimensions(search="商品", page=1, page_size=20)
    assert result["total"] == 1
    assert result["items"][0]["dim_id"] == "d1"
    assert result["items"][0]["dim_name"] == "商品维"


@pytest.mark.asyncio
async def test_create_dimension(monkeypatch):
    registered = []

    async def fake_register_dimension(**kwargs):
        registered.append(kwargs)
        return {"status": "created", "dim_id": "d-new", "version": 1}

    monkeypatch.setattr(
        "wren_agent.tools.pg_store.register_dimension", fake_register_dimension)
    req = DimensionCreateRequest(
        dim_name="商品维", dim_table="dim_room", db_name="db_dim",
        natural_keys=["source_room_id"], grain="source_room_id",
        fields=[{"name": "room_id"}],
    )
    result = await create_dimension(req)
    assert result == {"dim_id": "d-new", "status": "created"}
    assert registered[0]["owner_task_id"] == "manual"
    assert registered[0]["dim_table"] == "dim_room"
    assert registered[0]["db_name"] == "db_dim"


@pytest.mark.asyncio
async def test_create_dimension_conflict_409(monkeypatch):
    async def fake_register_dimension(**kwargs):
        return {"status": "conflict", "existing_dim": {"dim_name": "商品维"}}

    monkeypatch.setattr(
        "wren_agent.tools.pg_store.register_dimension", fake_register_dimension)
    req = DimensionCreateRequest(dim_name="商品维", dim_table="dim_room")
    with pytest.raises(HTTPException) as exc:
        await create_dimension(req)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_update_dimension(monkeypatch):
    async def fake_update(dim_id, **fields):
        return True

    monkeypatch.setattr(
        "wren_agent.tools.pg_store.update_dimension", fake_update)
    req = DimensionUpdateRequest(dim_name="商品维2")
    result = await update_dimension("d1", req)
    assert result == {"dim_id": "d1", "updated": True}


@pytest.mark.asyncio
async def test_update_dimension_missing_404(monkeypatch):
    async def fake_update(dim_id, **fields):
        return False

    monkeypatch.setattr(
        "wren_agent.tools.pg_store.update_dimension", fake_update)
    req = DimensionUpdateRequest(dim_name="x")
    with pytest.raises(HTTPException) as exc:
        await update_dimension("d-missing", req)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_dimension(monkeypatch):
    async def fake_delete(dim_id):
        return True

    monkeypatch.setattr(
        "wren_agent.tools.pg_store.soft_delete_dimension", fake_delete)
    result = await delete_dimension("d1")
    assert result == {"dim_id": "d1", "deleted": True}


@pytest.mark.asyncio
async def test_scan_history_dimensions(monkeypatch):
    """Design plans with DIM designs are scanned; dup dim_tables are skipped."""
    plan = DWDesignPlan(task_id="t-hist", modeling_goal="g")
    from wren_agent.models.design import DIMDesign

    plan.dim_designs = [
        DIMDesign(dim_name="商品维", dim_table="dim_room",
                  source="from_source_table", source_table="t_room",
                  natural_key=["source_room_id"], fields=[{"name": "room_id"}]),
        DIMDesign(dim_name="客户维", dim_table="dim_customer",
                  source="from_source_table", source_table="t_customer",
                  natural_key=["source_customer_id"]),
    ]
    artifacts = [
        {"task_id": "t-hist", "design_plan": plan.model_dump()},
        {"task_id": "t-dup", "design_plan": plan.model_dump()},  # same dim_tables
    ]

    registered = []

    async def fake_register_dimension(**kwargs):
        registered.append(kwargs)
        return {"status": "created", "dim_id": "d-scan", "version": 1}

    async def fake_list_designs(limit=None):
        return artifacts

    monkeypatch.setattr(
        "wren_agent.tools.pg_store.list_task_artifacts_designs", fake_list_designs)
    # existing baseline: dim_room already active → first task registers
    # dim_customer only; second task's dim_customer is then skipped
    monkeypatch.setattr(
        "wren_agent.tools.pg_store.get_pg_pool",
        lambda: _Pool(fetch_rows=[{"dim_table": "dim_room"}]),
    )
    monkeypatch.setattr(
        "wren_agent.tools.pg_store.register_dimension", fake_register_dimension)

    result = await scan_history_dimensions(DimensionScanRequest(limit=100))
    assert result["scanned_tasks"] == 2
    assert result["registered"] == 1
    assert result["skipped"] == 3
    by_table: dict[str, list[str]] = {}
    for r in result["dimensions"]:
        by_table.setdefault(r["dim_table"], []).append(r["status"])
    # dim_room: pre-existing in registry → both occurrences skipped
    assert by_table["dim_room"] == ["skipped", "skipped"]
    # dim_customer: first occurrence created, duplicate then skipped
    assert by_table["dim_customer"] == ["created", "skipped"]
    # owner_task_id preserves provenance
    assert registered[0]["owner_task_id"] == "t-hist"


@pytest.mark.asyncio
async def test_scan_history_dimensions_empty(monkeypatch):
    async def fake_list_designs(limit=None):
        return []

    monkeypatch.setattr(
        "wren_agent.tools.pg_store.list_task_artifacts_designs", fake_list_designs)
    monkeypatch.setattr(
        "wren_agent.tools.pg_store.get_pg_pool",
        lambda: _Pool(fetch_rows=[]),
    )
    result = await scan_history_dimensions(DimensionScanRequest())
    assert result == {"scanned_tasks": 0, "registered": 0,
                      "skipped": 0, "dimensions": []}
