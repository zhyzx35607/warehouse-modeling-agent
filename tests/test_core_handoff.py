from contextlib import asynccontextmanager

import pytest
from pydantic import ValidationError

from wren_agent.api.routes import CreateTaskRequest
from wren_agent.tools import core_handoff, pg_store


def _request(**overrides):
    values = dict(modeling_goal="goal", source_databases=[], dw_target={},
                  source_mode="business_db", warehouse_source_id="wh-1",
                  landing={"ods": "db_ods", "dim": "db_dim", "dwd": "db_dwd"})
    values.update(overrides)
    return CreateTaskRequest(**values)


def test_business_task_requires_explicit_warehouse_and_three_landings():
    with pytest.raises(ValidationError, match="warehouse_source_id"):
        _request(warehouse_source_id="")
    with pytest.raises(ValidationError, match="dwd"):
        _request(landing={"ods": "o", "dim": "d"})
    with pytest.raises(ValidationError, match="identifiers"):
        _request(landing={"ods": "db-ods", "dim": "d", "dwd": "w"})


def test_warehouse_task_keeps_existing_contract():
    request = CreateTaskRequest(modeling_goal="goal", source_databases=[], dw_target={},
                                source_mode="warehouse", landing={"dws": "x", "ads": "y"})
    assert request.warehouse_source_id == ""


def test_business_task_type_splits_ods_and_core_model():
    assert _request(business_task_type="ods_migration").business_task_type == "ods_migration"
    assert _request(business_task_type="core_model").business_task_type == "core_model"
    assert _request(business_task_type="ods_migration", landing={"ods": "db_ods"}).landing == {"ods": "db_ods"}
    with pytest.raises(ValidationError, match="dim"):
        _request(business_task_type="core_model", landing={"ods": "db_ods"})
    with pytest.raises(ValidationError, match="only valid"):
        CreateTaskRequest(modeling_goal="goal", source_databases=[], dw_target={},
                          source_mode="warehouse", business_task_type="core_model")


class _Tx:
    async def __aenter__(self): return self
    async def __aexit__(self, *_args): return None


class _Conn:
    def __init__(self): self.updated = None
    def transaction(self): return _Tx()
    async def fetchrow(self, *_args):
        return {"db_type": "doris", "connection_params": '{"layers":{"dim":"old_dim"}}'}
    async def execute(self, _sql, _db_id, payload): self.updated = payload


class _Pool:
    def __init__(self, conn): self.conn = conn
    @asynccontextmanager
    async def acquire(self): yield self.conn


@pytest.mark.asyncio
async def test_merge_layers_appends_without_overwriting(monkeypatch):
    conn = _Conn()
    monkeypatch.setattr(pg_store, "get_pg_pool", lambda: _Pool(conn))
    layers = await pg_store.merge_source_db_layers("wh-1", {"dim": "new_dim", "dwd": "new_dwd"})
    assert layers == {"dim": ["old_dim", "new_dim"], "dwd": "new_dwd"}
    assert "old_dim" in conn.updated and "new_dim" in conn.updated


@pytest.mark.asyncio
async def test_quality_failure_prevents_publication():
    result = await core_handoff.publish_core_assets({
        "source_mode": "business_db", "quality_checks": {"failed": 1},
    }, 1)
    assert result == {"published": False, "reason": "quality_gate_failed"}


@pytest.mark.asyncio
async def test_ensure_databases_uses_explicit_registered_target(monkeypatch):
    calls = []
    async def credentials(_source_id):
        return {"db_type": "doris", "host": "h", "port": 9030, "user": "u",
                "password": "p", "database": "default"}
    class Client:
        def __init__(self, **kwargs): calls.append(kwargs)
        async def execute_ddl(self, sql): calls.append(sql)
    monkeypatch.setattr(pg_store, "get_source_db_credentials", credentials)
    monkeypatch.setattr("wren_agent.tools.doris_client.DorisClient", Client)
    await core_handoff.ensure_core_databases({
        "create_missing_databases": True, "warehouse_source_id": "wh-1",
        "landing": {"ods": "db_ods", "dim": "db_dim", "dwd": "db_dwd"},
    })
    assert calls[0]["host"] == "h"
    # Connect to the task's ODS landing, never the registry default catalog
    # (task ac119218: `dwd_ecommerce_orders` → pymysql 1049).
    assert calls[0]["database"] == "db_ods"
    assert calls[0]["database"] != "default"
    assert calls[1:] == [
        "CREATE DATABASE IF NOT EXISTS `db_ods`",
        "CREATE DATABASE IF NOT EXISTS `db_dim`",
        "CREATE DATABASE IF NOT EXISTS `db_dwd`",
    ]


@pytest.mark.asyncio
async def test_ensure_databases_retries_without_catalog_when_ods_missing(monkeypatch):
    inits = []
    class Client:
        def __init__(self, **kwargs):
            inits.append(kwargs)
            self.database = kwargs.get("database")
        async def execute_ddl(self, sql):
            if self.database == "missing_ods":
                raise RuntimeError("(1049, \"Unknown database 'missing_ods'\")")
            inits.append(sql)
    async def credentials(_source_id):
        return {"db_type": "doris", "host": "h", "port": 9030, "user": "u",
                "password": "p", "database": "dwd_ecommerce_orders"}
    monkeypatch.setattr(pg_store, "get_source_db_credentials", credentials)
    monkeypatch.setattr("wren_agent.tools.doris_client.DorisClient", Client)
    await core_handoff.ensure_core_databases({
        "create_missing_databases": True, "warehouse_source_id": "wh-1",
        "landing": {"ods": "missing_ods", "dim": "db_dim", "dwd": "db_dwd"},
    })
    assert inits[0]["database"] == "missing_ods"
    assert inits[1]["database"] == ""
    assert inits[0]["database"] != "dwd_ecommerce_orders"
    assert inits[2:] == [
        "CREATE DATABASE IF NOT EXISTS `missing_ods`",
        "CREATE DATABASE IF NOT EXISTS `db_dim`",
        "CREATE DATABASE IF NOT EXISTS `db_dwd`",
    ]
