"""Data Asset Catalog API — browse registered sources → databases → tables → columns.

The catalog is a read-only, global asset map in front of the Metric Library:
all registered sources (business DBs + Doris warehouses) are listed with their
databases, tables (with Chinese comments) and columns (name / type / comment).

Query logic deliberately reuses the wizard helpers from metrics.py — the catalog
is the same metadata, presented as a browsable map instead of a generation flow.
"""

import logging

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/catalog", tags=["Asset Catalog"])

# Reuse the wizard's source resolution + connector construction so the catalog
# stays on the same connection path (registered credentials only).
from wren_agent.api.metrics import _make_connector, _resolve_source

# Page-size cap mirrors the other paginated endpoints (metrics/dimensions).
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 50


def _pagination_sql(
    db_type: str, projection: str, table_ref: str, offset: int, count: int
) -> str:
    """Build a paged SELECT for the source dialect.

    Page numbers come from FastAPI-validated ints and are inlined directly —
    each driver uses a different bind placeholder style (:1 vs %s vs $1), so
    avoiding binds here sidesteps the dialect mismatch entirely.
    """
    if db_type in ("mysql", "doris"):
        return f"SELECT {projection} FROM {table_ref} LIMIT {offset}, {count}"
    if db_type in ("postgresql", "kingbase", "dm"):
        return f"SELECT {projection} FROM {table_ref} LIMIT {count} OFFSET {offset}"
    if db_type == "oracle":
        # Classic double-nested ROWNUM pagination (works on 11g and later).
        end = offset + count
        return (
            "SELECT * FROM ("
            f"  SELECT t.*, ROWNUM rn FROM (SELECT {projection} FROM {table_ref}) t "
            f"  WHERE ROWNUM <= {end}"
            f") WHERE rn > {offset}"
        )
    raise ValueError(f"Paged row browsing is not supported for {db_type}")


@router.get("/sources")
async def catalog_sources():
    """All active registered sources (business DBs + Doris warehouses)."""
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
            "table_count": row.get("table_count"),
            "tags": list(row.get("tags") or []),
        })
    return {"sources": sources}


@router.get("/databases")
async def catalog_databases(source_id: str = Query(...)):
    """Databases browsable under a source.

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


@router.get("/tables")
async def catalog_tables(
    source_id: str = Query(...),
    database: str = Query(...),
):
    """Tables of a database with Chinese comments."""
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


@router.get("/columns")
async def catalog_columns(
    source_id: str = Query(...),
    database: str = Query(...),
    table: str = Query(...),
):
    """Columns of a table: name / data type / Chinese comment."""
    creds = await _resolve_source(source_id)
    connector = _make_connector(creds, database)
    try:
        rows = await connector.query(
            "SELECT COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT "
            "FROM information_schema.columns "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
            (database, table),
        )
    finally:
        await connector.aclose()
    columns = [
        {"column_name": r["COLUMN_NAME"],
         "data_type": r.get("COLUMN_TYPE") or "",
         "column_comment": r.get("COLUMN_COMMENT") or ""}
        for r in rows
    ]
    return {"source_id": source_id, "database": database, "table": table,
            "columns": columns}


@router.get("/rows")
async def catalog_rows(
    source_id: str = Query(...),
    database: str = Query(...),
    table: str = Query(...),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
):
    """Paged rows of a table (50/page default) with a two-row column header.

    The header is derived from information_schema: field name + type on the
    first line, Chinese comment on the second. Page SQL is built per dialect
    (mysql/doris LIMIT a,b; pg/kingbase/dm LIMIT b OFFSET a; oracle ROWNUM).
    Sensitive columns (name-pattern detected) are returned with values replaced
    by [REDACTED] — the column stays in the layout, only values are masked.
    """
    from wren_agent.tools.db_explorer import detect_sensitive_field

    creds = await _resolve_source(source_id)
    db_type = creds.get("db_type", "mysql")
    if db_type == "mongodb":
        raise HTTPException(400, "Paged row browsing is not supported for mongodb")

    connector = _make_connector(creds, database)
    try:
        # 1. Column list — same query as /columns, keeps header order stable.
        col_rows = await connector.query(
            "SELECT COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT "
            "FROM information_schema.columns "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
            (database, table),
        )
        columns = [
            {"column_name": r["COLUMN_NAME"],
             "data_type": r.get("COLUMN_TYPE") or "",
             "column_comment": r.get("COLUMN_COMMENT") or ""}
            for r in col_rows
        ]
        col_names = [c["column_name"] for c in columns]

        # 2. Total row count for pagination.
        table_ref = connector._quote_identifier(table)
        total = await connector.query_value(
            f"SELECT COUNT(*) FROM {table_ref}"
        ) or 0

        # 3. Paged data (page numbers are validated ints, inlined on purpose —
        #    each driver uses a different bind style, see _pagination_sql).
        offset = (page - 1) * page_size
        projection = ", ".join(
            connector._quote_identifier(c) for c in col_names
        )
        sql = _pagination_sql(db_type, projection, table_ref, offset, page_size)
        rows = await connector.query(sql)

        # 4. Mask sensitive values (keep the column, drop the raw value).
        sensitive = {
            c["column_name"]
            for c in columns
            if detect_sensitive_field(table, c["column_name"]) is not None
        }
        redacted: list[str] = []
        safe_rows: list[dict] = []
        for row in rows:
            safe: dict = {}
            for name, value in row.items():
                if name in sensitive:
                    safe[name] = "[REDACTED]"
                    if name not in redacted:
                        redacted.append(name)
                else:
                    safe[name] = value
            safe_rows.append(safe)
    finally:
        await connector.aclose()

    return {
        "columns": columns,
        "rows": safe_rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "redacted_fields": redacted,
    }
