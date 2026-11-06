"""REST API for Source Database Registry management.

Only GET and POST methods — consistent with the design constraint.
- GET  /api/v1/source-dbs          → list all
- GET  /api/v1/source-dbs/{id}     → get one
- POST /api/v1/source-dbs          → create/update (upsert on name)
- POST /api/v1/source-dbs/{id}/test → test connectivity
- POST /api/v1/source-dbs/{id}/delete → delete (via POST with action)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/source-dbs", tags=["Source Databases"])


# ── Request/Response models ─────────────────────────────────────

class SourceDBCreateRequest(BaseModel):
    name: str = Field(..., description="Unique name (e.g. 'ecommerce_mysql')")
    description: str = Field(default="", description="Human-readable description")
    db_type: str = Field(default="mysql", description="mysql|postgresql|oracle|dm|kingbase|mongodb|doris")
    host: str = Field(..., description="Host address")
    port: int = Field(default=3306)
    database_name: str = Field(..., description="Database/schema name")
    user_name: str = Field(default="", description="Database user")
    password: str = Field(default="", description="Database password for testing")
    credential_ref: str = Field(default="", description="Secret Manager key reference")
    tags: list[str] = Field(default_factory=list, description="Business tags")
    business_desc: str = Field(default="", description="Business domain description for LLM context")
    connection_params: dict = Field(
        default_factory=dict,
        description='Driver-level options, e.g. {"use_ssl": false, "charset": "utf8mb4"}. '
                    'MySQL defaults to SSL disabled; set use_ssl=true to opt in.',
    )


class SourceDBResponse(BaseModel):
    id: str
    name: str
    description: str
    db_type: str
    host: str
    port: int
    database_name: str
    user_name: str
    credential_ref: str
    tags: list[str]
    business_desc: str
    connection_params: dict = Field(default_factory=dict)
    table_count: Optional[int] = None
    last_explored_at: Optional[str] = None
    status: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class DeleteRequest(BaseModel):
    action: str = "delete"


# ── Warehouse source helpers (db_type=doris) ────────────────────

WAREHOUSE_LAYERS = ("ods", "dim", "dwd", "dwm", "dws", "ads")


def _validate_doris_layers(connection_params: dict) -> list[str]:
    """Validate a doris warehouse source: a layers mapping with >= 1 database.

    One cluster = one registration; `connection_params.layers` maps layers
    to one or more databases (one database may host several layers, e.g.
    wren_dw; one layer may host several databases after auto-discovery).
    Individual layers may be empty — a cluster legitimately may lack a dwm
    layer, and stale-entry removal can empty one — only at least one mapped
    database overall is required. Values may be a single str or a list[str].
    Returns the deduplicated list of layer databases.
    Raises ValueError on invalid config.
    """
    layers = (connection_params or {}).get("layers")
    if not isinstance(layers, dict) or not layers:
        raise ValueError(
            "doris 数仓源必须在 connection_params.layers 提供层→库映射"
        )
    dbs: list[str] = []
    for layer in WAREHOUSE_LAYERS:
        vals = layers.get(layer)
        if vals is None:
            continue  # layer not mapped at all
        if isinstance(vals, list):
            vals = [str(v).strip() for v in vals if str(v).strip()]
        else:
            vals = [str(vals).strip()] if str(vals).strip() else []
        for db in vals:
            if db not in dbs:
                dbs.append(db)
    if not dbs:
        raise ValueError("doris 数仓源 layers 未映射任何数据库")
    return dbs


# ── Warehouse auto-discovery (T-13) ─────────────────────────────

# Layer keyword -> table-name prefixes. Newly discovered databases are
# classified by the dominant table prefix (>=50% of classified tables);
# the database name keyword is a weaker secondary signal.
_LAYER_PREFIXES: dict[str, list[str]] = {
    "ods": ["ods_", "ods"],
    "dim": ["dim_", "dim"],
    "dwd": ["dwd_", "dwd"],
    "dwm": ["dwm_", "dwm"],
    "dws": ["dws_", "dws"],
    "ads": ["ads_", "ads"],
}

# Cluster-internal databases never treated as business domains
_SYSTEM_DBS = {
    "information_schema", "mysql", "performance_schema", "sys",
    "__internal_schema", "__statistics__", "_statistics_",
}


async def _classify_warehouse_db(
    host: str, port: int, user: str, password: str,
    connection_params: dict, db: str,
) -> str | None:
    """Guess the layer of a newly discovered Doris database.

    Signals, strongest first:
    1. Table-name prefixes (dim_/dwd_/dws_/ods_/ads_/dwm_) — the dominant
       prefix (>=50% of classified tables) wins.
    2. Database-name keyword (ods/dim/dwd/dwm/dws/ads).
    Returns None when no signal is strong enough (left unclassified for
    the user to map manually).
    """
    from wren_agent.tools.db_explorer import DBConnector

    conn = DBConnector(
        db_type="doris", host=host, port=port, user=user, password=password,
        database=db, connection_params=connection_params,
    )
    try:
        tables = await conn.list_tables()
    except Exception:
        tables = []

    counts = {layer: 0 for layer in _LAYER_PREFIXES}
    for t in tables:
        low = t.lower()
        for layer, prefixes in _LAYER_PREFIXES.items():
            if any(low.startswith(p) for p in prefixes):
                counts[layer] += 1
                break
    total = sum(counts.values())
    if total:
        best = max(counts, key=counts.get)
        if counts[best] / total >= 0.5:
            return best

    low = db.lower()
    for layer, prefixes in _LAYER_PREFIXES.items():
        keyword = prefixes[-1]  # bare keyword, e.g. "dwd"
        if low.startswith(keyword) or f"_{keyword}" in low:
            return layer
    return None


# ── Helpers ─────────────────────────────────────────────────────

def _row_to_response(row: dict) -> dict:
    """Convert PG row dict to API response, stripping password."""
    from wren_agent.tools.pg_store import parse_connection_params
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "description": row.get("description", ""),
        "db_type": row["db_type"],
        "host": row["host"],
        "port": row["port"],
        "database_name": row["database_name"],
        "user_name": row.get("user_name", ""),
        "credential_ref": row.get("credential_ref", ""),
        "tags": list(row.get("tags", []) or []),
        "business_desc": row.get("business_desc", ""),
        "connection_params": parse_connection_params(row.get("connection_params")),
        "table_count": row.get("table_count"),
        "last_explored_at": str(row["last_explored_at"]) if row.get("last_explored_at") else None,
        "status": row["status"],
        "created_at": str(row["created_at"]) if row.get("created_at") else None,
        "updated_at": str(row["updated_at"]) if row.get("updated_at") else None,
    }


# ── Routes ──────────────────────────────────────────────────────


@router.get("")
async def list_source_dbs(
    status: str = Query(default="active"),
    db_type: Optional[str] = Query(default=None),
    tag: Optional[str] = Query(default=None),
    format: str = Query(default="full", description="full|prompt"),
):
    """List all registered source databases.

    Query params:
    - status: active|inactive|all
    - db_type: filter by database type
    - tag: filter by tag
    - format: 'full' returns JSON, 'prompt' returns LLM-ready text
    """
    from wren_agent.tools.pg_store import list_source_dbs as pg_list, build_source_dbs_prompt

    status_filter = status if status in ("active", "inactive") else None
    rows = await pg_list(status=status_filter or "active", db_type=db_type, tag=tag)

    if format == "prompt":
        return {
            "prompt": build_source_dbs_prompt(rows),
            "count": len(rows),
        }

    return {
        "count": len(rows),
        "items": [_row_to_response(r) for r in rows],
    }


@router.get("/{db_id}")
async def get_source_db(db_id: str):
    """Get a single source database by ID or name."""
    from wren_agent.tools.pg_store import get_source_db as pg_get

    row = await pg_get(db_id)
    if not row:
        raise HTTPException(404, f"Source DB '{db_id}' not found")

    return _row_to_response(row)


@router.post("")
async def create_or_update_source_db(request: SourceDBCreateRequest):
    """Create or update a source database registration.

    Connectivity is tested BEFORE saving — if the test fails, the data source
    is NOT registered and a 400 error is returned with the failure reason.

    Upsert on 'name' — if a DB with the same name exists, its config is updated.
    Password is encrypted with SM4 before storage.
    """
    from wren_agent.tools.pg_store import register_source_db

    # ── 1. Require password for connectivity test ──────────────────
    if not request.password:
        raise HTTPException(
            400,
            "Password is required to test database connectivity before registration.",
        )

    # ── 1.5 Warehouse source (doris): validate layer mapping ───────
    if request.db_type == "doris":
        try:
            layer_dbs = _validate_doris_layers(request.connection_params)
        except ValueError as e:
            raise HTTPException(400, str(e))
    else:
        layer_dbs = []

    # ── 2. Test connectivity FIRST ─────────────────────────────────
    try:
        from wren_agent.tools.db_explorer import DBConnector

        def _make_connector(database: str) -> DBConnector:
            return DBConnector(
                db_type=request.db_type,
                host=request.host, port=request.port,
                user=request.user_name, password=request.password,
                database=database,
                connection_params=request.connection_params,
            )

        if layer_dbs:
            # Doris cluster: test every layer database (deduplicated)
            tables = []
            for layer_db in layer_dbs:
                tables.extend(await _make_connector(layer_db).list_tables())
            table_count = len(set(tables))
        else:
            tables = await _make_connector(request.database_name).list_tables()
            table_count = len(tables)
    except Exception as e:
        error_msg = str(e)[:500]
        raise HTTPException(
            400,
            f"Connection test failed: {error_msg}",
        )

    # ── 3. Save only after connectivity is confirmed ───────────────
    db_id = await register_source_db(
        name=request.name,
        description=request.description,
        db_type=request.db_type,
        host=request.host,
        port=request.port,
        database_name=request.database_name,
        user_name=request.user_name,
        password=request.password,  # Encrypted with SM4 before storage
        credential_ref=request.credential_ref,
        tags=request.tags,
        business_desc=request.business_desc,
        connection_params=request.connection_params,
    )

    # ── 4. Mark as active with table count ─────────────────────────
    from wren_agent.tools.pg_store import update_source_db_status
    await update_source_db_status(db_id, "active", table_count)

    return {
        "id": db_id,
        "name": request.name,
        "created": True,
        "connection_test": {
            "connected": True,
            "table_count": table_count,
        },
    }


@router.post("/{db_id}/test")
async def test_source_db(db_id: str, body: dict = None):
    """Test connectivity to a registered source database.

    Requires password in request body: {"password": "xxx"}
    """
    from wren_agent.tools.pg_store import get_source_db as pg_get

    row = await pg_get(db_id)
    if not row:
        raise HTTPException(404, f"Source DB '{db_id}' not found")

    password = (body or {}).get("password", "")
    if not password:
        raise HTTPException(400, "password is required for connectivity test")

    try:
        from wren_agent.tools.db_explorer import DBConnector
        from wren_agent.tools.pg_store import parse_connection_params

        def _make_connector(database: str) -> DBConnector:
            return DBConnector(
                db_type=row["db_type"],
                host=row["host"], port=row["port"],
                user=row["user_name"], password=password,
                database=database,
                connection_params=parse_connection_params(row.get("connection_params")),
            )

        if row["db_type"] == "doris":
            # Doris cluster: test every layer database
            layer_dbs = _validate_doris_layers(
                parse_connection_params(row.get("connection_params"))
            )
            tables = []
            for layer_db in layer_dbs:
                tables.extend(await _make_connector(layer_db).list_tables())
            table_count = len(set(tables))
        else:
            tables = await _make_connector(row["database_name"]).list_tables()
            table_count = len(tables)

        from wren_agent.tools.pg_store import update_source_db_status
        await update_source_db_status(str(row["id"]), "active", table_count)

        return {
            "connected": True,
            "table_count": len(tables),
            "sample_tables": tables[:10],
        }

    except Exception as e:
        from wren_agent.tools.pg_store import update_source_db_status
        await update_source_db_status(str(row["id"]), "error")

        return {"connected": False, "error": str(e)}


@router.post("/refresh-warehouse")
async def refresh_warehouse_sources():
    """Re-probe every registered Doris warehouse source and refresh table counts.

    Uses stored (SM4-encrypted) credentials — no password needed from the user.
    Purpose: after manually creating more dim/dwd/dws tables in the warehouse,
    the dashboard 刷新 button calls this so the new tables surface in the UI
    (and `table_count` / `last_explored_at` in the registry are updated) before
    a task is started. Task-time exploration always probes live regardless.

    Also performs auto-discovery (T-13): `SHOW DATABASES` on each cluster —
    databases created directly on the cluster that are not yet in the layers
    mapping are classified into a layer (dominant table-name prefix, falling
    back to the database-name keyword) and APPENDED to the mapping, which is
    persisted via update_source_db_layers. Ambiguous databases are returned in
    `unclassified` for manual mapping instead of being auto-added.
    """
    from wren_agent.tools.pg_store import (
        get_source_db_credentials,
        list_source_dbs as pg_list,
        update_source_db_layers,
        update_source_db_status,
    )

    rows = await pg_list(status="active", db_type="doris")
    refreshed: list[dict] = []
    failed: list[dict] = []
    discovered_all: list[dict] = []
    unclassified_all: list[str] = []
    removed_all: list[str] = []

    for row in rows:
        db_id = str(row["id"])
        name = row["name"]
        try:
            creds = await get_source_db_credentials(db_id)
            if not creds or not (creds.get("user") and creds.get("password")):
                raise ValueError("credentials missing in registry")
            connection_params = creds.get("connection_params") or {}
            layer_dbs = _validate_doris_layers(connection_params)

            from wren_agent.tools.db_explorer import DBConnector

            # ── Auto-discovery (T-13): databases created directly on the
            # cluster (new business domains) are detected and appended to
            # the layers mapping, so they become explorable without manual
            # re-registration. Mapped databases that no longer exist on the
            # cluster are dropped (stale entries would otherwise fail every
            # refresh, e.g. `Unknown database`). Runs BEFORE probing so new
            # databases are counted in this very refresh.
            discovered: list[dict] = []
            unclassified: list[str] = []
            removed: list[str] = []
            try:
                probe_conn = DBConnector(
                    db_type="doris",
                    host=row["host"], port=row["port"],
                    user=creds["user"], password=creds["password"],
                    database=None,  # SHOW DATABASES needs no database selected;
                    # a stale layer_db entry must not sink discovery
                    connection_params=connection_params,
                )
                all_dbs = await probe_conn.list_databases()
                db_set = set(all_dbs)
                known = set(layer_dbs)
                new_layers = dict(connection_params.get("layers") or {})
                removed: list[str] = []
                for layer, vals in list(new_layers.items()):
                    items = [str(v) for v in (vals if isinstance(vals, list) else [vals]) if str(v)]
                    kept = [v for v in items if v in db_set]
                    gone = [v for v in items if v not in db_set]
                    if gone:
                        removed.extend(gone)
                        new_layers[layer] = kept if len(kept) > 1 else (kept[0] if kept else "")
                for db in all_dbs:
                    if db in known or db.lower() in _SYSTEM_DBS:
                        continue
                    layer = await _classify_warehouse_db(
                        row["host"], row["port"], creds["user"], creds["password"],
                        connection_params, db,
                    )
                    if layer is None:
                        unclassified.append(db)
                        continue
                    vals = new_layers.get(layer)
                    if not isinstance(vals, list):
                        vals = [vals] if vals else []
                    if db not in vals:
                        vals.append(db)
                        new_layers[layer] = vals
                    discovered.append({"db": db, "layer": layer})
                if discovered or removed:
                    await update_source_db_layers(db_id, new_layers)
                    connection_params = {**connection_params, "layers": new_layers}
            except Exception as e:
                logger.warning("Warehouse auto-discovery failed for '%s' (id=%s): %s",
                               name, db_id, e)

            # Re-derive after discovery so new databases are probed too
            layer_dbs = _validate_doris_layers(connection_params)

            tables: list[str] = []
            layer_counts: dict[str, int] = {}
            for layer_db in layer_dbs:
                conn = DBConnector(
                    db_type="doris",
                    host=row["host"], port=row["port"],
                    user=creds["user"], password=creds["password"],
                    database=layer_db,
                    connection_params=connection_params,
                )
                layer_tables = await conn.list_tables()
                tables.extend(layer_tables)
                layer_counts[layer_db] = len(layer_tables)

            total = len(set(tables))

            # Per-layer totals for the UI (layer_counts is keyed by database)
            layers_map = connection_params.get("layers") or {}
            layer_totals: dict[str, int] = {}
            for layer, val in layers_map.items():
                dbs = val if isinstance(val, list) else [val]
                layer_totals[layer] = sum(
                    layer_counts.get(str(d).strip(), 0) for d in dbs if str(d).strip()
                )

            await update_source_db_status(db_id, "active", total)
            refreshed.append({
                "id": db_id,
                "name": name,
                "status": "active",
                "table_count": total,
                "layer_counts": layer_counts,
                "layer_totals": layer_totals,
                "layers": layers_map,
                "discovered": discovered,
                "unclassified": unclassified,
                "removed": removed,
            })
            discovered_all.extend(discovered)
            unclassified_all.extend(unclassified)
            removed_all.extend(removed)
        except Exception as e:
            logger.warning("Warehouse refresh failed for '%s' (id=%s): %s",
                           name, db_id, e)
            failed.append({"id": db_id, "name": name, "error": str(e)[:300]})

    return {
        "refreshed": refreshed,
        "failed": failed,
        "total": len(rows),
        "discovered": discovered_all,
        "unclassified": unclassified_all,
        "removed": removed_all,
    }


@router.post("/{db_id}/delete")
async def delete_source_db(db_id: str, body: DeleteRequest = DeleteRequest()):
    """Delete a source database registration (POST with action=delete)."""
    from wren_agent.tools.pg_store import delete_source_db as pg_delete

    deleted = await pg_delete(db_id)
    if not deleted:
        raise HTTPException(404, f"Source DB '{db_id}' not found")

    return {"deleted": True, "id": db_id}
