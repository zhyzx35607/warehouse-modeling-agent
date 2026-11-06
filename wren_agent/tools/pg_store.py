"""PostgreSQL connection pool and utility for task metadata and registries."""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

from wren_agent.config import settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

# ── Pool management ─────────────────────────────────────────────


async def init_pg_pool(
    dsn: str | None = None,
    min_size: int | None = None,
    max_size: int | None = None,
) -> asyncpg.Pool:
    """Initialize the global PG connection pool.

    Uses settings from config if parameters not provided.
    """
    global _pool
    if _pool is not None:
        return _pool

    # safe_dsn appends sslmode=disable — see PostgresSettings.safe_dsn
    # (OpenSSL applink crash on this Windows venv)
    dsn = dsn or settings.pg.safe_dsn
    min_size = min_size or settings.pg.pool_min_size
    max_size = max_size or settings.pg.pool_max_size

    _pool = await asyncpg.create_pool(dsn=dsn, min_size=min_size, max_size=max_size)
    logger.info(f"PG pool initialized (min={min_size}, max={max_size})")
    return _pool


async def close_pg_pool() -> None:
    """Close the PG connection pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def get_pg_pool() -> asyncpg.Pool:
    """Get the global PG pool (must be initialized first)."""
    if _pool is None:
        raise RuntimeError("PG pool not initialized. Call init_pg_pool() first.")
    return _pool


# ── Modeling stage persistence ─────────────────────────────────


async def upsert_modeling_stage(stage_run: Any) -> dict[str, Any]:
    """Create or update one child stage while preserving its parent task."""
    from wren_agent.models.stage import ModelingStageRun

    run = stage_run if isinstance(stage_run, ModelingStageRun) else ModelingStageRun.model_validate(stage_run)
    data = run.model_dump(mode="json")
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO modeling_stages
                (task_id, stage, thread_id, status, attempt, input_versions,
                 quality_gate, error, started_at, finished_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8, $9, $10, NOW())
            ON CONFLICT (task_id, stage) DO UPDATE SET
                thread_id = EXCLUDED.thread_id, status = EXCLUDED.status,
                attempt = EXCLUDED.attempt, input_versions = EXCLUDED.input_versions,
                quality_gate = EXCLUDED.quality_gate, error = EXCLUDED.error,
                started_at = COALESCE(EXCLUDED.started_at, modeling_stages.started_at),
                finished_at = EXCLUDED.finished_at, updated_at = NOW()
            RETURNING *
            """,
            data["task_id"], data["stage"], data["thread_id"], data["status"],
            data["attempt"], json.dumps(data["input_versions"]),
            json.dumps(data["quality_gate"]), data["error"],
            run.started_at, run.finished_at,
        )
    return dict(row)


async def create_stage_artifact(
    task_id: str,
    stage: str,
    artifact_type: str,
    payload: dict[str, Any],
    schema_fingerprint: str = "",
) -> dict[str, Any]:
    """Append an immutable artifact version under a transaction-local lock."""
    from wren_agent.models.stage import ModelingStage, StageArtifactType

    stage_value = ModelingStage(stage).value
    type_value = StageArtifactType(artifact_type).value
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"{task_id}:{stage_value}:{type_value}",
            )
            latest = await conn.fetchval(
                "SELECT MAX(version) FROM stage_artifacts "
                "WHERE task_id=$1 AND stage=$2 AND artifact_type=$3",
                task_id, stage_value, type_value,
            )
            version = int(latest or 0) + 1
            row = await conn.fetchrow(
                """
                INSERT INTO stage_artifacts
                    (task_id, stage, artifact_type, version, payload,
                     schema_fingerprint, supersedes_version)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
                RETURNING *
                """,
                task_id, stage_value, type_value, version,
                json.dumps(payload, default=str), schema_fingerprint,
                version - 1 if version > 1 else None,
            )
    return dict(row)


async def load_latest_stage_artifact(
    task_id: str, stage: str, artifact_type: str,
) -> dict[str, Any] | None:
    """Load the latest immutable artifact for a stage contract."""
    from wren_agent.models.stage import ModelingStage, StageArtifactType

    pool = get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM stage_artifacts WHERE task_id=$1 AND stage=$2 "
            "AND artifact_type=$3 ORDER BY version DESC LIMIT 1",
            task_id, ModelingStage(stage).value, StageArtifactType(artifact_type).value,
        )
    return dict(row) if row else None


# ── DimensionRegistry ───────────────────────────────────────────


async def register_dimension(
    dim_name: str,
    dim_table: str,
    owner_task_id: str,
    natural_keys: list[str],
    grain: str,
    fields: list[dict],
    variant_of: str | None = None,
    db_name: str = "",
) -> dict[str, Any]:
    """Register a dimension in the DimensionRegistry.

    Checks for conflicts before inserting. `db_name` scopes the physical
    dim_table — two sources with the same table name in different databases
    no longer collide on dedupe.
    """
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        # Check for existing (same table AND same database)
        existing = await conn.fetchrow(
            "SELECT dim_name, version, owner_task_id FROM dimension_registry "
            "WHERE dim_table = $1 AND db_name = $2 AND active = TRUE",
            dim_table, db_name or "",
        )
        if existing:
            return {
                "status": "conflict",
                "existing_dim": dict(existing),
                "message": f"Dimension table {dim_table} already exists in registry",
            }

        # Insert new
        row = await conn.fetchrow(
            """
            INSERT INTO dimension_registry (dim_name, dim_table, owner_task_id, natural_keys, grain, fields, variant_of, db_name, version, active)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 1, TRUE)
            RETURNING dim_id, version
            """,
            dim_name, dim_table, owner_task_id, natural_keys, grain,
            json_serialize(fields), variant_of, db_name or "",
        )
        return {"status": "created", "dim_id": row["dim_id"], "version": row["version"]}


async def query_dimension_registry(
    dim_name: str | None = None,
    db_names: list[str] | None = None,
    goal: str | None = None,
) -> list[dict]:
    """Query dimensions in the registry, optionally scoped to databases.

    `db_names` (a list — pass [db] for a single database) filters by the
    dimension's database, case-insensitively. Bare-name legacy rows
    (db_name = '') are always included so scoped lookups never empty out
    older unscoped data. `goal` further narrows to entries semantically
    related to the modeling goal (embedding-based, best-effort — rows
    without an embedding are not dropped).
    """
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        if dim_name:
            sql = "SELECT * FROM dimension_registry WHERE dim_name = $1 AND active = TRUE"
            params: list[Any] = [dim_name]
        else:
            sql = "SELECT * FROM dimension_registry WHERE active = TRUE"
            params = []
        if db_names:
            dbs = [str(d).strip().lower() for d in db_names if str(d).strip()]
            if dbs:
                params.append(dbs)
                sql += f" AND (lower(db_name) = ANY(${len(params)}::text[]) OR db_name = '')"
        sql += " ORDER BY dim_name, version"
        rows = await conn.fetch(sql, *params)

    items = [dict(r) for r in rows]
    if goal:
        related = await filter_registry_by_goal(
            db_names, goal, kind="dimension")
        if related is not None:
            items = [r for r in items if r.get("dim_name") in related]
    return items


async def get_dimension_variants(dim_name: str) -> list[dict]:
    """Get all variants of a dimension."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM dimension_registry WHERE (dim_name = $1 OR variant_of = $1) AND active = TRUE ORDER BY version",
            dim_name,
        )
        return [dict(r) for r in rows]


async def paginate_dimension_registry(
    search: str = "",
    dim_table_like: str = "",
    db_name: str = "",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """Paginated dimension listing with filters (dimension-library page).

    Returns {"total": int, "items": [dict, ...]}.
    """
    pool = get_pg_pool()
    where = ["active = TRUE"]
    params: list[Any] = []
    if search:
        params.append(f"%{search}%")
        where.append(f"dim_name ILIKE ${len(params)}")
    if dim_table_like:
        params.append(f"%{dim_table_like}%")
        where.append(f"dim_table ILIKE ${len(params)}")
    if db_name:
        params.append(db_name)
        where.append(f"db_name = ${len(params)}")
    params.extend([page_size, (page - 1) * page_size])
    sql = (
        "SELECT *, COUNT(*) OVER() AS full_count FROM dimension_registry "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY dim_name, version LIMIT ${len(params) - 1} OFFSET ${len(params)}"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    items = [dict(r) for r in rows]
    total = int(items[0]["full_count"]) if items else 0
    for item in items:
        item.pop("full_count", None)
    return {"total": total, "items": items}


async def update_dimension(dim_id: str, **fields) -> bool:
    """Partial update of a dimension row (non-None fields only).

    Returns True when a row was updated (active rows only).
    """
    allowed = {
        "dim_name", "dim_table", "natural_keys", "grain",
        "fields", "variant_of", "version", "db_name",
    }
    sets: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key not in allowed or value is None:
            continue
        # fields is a JSONB column — pass it as a JSON string
        if key == "fields":
            value = json_serialize(value)
        params.append(value)
        sets.append(f"{key} = ${len(params)}")
    if not sets:
        return False
    params.append(dim_id)
    sets.append("updated_at = NOW()")
    sql = (
        "UPDATE dimension_registry SET " + ", ".join(sets) +
        f" WHERE dim_id = ${len(params)} AND active = TRUE"
    )
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(sql, *params)
    return result and "UPDATE 1" in str(result)


async def soft_delete_dimension(dim_id: str) -> bool:
    """Soft-delete a dimension (active=FALSE). True when a row was updated."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE dimension_registry SET active = FALSE, updated_at = NOW() "
            "WHERE dim_id = $1 AND active = TRUE",
            dim_id,
        )
    return result and "UPDATE 1" in str(result)


# ── MetricRegistry ──────────────────────────────────────────────


async def register_metric(
    metric_name: str,
    metric_type: str,  # atomic | derived
    definition_sql: str,
    source_table: str,
    owner_task_id: str,
    grain: str = "",
    parent_metrics: list[str] | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Register a metric in the MetricRegistry.

    Detects naming conflicts and returns comparison for human resolution.
    """
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        # Fuzzy match for similar metrics
        similar = await conn.fetch(
            """
            SELECT metric_name, definition_sql, source_table, grain
            FROM metric_registry
            WHERE active = TRUE
              AND similarity(metric_name, $1) > 0.4
            ORDER BY similarity(metric_name, $1) DESC
            LIMIT 5
            """,
            metric_name,
        )

        conflicts = []
        for row in similar:
            row_dict = dict(row)
            # Check if definitions differ
            if (row_dict["definition_sql"] != definition_sql or
                row_dict["source_table"] != source_table):
                conflicts.append({
                    "existing_name": row_dict["metric_name"],
                    "existing_definition": row_dict["definition_sql"],
                    "existing_table": row_dict["source_table"],
                    "conflict_type": "name_conflict" if row_dict["metric_name"] == metric_name else "similar_name",
                })

        if conflicts:
            return {
                "status": "conflict",
                "conflicts": conflicts,
                "message": f"Found {len(conflicts)} potential metric conflicts",
            }

        # Insert
        row = await conn.fetchrow(
            """
            INSERT INTO metric_registry (metric_name, metric_type, definition_sql, source_table, owner_task_id, grain, parent_metrics, tags)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING metric_id
            """,
            metric_name, metric_type, definition_sql, source_table, owner_task_id, grain, parent_metrics or [], tags or [],
        )
        return {"status": "created", "metric_id": row["metric_id"]}


# ── Goal-semantic relevance filtering (migration 017) ───────────
# The design layer embeds the modeling goal once and retrieves registry
# entries whose embedding is cosine-similar (metric_name+grain+definition /
# dim_name+dim_table+grain). Threshold is a LOW 0.55 — high recall; the
# design prompt prefers a slightly noisy but never-empty related set over a
# precise-but-empty one. Result cache mirrors _sem_fallback_cache: every
# design layer re-runs the same goal query, so cache hits skip re-embedding.

_GOAL_SIM_THRESHOLD = 0.55
# Dimension names are English (dim_house_contract) while the modeling goal is
# Chinese — cross-language cosine similarity runs ~0.45-0.52 vs the 0.55-0.73
# band of Chinese metric names (measured on the ecommerce corpus). A
# lower dimension threshold keeps the clearly related dims while still
# dropping the financial_* bulk.
_GOAL_SIM_THRESHOLD_DIM = 0.45
_registry_goal_cache: dict[tuple, tuple[float, list[str] | None]] = {}
_REGISTRY_GOAL_CACHE_TTL = 1800  # seconds


async def filter_registry_by_goal(
    db_names: list[str] | None,
    goal: str,
    *,
    kind: str,  # "metric" | "dimension"
) -> list[str] | None:
    """Names of registry entries semantically related to the modeling goal.

    `db_names` is informational for the cache key (empty = whole registry —
    business mode has no db scope). Returns None when filtering is
    unavailable (embedding/DB failure) — the caller treats None as "no
    filter" (keep everything). Only rows WITH an embedding participate; rows
    still awaiting backfill are not dropped by design — they just don't
    contribute until embedded.
    """
    if not goal:
        return None
    import time as _time

    key = (kind, goal.strip(), tuple(sorted(db_names or [])))
    now = _time.monotonic()
    cached = _registry_goal_cache.get(key)
    if cached is not None and now - cached[0] < _REGISTRY_GOAL_CACHE_TTL:
        return cached[1]

    from wren_agent.tools.embedding import embed_texts

    vec = (await embed_texts([goal]))[0]
    if vec is None:
        return None

    table = "metric_registry" if kind == "metric" else "dimension_registry"
    name_col = "metric_name" if kind == "metric" else "dim_name"
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {name_col} AS name, 1 - (embedding <=> $1::vector) AS sim "
            f"FROM {table} WHERE embedding IS NOT NULL AND {name_col} <> '' "
            f"ORDER BY embedding <=> $1::vector",
            str(vec),
        )
    related = [r["name"] for r in rows
               if float(r["sim"]) >= (_GOAL_SIM_THRESHOLD_DIM if kind == "dimension" else _GOAL_SIM_THRESHOLD)]
    _registry_goal_cache[key] = (_time.monotonic(), related)
    return related


async def backfill_registry_embeddings(limit: int | None = None) -> int:
    """Embed registry rows with embedding IS NULL (idempotent, migration 017).

    Batch-embeds both metric_registry and dimension_registry rows that lack a
    vector. Returns the number of rows embedded. Embedding failures are
    skipped (never fatal) — re-running the script retries the leftovers.
    """
    from wren_agent.tools.embedding import embed_texts, registry_embedding_text

    pool = get_pg_pool()
    total = 0
    targets = (
        (
            "metric_registry", "metric_name",
            lambda r: registry_embedding_text(
                "metric", metric_name=r["metric_name"], grain=r.get("grain") or "",
                definition_sql=r.get("definition_sql") or "",
                tags=",".join(r.get("tags") or [])),
        ),
        (
            "dimension_registry", "dim_name",
            lambda r: registry_embedding_text(
                "dimension", dim_name=r["dim_name"], dim_table=r.get("dim_table") or "",
                grain=r.get("grain") or ""),
        ),
    )
    for table, name_col, build in targets:
        sql = f"SELECT * FROM {table} WHERE embedding IS NULL AND {name_col} <> ''"
        params: list[Any] = []
        if limit:
            params.append(limit)
            sql += f" LIMIT ${len(params)}"
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        if not rows:
            continue
        vecs = await embed_texts([build(dict(r)) for r in rows])
        for r, v in zip(rows, vecs):
            if v is None:
                continue
            async with pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE {table} SET embedding = $1::vector "
                    f"WHERE {name_col} = $2 AND embedding IS NULL",
                    str(v), r[name_col],
                )
            total += 1
    return total


async def query_metric_registry(
    metric_name: str | None = None,
    db_names: list[str] | None = None,
    goal: str | None = None,
) -> list[dict]:
    """Query metrics in the registry, optionally scoped to source databases.

    `db_names` filters by the first segment of `source_table` (the database
    prefix, e.g. dws_ecommerce_orders_sh), case-insensitively. Pass None (or
    empty) for everything. `goal` further narrows to entries semantically
    related to the modeling goal (embedding-based, best-effort).
    """
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        if metric_name:
            sql = "SELECT * FROM metric_registry WHERE metric_name ILIKE $1 AND active = TRUE"
            params: list[Any] = [f"%{metric_name}%"]
        else:
            sql = "SELECT * FROM metric_registry WHERE active = TRUE"
            params = []
        if db_names:
            dbs = [str(d).strip().lower() for d in db_names if str(d).strip()]
            if dbs:
                params.append(dbs)
                sql += f" AND lower(split_part(source_table, '.', 1)) = ANY(${len(params)}::text[])"
        sql += " ORDER BY metric_name"
        rows = await conn.fetch(sql, *params)

    items = [dict(r) for r in rows]
    if goal:
        related = await filter_registry_by_goal(db_names, goal, kind="metric")
        if related is not None:
            items = [r for r in items if r.get("metric_name") in related]
    return items


async def paginate_metric_registry(
    search: str = "",
    source_table_like: str = "",
    metric_type: str = "",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """Paginated metric listing with filters (metric-library page).

    Returns {"total": int, "items": [dict, ...]}.
    """
    pool = get_pg_pool()
    where = ["active = TRUE"]
    params: list[Any] = []
    if search:
        params.append(f"%{search}%")
        where.append(f"metric_name ILIKE ${len(params)}")
    if source_table_like:
        params.append(f"%{source_table_like}%")
        where.append(f"source_table ILIKE ${len(params)}")
    if metric_type:
        params.append(metric_type)
        where.append(f"metric_type = ${len(params)}")
    params.extend([page_size, (page - 1) * page_size])
    sql = (
        "SELECT *, COUNT(*) OVER() AS full_count FROM metric_registry "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY metric_name LIMIT ${len(params) - 1} OFFSET ${len(params)}"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    items = [dict(r) for r in rows]
    total = int(items[0]["full_count"]) if items else 0
    for item in items:
        item.pop("full_count", None)
    return {"total": total, "items": items}


async def update_metric(metric_id: str, **fields) -> bool:
    """Partial update of a metric row (non-None fields only).

    Returns True when a row was updated (active rows only).
    """
    allowed = {
        "metric_name", "metric_type", "definition_sql", "source_table",
        "grain", "parent_metrics", "tags",
    }
    sets: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key not in allowed or value is None:
            continue
        params.append(value)
        sets.append(f"{key} = ${len(params)}")
    if not sets:
        return False
    params.append(metric_id)
    sets.append("updated_at = NOW()")
    sql = (
        "UPDATE metric_registry SET " + ", ".join(sets) +
        f" WHERE metric_id = ${len(params)} AND active = TRUE"
    )
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(sql, *params)
    return result and "UPDATE 1" in str(result)


async def soft_delete_metric(metric_id: str) -> bool:
    """Soft-delete a metric (active=FALSE). True when a row was updated."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE metric_registry SET active = FALSE, updated_at = NOW() "
            "WHERE metric_id = $1 AND active = TRUE",
            metric_id,
        )
    return result and "UPDATE 1" in str(result)


# ── Blueprint Library ───────────────────────────────────────────


async def store_blueprint(
    name: str,
    description: str,
    domain: str,
    layers: list[dict],
    source_tables_signature: dict,
    source_task_id: str | None = None,
    tags: list[str] | None = None,
) -> str:
    """Store a blueprint in the library."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO blueprint_library (name, description, domain, layers, source_tables_signature, source_task_id, version, tags)
            VALUES ($1, $2, $3, $4, $5, $6, 1, $7)
            RETURNING blueprint_id
            """,
            name, description, domain, json_serialize(layers),
            json_serialize(source_tables_signature), source_task_id, tags or [],
        )
        return row["blueprint_id"]


async def search_blueprints(
    domain: str | None = None,
    table_names: list[str] | None = None,
    tags: list[str] | None = None,
    limit: int = 5,
) -> list[dict]:
    """Search blueprint library by domain, tables, or tags."""
    pool = get_pg_pool()
    conditions = ["active = TRUE"]
    params: list[Any] = []
    idx = 1

    if domain:
        conditions.append(f"domain = ${idx}")
        params.append(domain)
        idx += 1

    if tags:
        conditions.append(f"tags && ${idx}")
        params.append(tags)
        idx += 1

    if table_names:
        # Pre-filter: blueprint signature must overlap at least one source
        # table (case-insensitive). Guards against non-array signatures.
        conditions.append(
            f"(jsonb_typeof(source_tables_signature->'tables') = 'array' "
            f"AND EXISTS (SELECT 1 FROM jsonb_array_elements_text("
            f"source_tables_signature->'tables') AS t(name) "
            f"WHERE lower(t.name) = ANY(${idx})))"
        )
        params.append([t.lower() for t in table_names])
        idx += 1

    where = " AND ".join(conditions)
    query = f"SELECT * FROM blueprint_library WHERE {where} ORDER BY usage_count DESC, updated_at DESC LIMIT {limit}"

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)
        return [dict(r) for r in rows]


async def increment_blueprint_usage(blueprint_id: str) -> None:
    """Increment usage count for a blueprint."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE blueprint_library SET usage_count = usage_count + 1, updated_at = NOW() WHERE blueprint_id = $1",
            blueprint_id,
        )


# ── Schema Snapshot ─────────────────────────────────────────────


async def record_dag_table_map(
    table_name: str,
    project_code: int,
    workflow_code: int,
    task_id: str,
) -> None:
    """Record table -> DolphinScheduler workflow mapping (migration 009).

    Written by dag_deploy for every deployed table; warehouse mode resolves
    upstream DWD/DWM tables to their workflows via this map (wayfinder T-11).
    """
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO dag_table_map (table_name, project_code, workflow_code, task_id) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (table_name) DO UPDATE SET "
            "project_code = $2, workflow_code = $3, task_id = $4, created_at = NOW()",
            table_name, project_code, workflow_code, task_id,
        )


async def lookup_dag_table_map(table_name: str) -> dict | None:
    """Resolve a table to its deployed workflow, or None."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT table_name, project_code, workflow_code, task_id "
            "FROM dag_table_map WHERE table_name = $1",
            table_name,
        )
        return dict(row) if row else None


async def pause_task(task_id: str, pause_type: str) -> None:
    """Register a paused task (migration 010). Idempotent — re-entering the
    pause node (recovery) resets the row to paused with an empty payload."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO task_pauses (task_id, pause_type, status, payload) "
            "VALUES ($1, $2, 'paused', '{}') "
            "ON CONFLICT (task_id) DO UPDATE SET "
            "pause_type = $2, status = 'paused', payload = '{}', "
            "resumed_at = NULL, created_at = NOW()",
            task_id, pause_type,
        )


async def resume_task(task_id: str, payload: dict) -> None:
    """Record a resume decision — the paused node polls this row."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE task_pauses SET status = 'resumed', payload = $2, "
            "resumed_at = NOW() WHERE task_id = $1",
            # asyncpg's default JSONB codec accepts a JSON string, not dict.
            task_id, json_serialize(payload),
        )


async def get_task_pause(task_id: str) -> dict | None:
    """Read the pause row (None if the task never paused / was cleared)."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT task_id, pause_type, status, payload FROM task_pauses "
            "WHERE task_id = $1",
            task_id,
        )
        if not row:
            return None
        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                payload = {}
        return {
            "task_id": row["task_id"],
            "pause_type": row["pause_type"],
            "status": row["status"],
            "payload": payload if isinstance(payload, dict) else {},
        }


async def clear_task_pause(task_id: str) -> None:
    """Remove the pause row once the paused node has consumed the decision."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM task_pauses WHERE task_id = $1", task_id)


async def store_schema_snapshot(
    task_id: str,
    table_name: str,
    source_db_id: str,
    fields: dict[str, str],
    primary_keys: list[str],
) -> None:
    """Store a schema snapshot for drift detection."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        # Compare against the latest completed baseline, never against a retry
        # snapshot written by the same task.
        prev = await conn.fetchrow(
            "SELECT snapshot_id, fields FROM schema_snapshots "
            "WHERE table_name = $1 AND source_db_id = $2 AND task_id <> $3 "
            "ORDER BY snapshot_at DESC LIMIT 1",
            table_name, source_db_id, task_id,
        )

        # asyncpg returns JSONB as text unless a custom codec is registered.
        # Normalize it before drift comparison, which requires dictionary keys.
        prev_fields = _parse_json_object(prev["fields"]) if prev else {}

        drift = _detect_drift(prev_fields, fields) if prev_fields else []

        # A recovered node may write the same table again. Update that task's
        # snapshot in place; insert only when this is its first write.
        await conn.execute(
            """
            WITH updated AS (
                UPDATE schema_snapshots
                   SET fields = $4, primary_keys = $5, has_drift = $6,
                       drift_details = $7, snapshot_at = NOW()
                 WHERE task_id = $1 AND table_name = $2 AND source_db_id = $3
                 RETURNING snapshot_id
            )
            INSERT INTO schema_snapshots
                (task_id, table_name, source_db_id, fields, primary_keys,
                 has_drift, drift_details)
            SELECT $1, $2, $3, $4, $5, $6, $7
             WHERE NOT EXISTS (SELECT 1 FROM updated)
            """,
            task_id, table_name, source_db_id,
            json_serialize(fields), primary_keys,
            bool(drift), json_serialize(drift),
        )


async def load_latest_schema_snapshots(
    task_id: str,
    source_db_id: str,
) -> dict[str, "SchemaSnapshot"]:
    """Load the latest per-table baseline from a previous modeling task."""
    from wren_agent.models.schema import SchemaSnapshot

    pool = get_pg_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT ON (table_name) table_name, fields, primary_keys, "
            "snapshot_at FROM schema_snapshots "
            "WHERE source_db_id = $1 AND task_id <> $2 "
            "ORDER BY table_name, snapshot_at DESC",
            source_db_id, task_id,
        )

    snapshots: dict[str, SchemaSnapshot] = {}
    for row in rows:
        table_name = row["table_name"]
        snapshots[table_name] = SchemaSnapshot(
            table_name=table_name,
            source_db_id=source_db_id,
            snapshot_at=row["snapshot_at"],
            fields=_parse_json_object(row["fields"]),
            primary_keys=list(row["primary_keys"] or []),
        )
    return snapshots


def _detect_drift(
    prev_fields: dict[str, str],
    curr_fields: dict[str, str],
) -> list[dict]:
    """Detect schema drift between two field snapshots."""
    drift = []
    prev_set = set(prev_fields.keys())
    curr_set = set(curr_fields.keys())

    # Removed columns
    for col in prev_set - curr_set:
        drift.append({"type": "column_removed", "column": col, "prev_type": prev_fields[col]})

    # Added columns
    for col in curr_set - prev_set:
        drift.append({"type": "column_added", "column": col, "curr_type": curr_fields[col]})

    # Type changes
    for col in prev_set & curr_set:
        if prev_fields[col] != curr_fields[col]:
            drift.append({
                "type": "column_type_changed",
                "column": col,
                "prev_type": prev_fields[col],
                "curr_type": curr_fields[col],
            })

    return drift


# ── Source DB Registry ──────────────────────────────────────────


async def register_source_db(
    name: str,
    description: str,
    db_type: str,
    host: str,
    port: int,
    database_name: str,
    user_name: str = "",
    password: str = "",
    credential_ref: str = "",
    tags: list[str] | None = None,
    business_desc: str = "",
    connection_params: dict | None = None,
) -> str:
    """Register a source database connection.

    Password is encrypted with SM4 before storage.
    Returns the UUID.
    """
    from wren_agent.tools.sm4_crypto import sm4_encrypt

    encrypted_pw = sm4_encrypt(password) if password else ""

    pool = get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO source_db_registry
               (name, description, db_type, host, port, database_name,
                user_name, credential_ref, encrypted_password, tags, business_desc,
                connection_params)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
               ON CONFLICT (name) DO UPDATE SET
                 description=EXCLUDED.description, db_type=EXCLUDED.db_type,
                 host=EXCLUDED.host, port=EXCLUDED.port,
                 database_name=EXCLUDED.database_name,
                 user_name=EXCLUDED.user_name,
                 credential_ref=EXCLUDED.credential_ref,
                 encrypted_password=EXCLUDED.encrypted_password,
                 tags=EXCLUDED.tags,
                 business_desc=EXCLUDED.business_desc,
                 connection_params=EXCLUDED.connection_params,
                 updated_at=NOW()
               RETURNING id""",
            name, description, db_type, host, port, database_name,
            user_name, credential_ref, encrypted_pw, tags or [], business_desc,
            json.dumps(connection_params or {}),
        )
        return str(row["id"])


def parse_connection_params(value: Any) -> dict:
    """Normalize a JSONB connection_params value to a dict.

    asyncpg returns jsonb columns as str (no codec registered).
    """
    return _parse_json_object(value)


def _parse_json_object(value: Any) -> dict:
    """Normalize a JSON/JSONB object returned by asyncpg to a dictionary."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


async def get_source_db_credentials(db_id: str) -> dict | None:
    """Get source DB connection info with decrypted password."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, db_type, host, port, database_name, user_name, "
            "encrypted_password, connection_params FROM source_db_registry "
            "WHERE (id::text = $1 OR name = $1) AND status = 'active'",
            db_id,
        )
        if not row:
            return None

        from wren_agent.tools.sm4_crypto import sm4_decrypt
        password = sm4_decrypt(row["encrypted_password"]) if row["encrypted_password"] else ""

        return {
            "id": str(row["id"]),
            "name": row["name"],
            "db_type": row["db_type"],
            "host": row["host"],
            "port": row["port"],
            "database": row["database_name"],
            "user": row["user_name"],
            "password": password,
            "connection_params": parse_connection_params(row["connection_params"]),
        }


async def list_source_dbs(
    status: str = "active",
    db_type: str | None = None,
    tag: str | None = None,
) -> list[dict]:
    """List registered source databases, optionally filtered."""
    pool = get_pg_pool()
    conditions = ["status = $1"]
    params: list[Any] = [status]
    idx = 2

    if db_type:
        conditions.append(f"db_type = ${idx}")
        params.append(db_type)
        idx += 1

    if tag:
        conditions.append(f"${idx} = ANY(tags)")
        params.append(tag)
        idx += 1

    where = " AND ".join(conditions)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM source_db_registry WHERE {where} ORDER BY name",
            *params,
        )
        return [dict(r) for r in rows]


async def get_source_db(db_id: str) -> dict | None:
    """Get a source DB by ID or name."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM source_db_registry WHERE id::text = $1 OR name = $1",
            db_id,
        )
        return dict(row) if row else None


async def update_source_db_status(
    db_id: str,
    status: str,
    table_count: int | None = None,
) -> None:
    """Update the status and optionally table_count."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        if table_count is not None:
            await conn.execute(
                "UPDATE source_db_registry SET status=$1, table_count=$2, last_explored_at=NOW(), updated_at=NOW() WHERE id=$3",
                status, table_count, db_id,
            )
        else:
            await conn.execute(
                "UPDATE source_db_registry SET status=$1, updated_at=NOW() WHERE id=$2",
                status, db_id,
            )


async def update_source_db_database_name(db_id: str, database_name: str) -> None:
    """Point the warehouse registration's default catalog at a live database.

    `database_name` is used for connectivity tests and any code path that
    still opens Doris with the registry default. A stale value (for example
    `dwd_ecommerce_orders` after that catalog was dropped) makes deploy fail
    with pymysql 1049 before CREATE DATABASE can run.
    """
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE source_db_registry SET database_name=$1, updated_at=NOW() "
            "WHERE id::text=$2 OR name=$2",
            database_name, db_id,
        )


async def update_source_db_layers(db_id: str, layers: dict) -> None:
    """Update only the `layers` key inside connection_params (jsonb merge).

    Used by warehouse auto-discovery: newly detected databases are appended
    to the layer mapping and persisted without touching credentials.
    """
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        # asyncpg's jsonb codec accepts only str/bytes, not dict — serialize.
        await conn.execute(
            "UPDATE source_db_registry "
            "SET connection_params = connection_params || $2::jsonb, updated_at = NOW() "
            "WHERE id = $1",
            db_id,
            json.dumps({"layers": layers}, ensure_ascii=False),
        )


async def merge_source_db_layers(db_id: str, additions: dict[str, str]) -> dict:
    """Append layer databases atomically; never remove or replace existing values."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT db_type, connection_params FROM source_db_registry "
                "WHERE id::text=$1 AND status='active' FOR UPDATE", db_id,
            )
            if not row or str(row["db_type"]).lower() != "doris":
                raise ValueError("warehouse source must be an active Doris registration")
            params = parse_connection_params(row["connection_params"])
            layers = dict(params.get("layers") or {})
            for layer, database in additions.items():
                if layer not in {"ods", "dim", "dwd"} or not database:
                    continue
                current = layers.get(layer)
                values = list(current) if isinstance(current, list) else ([current] if current else [])
                if database not in values:
                    values.append(database)
                layers[layer] = values[0] if len(values) == 1 else values
            await conn.execute(
                "UPDATE source_db_registry SET connection_params=connection_params || $2::jsonb, "
                "last_explored_at=NOW(), updated_at=NOW() WHERE id::text=$1",
                db_id, json.dumps({"layers": layers}, ensure_ascii=False),
            )
    return layers


async def query_field_semantics(table_names: list[str] | None = None) -> list[dict]:
    """Query the field semantic dictionary (migration 011).

    Rows: table_name, field_name, db_name, chinese_meaning, field_role,
    enum_values. `table_names=None` returns everything; otherwise only the
    given tables.
    """
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        if table_names:
            rows = await conn.fetch(
                "SELECT table_name, field_name, db_name, chinese_meaning, field_role, enum_values "
                "FROM field_semantics WHERE table_name = ANY($1::text[])",
                list(table_names),
            )
        else:
            rows = await conn.fetch(
                "SELECT table_name, field_name, db_name, chinese_meaning, field_role, enum_values "
                "FROM field_semantics"
            )
        return [dict(r) for r in rows]


# Process-local result cache for the vector fallback: (table, field, threshold)
# -> hit dict or None. Every design layer re-queries the same uncovered fields
# (dim/dwd/dws/ads each inject semantics), and the hit rate for truly unknown
# fields is low — without a result cache each round re-embeds + re-scans the
# corpus (hundreds of DashScope /embeddings calls per task; see 405cf434).
# Hits AND misses are cached; the TTL bounds staleness when the dictionary
# gains rows mid-process.
_sem_fallback_cache: dict[tuple[str, str, float], tuple[float, dict | None]] = {}
_SEM_FALLBACK_CACHE_TTL = 1800  # seconds


async def semantic_field_semantics_fallback(
    pairs: list[tuple[str, str]],
    threshold: float = 0.70,
) -> dict[str, dict[str, dict]]:
    """Vector fallback for the field-semantics dictionary (migration 013).

    For (table, field) pairs with no exact dictionary row, embed the bare
    FIELD NAME (measured on the ecommerce corpus: true matches score
    0.71-0.79, false positives ≤ 0.61 — including the table name in the query
    text adds noise and blurs the separation) and return the nearest embedded
    meaning — catches synonym/naming-variant reuse (amt vs amount vs 金额)
    that exact matching and prefix rules miss.

    Returns {table: {field: {chinese_meaning, field_role, source='vector_similar',
    sem_from, similarity}}}; pairs below `threshold` are simply absent.
    Never raises — callers treat it as best-effort.
    """
    from wren_agent.tools.embedding import embed_texts

    if not pairs:
        return {}

    import time as _time

    now = _time.monotonic()
    out: dict[str, dict[str, dict]] = {}
    todo: list[tuple[str, str]] = []
    for (table, field) in pairs:
        key = (table, field, threshold)
        cached = _sem_fallback_cache.get(key)
        if cached is not None and now - cached[0] < _SEM_FALLBACK_CACHE_TTL:
            if cached[1]:
                out.setdefault(table, {})[field] = cached[1]
            continue
        todo.append((table, field))

    if todo:
        texts = [f for _, f in todo]
        vectors = await embed_texts(texts)

        pool = get_pg_pool()
        async with pool.acquire() as conn:
            for (table, field), vec in zip(todo, vectors):
                if vec is None:
                    continue
                row = await conn.fetchrow(
                    "SELECT table_name, field_name, chinese_meaning, field_role, "
                    "1 - (embedding <=> $1::vector) AS sim "
                    "FROM field_semantics "
                    "WHERE embedding IS NOT NULL AND chinese_meaning <> '' "
                    "ORDER BY embedding <=> $1::vector LIMIT 1",
                    str(vec),
                )
                hit = None
                if row and float(row["sim"]) >= threshold:
                    hit = {
                        "chinese_meaning": row["chinese_meaning"],
                        "field_role": row["field_role"] or "",
                        "source": "vector_similar",
                        "sem_from": f"{row['table_name']}.{row['field_name']}",
                        "similarity": round(float(row["sim"]), 3),
                    }
                    out.setdefault(table, {})[field] = hit
                _sem_fallback_cache[(table, field, threshold)] = (_time.monotonic(), hit)

    return out


async def upsert_field_semantics(rows: list[dict]) -> int:
    """Upsert field semantics rows; returns the number of rows written.

    Each row: table_name, field_name, chinese_meaning, field_role,
    enum_values (list), source.
    """
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for r in rows:
                await conn.execute(
                    "INSERT INTO field_semantics "
                    "(table_name, field_name, db_name, chinese_meaning, field_role, enum_values, source) "
                    "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7) "
                    "ON CONFLICT (table_name, field_name) DO UPDATE SET "
                    "chinese_meaning = EXCLUDED.chinese_meaning, "
                    "field_role = EXCLUDED.field_role, "
                    "enum_values = EXCLUDED.enum_values, "
                    "source = EXCLUDED.source, "
                    # Content changed → stale vector; NULL forces re-embed
                    "embedding = NULL, "
                    "updated_at = NOW()",
                    r["table_name"], r["field_name"], r.get("db_name", "") or "",
                    r.get("chinese_meaning", "") or "",
                    r.get("field_role", "") or "",
                    json.dumps(r.get("enum_values", []), ensure_ascii=False),
                    r.get("source", "manual") or "manual",
                )
    return len(rows)


async def upsert_field_semantics_from_comments(rows: list[dict]) -> int:
    """Upsert semantics harvested from Doris column comments.

    Authoritative rows (source != 'doris_comment', e.g. the xlsx workbook)
    are NEVER overwritten — comments only fill gaps and refresh previously
    comment-harvested rows when the comment changes.
    Each row: table_name, field_name, db_name (optional), chinese_meaning.
    """
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            for r in rows:
                await conn.execute(
                    "INSERT INTO field_semantics "
                    "(table_name, field_name, db_name, chinese_meaning, field_role, enum_values, source) "
                    "VALUES ($1, $2, $3, $4, '', '[]', 'doris_comment') "
                    "ON CONFLICT (table_name, field_name) DO UPDATE SET "
                    "chinese_meaning = EXCLUDED.chinese_meaning, "
                    # Only invalidate the vector when the meaning actually
                    # changed — re-explore of the same comments used to NULL
                    # every embedding and then block schema_explore on a full
                    # corpus re-embed.
                    "embedding = CASE "
                    "  WHEN field_semantics.chinese_meaning IS DISTINCT FROM "
                    "       EXCLUDED.chinese_meaning THEN NULL "
                    "  ELSE field_semantics.embedding END, "
                    "updated_at = NOW() "
                    "WHERE field_semantics.source = 'doris_comment'",
                    r["table_name"], r["field_name"], r.get("db_name", "") or "",
                    r["chinese_meaning"],
                )
    return len(rows)


async def delete_source_db(db_id: str) -> bool:
    """Delete a source DB registration."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM source_db_registry WHERE id = $1",
            db_id,
        )
        # Parse result string like "DELETE 1"
        return "1" in result


def source_db_to_prompt_entry(db: dict) -> str:
    """Format a source DB registry entry as a prompt snippet for the LLM."""
    parts = [
        f"- **{db['name']}** ({db['db_type']}@{db['host']}:{db['port']}/{db['database_name']})",
    ]
    if db.get("description"):
        parts.append(f"  Description: {db['description']}")
    if db.get("business_desc"):
        parts.append(f"  Business context: {db['business_desc']}")
    if db.get("tags"):
        parts.append(f"  Tags: {', '.join(db['tags'])}")
    if db.get("table_count"):
        parts.append(f"  Tables: {db['table_count']}")
    if db.get("last_explored_at"):
        parts.append(f"  Last explored: {db['last_explored_at']}")
    return "\n".join(parts)


def build_source_dbs_prompt(dbs: list[dict]) -> str:
    """Build a prompt section listing available source databases for the agent."""
    if not dbs:
        return "No source databases registered."

    lines = ["## Available Source Databases", ""]
    for db in dbs:
        lines.append(source_db_to_prompt_entry(db))
        lines.append("")
    return "\n".join(lines)


# ── Helpers ─────────────────────────────────────────────────────


def json_serialize(obj: Any) -> str:
    """Serialize an object to JSON string for PG JSONB."""
    import json
    return json.dumps(obj, default=str)


# ── Task artifact persistence ─────────────────────────────────────


def _serialize_model(obj: Any) -> Any:
    """Serialize a Pydantic model or list of models to JSON-compatible dict."""
    import json

    if obj is None:
        return None
    if isinstance(obj, list):
        return json.loads(json.dumps([_to_dict(o) for o in obj], default=str))
    return json.loads(json.dumps(_to_dict(obj), default=str))


def _to_dict(obj: Any) -> Any:
    """Recursively convert models and containers to JSON-compatible values."""
    if hasattr(obj, "model_dump"):
        return _to_dict(obj.model_dump())
    elif hasattr(obj, "dict"):
        return _to_dict(obj.dict())
    elif isinstance(obj, dict):
        return {key: _to_dict(value) for key, value in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_to_dict(value) for value in obj]
    return obj


def _redact_sensitive_samples(explorations: Any) -> Any:
    """Remove sensitive samples from serialized exploration artifacts."""
    serialized = _serialize_model(explorations)
    if not isinstance(serialized, dict):
        return serialized

    from wren_agent.tools.db_explorer import detect_sensitive_field

    for exploration in serialized.values():
        if not isinstance(exploration, dict):
            continue
        for table in exploration.get("tables", []):
            if not isinstance(table, dict):
                continue
            table_name = str(table.get("name", ""))
            for field in table.get("fields", []):
                if not isinstance(field, dict):
                    continue
                field_name = str(field.get("name", ""))
                if field.get("is_sensitive") or detect_sensitive_field(table_name, field_name):
                    field["is_sensitive"] = True
                    field["sample_values"] = []
    return serialized


def _dedupe_node_timeline(timeline: list | None) -> list:
    """Latest-wins dedupe by node name, preserving first-seen order.

    Defense-in-depth: the node tracer already dedupes in state, but dirty
    pre-fix rows and racing writes must never be persisted as duplicates
    (the dashboard counts entries → "78/29 completed").
    """
    if not timeline:
        return []
    index: dict[str, int] = {}
    result: list = []
    for entry in timeline:
        node = entry.get("node") if isinstance(entry, dict) else None
        if node is None:
            result.append(entry)
            continue
        if node in index:
            result[index[node]] = entry
        else:
            index[node] = len(result)
            result.append(entry)
    return result


async def persist_task_artifacts(state: dict) -> None:
    """Persist completed task artifacts to dedicated PG tables.

    Called from engine._run_task() in the finally block.
    Only persists if task completed or failed (not for cancelled mid-run).
    """
    import json

    try:
        pool = get_pg_pool()
        task_id = state.get("task_id", "")

        # ── task_artifacts ──
        design_plan = _serialize_model(state.get("design_plan"))
        explorations = _redact_sensitive_samples(state.get("explorations", {}))
        sensitive_fields = _serialize_model(state.get("sensitive_fields", []))
        schema_drifts = _serialize_model(state.get("schema_drifts", []))
        incremental_strategies = _serialize_model(state.get("incremental_strategies", []))
        matched_blueprints = _serialize_model(state.get("matched_blueprints", []))

        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO task_artifacts
                    (task_id, design_plan, construction_report, plain_report, data_asset_map,
                     kg_export_id, explorations, sensitive_fields, schema_drifts,
                     incremental_strategies, matched_blueprints)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (task_id) DO UPDATE SET
                    design_plan = COALESCE(EXCLUDED.design_plan, task_artifacts.design_plan),
                    construction_report = COALESCE(NULLIF(EXCLUDED.construction_report, ''), task_artifacts.construction_report),
                    plain_report = COALESCE(NULLIF(EXCLUDED.plain_report, ''), task_artifacts.plain_report),
                    data_asset_map = COALESCE(NULLIF(EXCLUDED.data_asset_map, ''), task_artifacts.data_asset_map),
                    kg_export_id = COALESCE(NULLIF(EXCLUDED.kg_export_id, ''), task_artifacts.kg_export_id),
                    explorations = COALESCE(EXCLUDED.explorations, task_artifacts.explorations),
                    sensitive_fields = COALESCE(EXCLUDED.sensitive_fields, task_artifacts.sensitive_fields),
                    schema_drifts = COALESCE(EXCLUDED.schema_drifts, task_artifacts.schema_drifts),
                    incremental_strategies = COALESCE(EXCLUDED.incremental_strategies, task_artifacts.incremental_strategies),
                    matched_blueprints = COALESCE(EXCLUDED.matched_blueprints, task_artifacts.matched_blueprints)
                """,
                task_id,
                json.dumps(design_plan) if design_plan else None,
                state.get("construction_report", ""),
                state.get("plain_report", ""),
                state.get("data_asset_map", ""),
                state.get("kg_export_id", ""),
                json.dumps(explorations) if explorations else None,
                json.dumps(sensitive_fields) if sensitive_fields else None,
                json.dumps(schema_drifts) if schema_drifts else None,
                json.dumps(incremental_strategies) if incremental_strategies else None,
                json.dumps(matched_blueprints) if matched_blueprints else None,
            )

            # ── task_lifecycle ──
            validation_results = _serialize_model(state.get("validation_results", []))
            execution_results = _serialize_model(state.get("execution_results", []))
            error_classifications = _serialize_model(state.get("error_classifications", []))
            clarifications = _serialize_model(state.get("clarifications", []))
            approval_items = _serialize_model(state.get("approval_items", []))
            node_timeline = _dedupe_node_timeline(state.get("node_timeline", []))
            audit_log = state.get("audit_log", [])
            errors = state.get("errors", [])
            warnings = state.get("warnings", [])

            await conn.execute(
                """
                INSERT INTO task_lifecycle
                    (task_id, validation_results, execution_results,
                     error_classifications, clarifications, approval_items,
                     node_timeline, audit_log, errors, warnings)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (task_id) DO UPDATE SET
                    validation_results = COALESCE(EXCLUDED.validation_results, task_lifecycle.validation_results),
                    execution_results = COALESCE(EXCLUDED.execution_results, task_lifecycle.execution_results),
                    error_classifications = COALESCE(EXCLUDED.error_classifications, task_lifecycle.error_classifications),
                    clarifications = COALESCE(EXCLUDED.clarifications, task_lifecycle.clarifications),
                    approval_items = COALESCE(EXCLUDED.approval_items, task_lifecycle.approval_items),
                    node_timeline = COALESCE(EXCLUDED.node_timeline, task_lifecycle.node_timeline),
                    audit_log = COALESCE(EXCLUDED.audit_log, task_lifecycle.audit_log),
                    errors = COALESCE(EXCLUDED.errors, task_lifecycle.errors),
                    warnings = COALESCE(EXCLUDED.warnings, task_lifecycle.warnings),
                    updated_at = NOW()
                """,
                task_id,
                json.dumps(validation_results) if validation_results else None,
                json.dumps(execution_results) if execution_results else None,
                json.dumps(error_classifications) if error_classifications else None,
                json.dumps(clarifications) if clarifications else None,
                json.dumps(approval_items) if approval_items else None,
                json.dumps(node_timeline) if node_timeline else None,
                json.dumps(audit_log) if audit_log else None,
                json.dumps(errors) if errors else None,
                json.dumps(warnings) if warnings else None,
            )

        logger.info(f"[{task_id[:8]}] Artifacts persisted to PG")

    except Exception as e:
        logger.warning(f"Failed to persist task artifacts: {e}")


async def persist_llm_trace(
    task_id: str,
    node: str,
    skills: list[str] | tuple[str, ...],
    system_prompt: str,
    user_message: str,
    response: str,
    duration_ms: float = 0,
) -> None:
    """Persist one full LLM call (prompt + response) to llm_trace.

    Written fire-and-forget from LLMClient — the complete conversation
    corpus for skill optimization and eval/ grading. Contents are capped
    at 100k chars per field to protect PG from pathological rows.
    """
    import json
    from wren_agent.tools.redaction import redact_text

    if not task_id:
        return

    _cap = 100_000
    try:
        pool = get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO llm_trace
                    (task_id, node, skills, system_prompt, user_message, response, duration_ms)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                task_id,
                node[:64],
                json.dumps(list(skills), ensure_ascii=False),
                redact_text(system_prompt or "")[:_cap],
                redact_text(user_message or "")[:_cap],
                redact_text(response or "")[:_cap],
                float(duration_ms),
            )
    except Exception as e:
        logger.debug(f"LLM trace persist skipped for {task_id[:8]}: {e}")


async def persist_runtime_lifecycle(
    task_id: str,
    node_timeline: list,
    audit_log: list,
    errors: list | None = None,
    warnings: list | None = None,
) -> None:
    """Incrementally upsert runtime observability data for a RUNNING task.

    Called fire-and-forget from the node tracer after each node finishes,
    so the dashboard can rebuild Event/Node timelines after a page switch
    (the bare graph has no checkpointer — this is the only mid-run source).
    """
    import json

    if not task_id:
        return
    node_timeline = _dedupe_node_timeline(node_timeline)
    try:
        pool = get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO task_lifecycle (task_id, node_timeline, audit_log, errors, warnings)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (task_id) DO UPDATE SET
                    node_timeline = COALESCE(EXCLUDED.node_timeline, task_lifecycle.node_timeline),
                    audit_log = COALESCE(EXCLUDED.audit_log, task_lifecycle.audit_log),
                    errors = COALESCE(EXCLUDED.errors, task_lifecycle.errors),
                    warnings = COALESCE(EXCLUDED.warnings, task_lifecycle.warnings),
                    updated_at = NOW()
                """,
                task_id,
                json.dumps(node_timeline, default=str) if node_timeline else None,
                json.dumps(audit_log, default=str) if audit_log else None,
                json.dumps(errors, default=str) if errors else None,
                json.dumps(warnings, default=str) if warnings else None,
            )
    except Exception as e:
        logger.debug(f"Runtime lifecycle persist skipped for {task_id[:8]}: {e}")


async def persist_runtime_task_status(
    task_id: str, status: str, phase: str = "",
) -> None:
    """Mirror the RUNNING task's status/phase onto modeling_tasks mid-run.

    Called fire-and-forget from the node tracer after every node completes.
    The terminal/checkpoint writers only fire at pause/end points, so a long
    design run left modeling_tasks.status stuck at the creation value
    (`exploring/init`) while nodes advanced — the dashboard and any monitor
    could not tell real progress (task d28431de: 74-minute run, status never
    left exploring/init).
    """
    if not task_id or not status:
        return
    try:
        pool = get_pg_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO modeling_tasks (task_id, thread_id, status, phase, modeling_goal) "
                "VALUES ($1, $1, $2, $3, '') "
                "ON CONFLICT (task_id) DO UPDATE SET "
                "status=$2, phase=$3, updated_at=NOW()",
                task_id, status, phase or status,
            )
    except Exception as e:
        logger.debug(f"Runtime status persist skipped for {task_id[:8]}: {e}")


async def load_task_artifacts(task_id: str) -> dict | None:
    """Load persisted task artifacts + lifecycle for a completed task.

    Returns None if no persisted data found (task still running or never persisted).
    """
    try:
        pool = get_pg_pool()
        async with pool.acquire() as conn:
            artifacts = await conn.fetchrow(
                "SELECT * FROM task_artifacts WHERE task_id = $1", task_id
            )
            lifecycle = await conn.fetchrow(
                "SELECT * FROM task_lifecycle WHERE task_id = $1", task_id
            )

        if not artifacts and not lifecycle:
            return None

        result = {}

        if artifacts:
            import json
            row = dict(artifacts)
            for field in ("design_plan", "explorations", "sensitive_fields",
                          "schema_drifts", "incremental_strategies", "matched_blueprints"):
                if row.get(field) and isinstance(row[field], str):
                    try:
                        row[field] = json.loads(row[field])
                    except (json.JSONDecodeError, TypeError):
                        pass
            result.update(row)

        if lifecycle:
            import json
            row = dict(lifecycle)
            for field in ("validation_results", "execution_results",
                          "error_classifications", "clarifications", "approval_items",
                          "node_timeline", "audit_log", "errors", "warnings"):
                if row.get(field) and isinstance(row[field], str):
                    try:
                        row[field] = json.loads(row[field])
                    except (json.JSONDecodeError, TypeError):
                        pass
            result.update(row)

        return result

    except Exception as e:
        logger.error(f"Failed to load task artifacts for {task_id}: {e}")
        return None


async def list_task_artifacts_designs(limit: int | None = None) -> list[dict]:
    """List persisted design plans across tasks (history-scan data source).

    Returns [{"task_id": str, "design_plan": dict}, ...] with design_plan
    JSON-decoded. Optional limit caps the number of tasks scanned.
    """
    pool = get_pg_pool()
    sql = "SELECT task_id, design_plan FROM task_artifacts WHERE design_plan IS NOT NULL"
    params: list[Any] = []
    if limit:
        params.append(limit)
        sql += f" ORDER BY created_at DESC LIMIT ${len(params)}"
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    out = []
    for r in rows:
        plan = r["design_plan"]
        if isinstance(plan, str):
            try:
                plan = json.loads(plan)
            except (json.JSONDecodeError, TypeError):
                continue
        if isinstance(plan, dict):
            out.append({"task_id": r["task_id"], "design_plan": plan})
    return out


# ── Approval narratives (migration 012) ──────────────────────────
# Per-approval-round narrative blocks (LLM-generated goal/metric/impact
# interpretations + computed risk block). See wayfinder T-01/T-02.


async def upsert_approval_narrative(
    task_id: str,
    approval_id: str,
    block: str,
    status: str,
    content: dict | None = None,
    error: str = "",
    attempts: int = 0,
) -> None:
    """Upsert one narrative block row for an approval round."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO approval_narratives
                (approval_id, task_id, block, status, content_json, error, attempts, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
            ON CONFLICT (approval_id, block) DO UPDATE SET
                status       = EXCLUDED.status,
                content_json = EXCLUDED.content_json,
                error        = EXCLUDED.error,
                attempts     = EXCLUDED.attempts,
                updated_at   = NOW()
            """,
            approval_id, task_id, block, status,
            json.dumps(content, ensure_ascii=False) if content is not None else None,
            error, attempts,
        )


async def load_approval_narratives(approval_id: str) -> list[dict]:
    """Load all narrative blocks for an approval round."""
    pool = get_pg_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT block, status, content_json, error, attempts, updated_at "
            "FROM approval_narratives WHERE approval_id = $1",
            approval_id,
        )
    out = []
    for r in rows:
        row = dict(r)
        if row.get("content_json") and isinstance(row["content_json"], str):
            try:
                row["content_json"] = json.loads(row["content_json"])
            except (json.JSONDecodeError, TypeError):
                pass
        out.append(row)
    return out
