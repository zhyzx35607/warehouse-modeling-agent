"""Metric Library API — list/CRUD + generation wizard.

The metric_registry (PG) is the central registry of data metrics. Metrics
come from three sources:
- manual creation (POST /api/v1/metrics)
- approved-task registration (_register_approved_assets in approval.py)
- the generation wizard below: scan a source database's tables' numeric
  columns with Chinese comments → atomic metrics (idempotent).

Wizard only surfaces REGISTERED source databases (task-inline sources are
not resolvable here by design).
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/metrics", tags=["Metrics"])


# ── Request / response models ───────────────────────────────────


class MetricCreateRequest(BaseModel):
    metric_name: str = Field(..., max_length=120)
    metric_type: str = Field(default="atomic", pattern="^(atomic|derived)$")
    definition_sql: str = Field(..., max_length=5000)
    source_table: str = Field(..., max_length=255)
    grain: str = Field(default="", max_length=500)
    parent_metrics: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class MetricUpdateRequest(BaseModel):
    metric_name: Optional[str] = Field(default=None, max_length=120)
    metric_type: Optional[str] = Field(default=None, pattern="^(atomic|derived)$")
    definition_sql: Optional[str] = Field(default=None, max_length=5000)
    source_table: Optional[str] = Field(default=None, max_length=255)
    grain: Optional[str] = Field(default=None, max_length=500)
    parent_metrics: Optional[list[str]] = None
    tags: Optional[list[str]] = None


class MetricGenerateRequest(BaseModel):
    source_id: str = Field(...)
    database: str = Field(...)
    tables: list[str] = Field(..., min_length=1, max_length=200)


def _metric_item(row: dict) -> dict:
    return {
        "metric_id": str(row["metric_id"]),
        "metric_name": row["metric_name"],
        "metric_type": row["metric_type"],
        "definition_sql": row["definition_sql"],
        "source_table": row["source_table"],
        "grain": row["grain"],
        "parent_metrics": list(row.get("parent_metrics") or []),
        "tags": list(row.get("tags") or []),
        "active": row["active"],
        "created_at": str(row["created_at"]) if row.get("created_at") else None,
        "updated_at": str(row["updated_at"]) if row.get("updated_at") else None,
    }


async def _resolve_source(source_id: str) -> dict:
    """Resolve registered source credentials or raise 404/400."""
    from wren_agent.tools.pg_store import get_source_db_credentials

    creds = await get_source_db_credentials(source_id)
    if not creds:
        raise HTTPException(404, f"Source DB '{source_id}' not found in registry")
    if not creds.get("user"):
        raise HTTPException(400, f"Source DB '{source_id}' has no credentials")
    return creds


def _make_connector(creds: dict, database: str):
    from wren_agent.tools.db_explorer import DBConnector

    return DBConnector(
        host=creds["host"], port=creds["port"],
        user=creds["user"], password=creds["password"],
        database=database or creds.get("database") or "",
        db_type=creds.get("db_type", "mysql"),
        connection_params=creds.get("connection_params") or {},
    )


# ── CRUD ────────────────────────────────────────────────────────


@router.get("")
async def list_metrics(
    search: str = Query(default=""),
    source_table_like: str = Query(default=""),
    metric_type: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
):
    """Paginated metric list with search / source-table / type filters."""
    from wren_agent.tools.pg_store import paginate_metric_registry

    result = await paginate_metric_registry(
        search=search, source_table_like=source_table_like,
        metric_type=metric_type, page=page, page_size=page_size,
    )
    return {
        "total": result["total"],
        "page": page,
        "page_size": page_size,
        "items": [_metric_item(r) for r in result["items"]],
    }


@router.post("", status_code=201)
async def create_metric(request: MetricCreateRequest):
    """Manually register a metric."""
    from wren_agent.tools.pg_store import get_pg_pool

    pool = get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO metric_registry "
            "(metric_name, metric_type, definition_sql, source_table, "
            " owner_task_id, grain, parent_metrics, tags) "
            "VALUES ($1, $2, $3, $4, 'manual', $5, $6, $7) "
            "RETURNING metric_id",
            request.metric_name, request.metric_type, request.definition_sql,
            request.source_table, request.grain,
            request.parent_metrics, request.tags,
        )
    return {"metric_id": str(row["metric_id"]), "status": "created"}


@router.put("/{metric_id}")
async def update_metric(metric_id: str, request: MetricUpdateRequest):
    """Partial update of a metric (non-None fields)."""
    from wren_agent.tools.pg_store import update_metric as pg_update

    updated = await pg_update(metric_id, **request.model_dump())
    if not updated:
        raise HTTPException(404, f"Metric '{metric_id}' not found")
    return {"metric_id": metric_id, "updated": True}


@router.delete("/{metric_id}")
async def delete_metric(metric_id: str):
    """Soft-delete a metric (active=FALSE)."""
    from wren_agent.tools.pg_store import soft_delete_metric

    deleted = await soft_delete_metric(metric_id)
    if not deleted:
        raise HTTPException(404, f"Metric '{metric_id}' not found")
    return {"metric_id": metric_id, "deleted": True}


# ── Generation wizard ───────────────────────────────────────────


@router.get("/wizard/sources")
async def wizard_sources():
    """Registered source databases for wizard step 1."""
    from wren_agent.tools.pg_store import list_source_dbs

    rows = await list_source_dbs(status="active")
    sources = []
    for row in rows:
        cp = row.get("connection_params") or {}
        if isinstance(cp, str):
            import json as _json
            try:
                cp = _json.loads(cp)
            except (TypeError, ValueError):
                cp = {}
        layers = cp.get("layers") or {}
        sources.append({
            "id": str(row["id"]),
            "name": row["name"],
            "db_type": row["db_type"],
            "host": row["host"],
            "port": row["port"],
            "database_name": row.get("database_name") or "",
            "has_layers": bool(isinstance(layers, dict) and layers),
            "tags": list(row.get("tags") or []),
        })
    return {"sources": sources}


@router.get("/wizard/databases")
async def wizard_databases(source_id: str = Query(...)):
    """Databases selectable for a source (wizard step 2).

    Warehouse (doris) sources: layer mapping values, deduplicated with the
    layer names attached. Other sources: the single configured database.
    """
    creds = await _resolve_source(source_id)
    layers = (creds.get("connection_params") or {}).get("layers") or {}
    if isinstance(layers, dict) and layers:
        by_db: dict[str, set[str]] = {}
        for layer, raw in layers.items():
            dbs = raw if isinstance(raw, list) else ([raw] if raw else [])
            for db in dbs:
                db = str(db or "").strip()
                if db:
                    by_db.setdefault(db, set()).add(str(layer))
        databases = [
            {"name": db, "layers": sorted(layer_names)}
            for db, layer_names in sorted(by_db.items())
        ]
    else:
        db = (creds.get("database") or "").strip()
        databases = [{"name": db, "layers": []}] if db else []
    return {"source_id": source_id, "databases": databases}


@router.get("/wizard/tables")
async def wizard_tables(
    source_id: str = Query(...),
    database: str = Query(...),
):
    """Tables of a database with comments (wizard step 3)."""
    creds = await _resolve_source(source_id)
    connector = _make_connector(creds, database)
    try:
        rows = await connector.query(
            "SELECT TABLE_NAME, TABLE_COMMENT FROM information_schema.tables "
            "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE' "
            "ORDER BY TABLE_NAME",
            (database,),
        )
    finally:
        await connector.aclose()
    tables = [
        {"table_name": r["TABLE_NAME"],
         "table_comment": r.get("TABLE_COMMENT") or ""}
        for r in rows
    ]
    return {"source_id": source_id, "database": database, "tables": tables}


@router.get("/wizard/columns")
async def wizard_columns(
    source_id: str = Query(...),
    database: str = Query(...),
    table: str = Query(...),
):
    """Columns of a table with comments + metric-candidate flag."""
    from wren_agent.tools.metric_bootstrap import is_metric_candidate

    creds = await _resolve_source(source_id)
    connector = _make_connector(creds, database)
    try:
        rows = await connector.query(
            "SELECT COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT, "
            "COLUMN_KEY FROM information_schema.columns "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
            (database, table),
        )
    finally:
        await connector.aclose()
    columns = []
    for r in rows:
        name = r["COLUMN_NAME"]
        dtype = r.get("COLUMN_TYPE") or ""
        columns.append({
            "column_name": name,
            "data_type": dtype,
            "column_comment": r.get("COLUMN_COMMENT") or "",
            "is_primary_key": str(r.get("COLUMN_KEY") or "").upper() in ("PRI", "UNI"),
            "is_metric_candidate": is_metric_candidate(name, dtype),
        })
    return {"source_id": source_id, "database": database, "table": table,
            "columns": columns}


@router.post("/generate")
async def generate_metrics(request: MetricGenerateRequest):
    """Scan selected tables and register atomic metrics (idempotent).

    Numeric columns with Chinese comments become metrics; columns already
    registered for the same (source_table, metric_name) are skipped.
    """
    from wren_agent.tools.metric_bootstrap import (
        _bootstrap_metric_rows_from_columns,
        register_metric_rows,
    )

    creds = await _resolve_source(request.source_id)

    # 1. Collect columns of all selected tables
    columns: list[dict] = []
    connector = _make_connector(creds, request.database)
    try:
        for table in request.tables:
            rows = await connector.query(
                "SELECT COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT, "
                "COLUMN_KEY FROM information_schema.columns "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
                (request.database, table),
            )
            pk_names = [
                r["COLUMN_NAME"] for r in rows
                if str(r.get("COLUMN_KEY") or "").upper() in ("PRI", "UNI")
            ]
            for r in rows:
                columns.append({
                    "source_table": f"{request.database}.{table}",
                    "column_name": r["COLUMN_NAME"],
                    "data_type": r.get("COLUMN_TYPE") or "",
                    "comment": r.get("COLUMN_COMMENT") or "",
                    "pk_names": pk_names,
                    "layer": "",
                })
    finally:
        await connector.aclose()

    # 2. Build metric rows and batch-register via the shared write path
    metric_rows = _bootstrap_metric_rows_from_columns(columns)
    result = await register_metric_rows(metric_rows, owner="manual-generation")
    return {
        "generated": result["registered"],
        "skipped": len(result["skipped_items"]),
        "metrics": result["inserted"] + result["skipped_items"],
    }
