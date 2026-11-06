"""Schema exploration nodes — direct DBConnector per task.

Each ModelingTask creates its own DBConnector instances dynamically.
Connectors are created on-demand, used, then released — never global singletons.

Architecture:
  Agent Node → DBConnector(host,port,user,password,database,db_type) → Database

The MCP server (db_server.py) is available as an optional external interface
for tools that want to discover/query databases via the MCP protocol.
"""

from __future__ import annotations

import asyncio
import logging
import re as _re
import uuid
from datetime import datetime, timezone
from typing import Any

from wren_agent.models.schema import (
    FieldSchema,
    RelationshipType,
    SchemaExploration,
    TableSchema,
    TableRelationship,
)
from wren_agent.models.task import SensorResult, TaskPhase
from wren_agent.state import AgentState, is_ods_migration_task

logger = logging.getLogger(__name__)


def _collect_comment_semantics(explorations: dict) -> list[dict]:
    """Collect table.field -> comment rows for the semantic dictionary.

    Only fields with a non-empty column comment are harvested; comments that
    are just the column name itself (empty meaning) are skipped.
    """
    rows: list[dict] = []
    for exp in explorations.values():
        for table in getattr(exp, "tables", []) or []:
            for f in table.fields:
                comment = (getattr(f, "comment", None) or "").strip()
                if not comment or comment.lower() == f.name.lower():
                    continue
                rows.append({
                    "table_name": table.name,
                    "field_name": f.name,
                    "chinese_meaning": comment[:200],
                })
    return rows


# Layers explorable in warehouse mode — ods is out of scope and ads is a
# produce-only target (see wayfinder T-06). dwm is kept for clusters that
# have a mid-layer database.
WAREHOUSE_EXPLORABLE_LAYERS = ("dim", "dwd", "dwm", "dws")


async def _attach_safe_samples(connector, schema, table_name: str, limit: int = 5) -> None:
    """Populate non-sensitive samples without selecting sensitive columns."""
    from wren_agent.tools.db_explorer import apply_safe_samples, safe_sample_columns

    safe_columns = safe_sample_columns(schema)
    if not safe_columns:
        return

    samples = await connector.sample_data(
        table_name, limit=limit, columns=safe_columns,
    )
    apply_safe_samples(schema, samples, max_values=3)


async def _explore_tables_on_connector(
    connector,
    table_names: list[str],
    *,
    with_samples: bool = False,
    profile_nulls: bool = False,
) -> list[TableSchema]:
    """Fetch table schemas on an open connector (batched catalog queries).

    Light exploration defaults to metadata only — per-table ``SELECT … LIMIT``
    sampling is deferred to deep explore. That avoids N extra round-trips on
    every warehouse table during ``schema_explore_light``. When
    ``profile_nulls`` is set (warehouse layers, and core_model ODS explore),
    date/period columns also get a fill-rate ``null_ratio`` so empty dates
    copied by core modeling cannot be used as warehouse grain.
    """
    if not table_names:
        return []

    schemas = await connector.get_tables_info(table_names)
    logger.info(
        "Explore batch %s: %d/%d tables (samples=%s, null_profile=%s)",
        getattr(connector, "database", "?"),
        len(schemas),
        len(table_names),
        with_samples,
        profile_nulls,
    )
    if with_samples:
        for schema in schemas:
            await _attach_safe_samples(connector, schema, schema.name)
    if profile_nulls:
        from wren_agent.tools.column_profile import attach_null_ratios

        for schema in schemas:
            try:
                await attach_null_ratios(schema, connector.query_one)
            except Exception:
                logger.warning(
                    "null_ratio profile failed for %s.%s",
                    schema.schema_name, schema.name, exc_info=True,
                )
    return schemas


# Candidate join-key name pairs probed by VALUE OVERLAP when Doris exposes no
# FK metadata. The `*` wildcard matches any column of the given base name
# (e.g. `*_id` joins `contract_id` ↔ `contract_id` across tables).
_JOIN_KEY_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("contract_id", "contract_id"),
    ("contract_id", "source_record_id"),
    ("org_id", "org_id"),
    ("org_id", "organization_id"),
    ("store_id", "store_id"),
    ("dept_id", "dept_id"),
    ("group_id", "group_id"),
    ("operate_customer_contract_id", "contract_id"),
    ("operate_customer_contract_id", "source_record_id"),
    ("*_id", "*_id"),
)


def _join_key_pairs(tables: list[TableSchema]) -> list[tuple[str, str, str, str]]:
    """(source_table, source_col, target_table, target_col) candidate pairs."""
    by_name: dict[str, str] = {}
    for t in tables:
        by_name[t.name] = t.name
    cols_by_table = {t.name: {f.name for f in t.fields} for t in tables}
    pairs: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    names = list(by_name)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            ca, cb = cols_by_table[a], cols_by_table[b]
            for ka, kb in _JOIN_KEY_CANDIDATES:
                ma = sorted(x for x in ca if _col_matches(x, ka))
                mb = sorted(x for x in cb if _col_matches(x, kb))
                for x in ma:
                    for y in mb:
                        key = (a, x, b, y)
                        if key in seen:
                            continue
                        seen.add(key)
                        pairs.append(key)
                        pairs.append((b, y, a, x))
            # wildcard: any same-named *_id column across the pair
            common = sorted(ca & cb & {c for c in ca if _col_matches(c, "*_id")})
            for c in common:
                key = (a, c, b, c)
                if key in seen:
                    continue
                seen.add(key)
                pairs.append(key)
                pairs.append((b, c, a, c))
    return pairs


def _col_matches(col: str, pattern: str) -> bool:
    col = col.lower()
    pattern = pattern.lower()
    if pattern.endswith("*_id"):
        return col.endswith("_id")
    return col == pattern


async def _probe_join_relationships(
    connector: Any, tables: list[TableSchema], database: str,
) -> list[TableRelationship]:
    """Value-overlap probes for candidate join keys — a lightweight EXISTS
    check, bounded per layer, marked verified when the overlap is real.

    Doris has no FK metadata, so without this the design LLM gets zero join
    hints and guesses keys by name alone (task bfa6c0d8: `operate_customer_
    contract_id = contract_id` — two disjoint ID spaces, materialized 0
    rows). Each probe is `SELECT 1 ... LIMIT 1` so it costs a fraction of a
    row scan; failures (bad column, permission) are skipped silently.
    """
    if not tables or len(tables) < 2:
        return []
    pairs = _join_key_pairs(tables)
    # Cap probes per layer so a 52-table layer cannot issue thousands of
    # round-trips; the fact tables come first in exploration order.
    _MAX_PROBES = 40
    pairs = pairs[:_MAX_PROBES]
    schema_by_name = {t.name: t.schema_name or "" for t in tables}
    hits: list[TableRelationship] = []
    for src, src_col, dst, dst_col in pairs:
        # Cross-db pairs (dim ↔ dwd share one exploration but live in
        # different catalogs) must be qualified; same-db stays bare.
        sq = schema_by_name.get(src, "")
        dq = schema_by_name.get(dst, "")
        s_qual = f"{sq}.`{src}`" if sq else f"`{src}`"
        d_qual = f"{dq}.`{dst}`" if dq else f"`{dst}`"
        try:
            ok = await connector.query_value(
                f"SELECT EXISTS(SELECT 1 FROM {s_qual} s JOIN {d_qual} d "
                f"ON s.`{src_col}` = d.`{dst_col}`) AS hit"
            )
        except Exception:
            continue
        if ok:
            hits.append(TableRelationship(
                source_table=src, source_column=src_col,
                target_table=dst, target_column=dst_col,
                relationship_type=RelationshipType.MANY_TO_ONE,
                verified=True,
                source="value_overlap_probe",
            ))
    if hits:
        logger.info(
            "Join value-overlap probe on %s: %d candidate relationship(s) found",
            database, len(hits),
        )
    return hits


async def _explore_tables_for_db(
    sdb: dict[str, Any],
    table_names: list[str],
    *,
    with_samples: bool = False,
) -> list[TableSchema]:
    """Explore tables on one source DB using a single reused connection."""
    from wren_agent.tools.db_explorer import DBConnector

    if not table_names:
        return []

    connector = DBConnector(
        host=sdb["host"], port=sdb["port"],
        user=sdb["user"], password=sdb["password"],
        database=sdb["database"], db_type=sdb["db_type"],
        connection_params=sdb.get("connection_params") or {},
    )
    try:
        return await _explore_tables_on_connector(
            connector, table_names, with_samples=with_samples,
        )
    finally:
        await connector.aclose()


# Back-compat alias used by tests
_explore_tables_parallel = _explore_tables_for_db


def _explore_table_cap() -> int:
    from wren_agent.config import settings
    return settings.agent.explore_max_tables


def _select_tables_to_explore(
    candidates: list[str],
    *,
    cap: int,
    business_task_type: str = "full",
) -> tuple[list[str], bool]:
    """Choose which catalog names to describe.

    Whole-database ODS migration must explore every source table; the cap is
    a prompt-size guard for `full` / core-model tasks, not a sync filter.
    """
    if business_task_type == "ods_migration":
        return list(candidates), False
    if len(candidates) > cap:
        return candidates[:cap], True
    return list(candidates), False

# Layer -> table-name prefix filter for single-db mixed deployments (T-06).
# Each layer keeps its own prefixes and drops the other layers' prefixes, so
# a wren_dw-style database explored as "dim" never surfaces dws_/ods_/ads_
# tables. Prefix-less tables survive (star-schema / misc tables stay visible).
_LAYER_PREFIXES = {
    "dim": (("dim_",), ("ods_", "dwd_", "dwm_", "dws_", "ads_", "fct_")),
    "dwd": (("dwd_", "dwm_", "fct_"), ("ods_", "dim_", "dws_", "ads_")),
    "dwm": (("dwd_", "dwm_", "fct_"), ("ods_", "dim_", "dws_", "ads_")),
    "dws": (("dws_",), ("ods_", "dim_", "dwd_", "dwm_", "ads_", "fct_")),
}


def _filter_layer_tables(tables: list[str], layer: str) -> list[str]:
    """Keep tables belonging to `layer` by name-prefix convention (T-06).

    Only the explicitly foreign prefixes are dropped (e.g. a dim-layer
    exploration must not surface dws_/ods_/ads_/dwd_ tables); everything else
    — including prefix-less utility tables — stays visible.

    Call this only for databases mapped to **multiple** layers (wren_dw-style
    mixed single-db). Dedicated layer databases (ecommerce / db_*) must
    keep every table — including ``fct_`` facts inside a dim-named database.
    """
    _, drop = _LAYER_PREFIXES.get(layer, ((), ()))
    out = []
    for t in tables:
        lower = t.lower()
        if any(lower.startswith(p) for p in drop):
            continue
        out.append(t)
    return out


def _db_layer_appearances(layers: dict, database: str) -> int:
    """How many layer keys in ``layers`` map include ``database``."""
    count = 0
    for raw in layers.values():
        dbs = raw if isinstance(raw, list) else ([raw] if raw else [])
        mapped = {str(d).strip() for d in dbs if str(d).strip()}
        if database in mapped:
            count += 1
    return count


def _tables_for_warehouse_layer(
    all_tables: list[str],
    layer: str,
    layer_db: str,
    layers: dict,
) -> list[str]:
    """Select tables to explore for one warehouse layer database (T-06).

    - Multi-layer shared DB → prefix-filter so each layer key stays pure.
    - Dedicated layer DB → take the full catalog (star-schema fct_/dim_ 全收).
    """
    if _db_layer_appearances(layers, layer_db) > 1:
        return _filter_layer_tables(all_tables, layer)
    return list(all_tables)


async def schema_explore_light(state: AgentState) -> AgentState:
    """Phase 1: Light exploration — creates DBConnector per source DB.

    For each source database:
    1. Create a DBConnector with the DB credentials
    2. List tables, get column info, sample data, discover FKs
    3. Store results, release the connector

    Warehouse mode (source_mode="warehouse"): a single Doris cluster source is
    explored per layer database — exploration keys become "<source>#<layer>".
    """
    from wren_agent.tools.db_explorer import DBConnector

    now = datetime.now(timezone.utc)
    state["updated_at"] = now
    state["phase"] = TaskPhase.SCHEMA_EXPLORE_LIGHT

    source_dbs = state.get("source_databases", [])
    logger.info(
        "[%s] schema_explore_light start mode=%s sources=%d",
        state.get("task_id", "")[:8],
        state.get("source_mode", "business_db"),
        len(source_dbs),
    )

    if (state.get("source_mode") == "warehouse"
            or state.get("business_task_type") == "core_model"):
        explorations = await _explore_warehouse_layers(state)
    else:
        explorations = await _explore_business_dbs(state)

    state["explorations"] = explorations
    logger.info(
        "[%s] schema_explore_light raw done: %d exploration keys, %d tables",
        state.get("task_id", "")[:8],
        len(explorations),
        sum(len(e.tables) for e in explorations.values()),
    )

    # Field semantics harvest (A-side): Doris column comments are the runtime
    # truth for field meanings. Upsert them into the dictionary so grill
    # stops clarifying commented fields — authoritative xlsx rows are never
    # overwritten (see upsert_field_semantics_from_comments).
    try:
        from wren_agent.tools.pg_store import upsert_field_semantics_from_comments

        rows = _collect_comment_semantics(explorations)
        if rows:
            written = await upsert_field_semantics_from_comments(rows)
            logger.info(
                "[%s] Harvested %d field semantics from column comments",
                state.get("task_id", "")[:8], written,
            )
            # Embed only tables touched by this harvest, capped, and off the
            # critical path. A previous await backfill_pending_embeddings()
            # drained the entire NULL-embedding corpus (and could loop forever
            # when the embedding API failed) — so tiny explores still hung on
            # "Schema 探索" for minutes.
            import asyncio as _asyncio
            from wren_agent.tools.embedding import backfill_pending_embeddings

            harvested_tables = sorted({r["table_name"] for r in rows})
            max_embed = min(max(len(rows), 1), 100)

            async def _bg_embed() -> None:
                try:
                    await backfill_pending_embeddings(
                        max_rows=max_embed,
                        table_names=harvested_tables,
                    )
                except Exception:
                    logger.warning(
                        "[%s] Background embedding backfill failed",
                        state.get("task_id", "")[:8], exc_info=True,
                    )

            try:
                _asyncio.get_running_loop().create_task(_bg_embed())
            except RuntimeError:
                # No running loop (sync/test callers) — run inline.
                await _bg_embed()
    except Exception:
        logger.warning("Field semantics harvest failed", exc_info=True)

    from wren_agent.tools.event_emitter import emit_phase
    emit_phase(state, "schema_explore_light")

    total_tables = sum(len(e.tables) for e in explorations.values())

    state["audit_log"] = state.get("audit_log", []) + [{
        "phase": "schema_explore_light",
        "action": "completed",
        "databases_explored": len(explorations),
        "tables": total_tables,
        "timestamp": now.isoformat(),
    }]

    # Fail-fast gate: with source DBs configured but zero tables explored,
    # every downstream design layer would be empty (zero-column ADS DDL,
    # meaningless LLM reviews, revise/deploy loops). Stop here with a clear
    # error instead of letting the empty state cascade.
    if source_dbs and total_tables == 0:
        from wren_agent.models.task import TaskStatus
        from wren_agent.tools.event_emitter import emit_error, emit_status

        detail = "; ".join(state.get("errors", [])[-len(source_dbs):]) or "no tables matched"
        msg = (
            f"Schema exploration produced no tables from {len(source_dbs)} "
            f"source database(s) — aborting before design. Causes: {detail}"
        )
        state["errors"] = state.get("errors", []) + [msg]
        state["status"] = TaskStatus.FAILED
        logger.error("[%s] %s", state.get("task_id", "")[:8], msg)
        emit_error(state, msg)
        emit_status(state, TaskStatus.FAILED.value)
        try:
            from wren_agent.engine import update_task_pg_status
            await update_task_pg_status(
                state.get("task_id", ""), TaskStatus.FAILED.value, "schema_explore_light"
            )
        except Exception:
            logger.warning("Failed to persist FAILED status to PG", exc_info=True)

    return state


async def _explore_business_dbs(state: AgentState) -> dict[str, SchemaExploration]:
    """Business-db mode exploration — one DBConnector per source database."""
    from wren_agent.tools.db_explorer import DBConnector

    source_dbs = state.get("source_databases", [])
    explorations: dict[str, SchemaExploration] = {}

    for source_db in source_dbs:
        sdb = await _extract_db_params(source_db)
        db_id = sdb["db_id"]

        if not sdb["host"] or not sdb["database"]:
            state["warnings"] = state.get("warnings", []) + [
                f"Source DB '{db_id}' missing host/database — skipping"
            ]
            continue

        if sdb["credential_source"] in ("missing", "lookup_error"):
            # Never connect with empty credentials — the MySQL driver would
            # fall back to the OS username and fail with a confusing 1045.
            state["errors"] = state.get("errors", []) + [
                f"数据源未注册或凭据缺失: id='{db_id}' name='{sdb['name']}' "
                f"({sdb['credential_source']}) — 跳过探索，请先在数据源管理中注册该库"
            ]
            logger.warning(
                "Skipping exploration of '%s' (name='%s'): credential_source=%s",
                db_id, sdb["name"], sdb["credential_source"],
            )
            continue

        connector = None
        try:
            connector = DBConnector(
                host=sdb["host"], port=sdb["port"],
                user=sdb["user"], password=sdb["password"],
                database=sdb["database"], db_type=sdb["db_type"],
                connection_params=sdb["connection_params"],
            )
            all_tables = await connector.list_tables()
            if is_ods_migration_task(state):
                candidates = list(all_tables)
            else:
                relevant = _filter_relevant_tables(all_tables, state["modeling_goal"])
                # Vocabulary miss → explore the whole DB (caller decides), not a
                # hidden fallback inside the filter helper.
                candidates = relevant if relevant else list(all_tables)
            cap = _explore_table_cap()
            to_explore, truncated = _select_tables_to_explore(
                candidates,
                cap=cap,
                business_task_type=(
                    "ods_migration" if is_ods_migration_task(state)
                    else (state.get("business_task_type") or "full")
                ),
            )
            if truncated:
                state["warnings"] = state.get("warnings", []) + [
                    f"'{db_id}': exploring first {cap} of {len(candidates)} tables "
                    f"(AGENT_EXPLORE_MAX_TABLES)"
                ]

            exploration = SchemaExploration(
                source_db_id=db_id,
                phase="light",
                catalog_table_names=list(all_tables),
            )
            exploration.tables = await _explore_tables_on_connector(
                connector, to_explore,
                profile_nulls=(
                    state.get("business_task_type") == "core_model"
                ),
            )

            fks = await connector.get_foreign_keys()
            explored_names = {t.name for t in exploration.tables}
            for fk in fks:
                if fk.source_table in explored_names and fk.target_table in explored_names:
                    exploration.relationships.append(fk)

            explorations[db_id] = exploration

            state["warnings"] = state.get("warnings", []) + [
                f"Explored '{db_id}': {len(exploration.tables)} tables, "
                f"{len(exploration.relationships)} relationships"
            ]

        except Exception as e:
            state["errors"] = state.get("errors", []) + [
                f"Failed to explore '{db_id}': {e}"
            ]
            logger.error("Exploration of '%s' failed", db_id, exc_info=True)
        finally:
            if connector is not None:
                await connector.aclose()

    return explorations


async def _explore_warehouse_layers(state: AgentState) -> dict[str, SchemaExploration]:
    """Warehouse-mode exploration — one Doris cluster source, per-layer loop.

    Each explorable layer (dim/dwd/dwm/dws) maps to a database via
    `connection_params.layers`; results are keyed "<source_id>#<layer>"
    (see wayfinder T-07). ods/ads are skipped (T-06).
    """
    from wren_agent.tools.db_explorer import DBConnector

    source_dbs = state.get("source_databases", [])
    explorations: dict[str, SchemaExploration] = {}
    # Same host:port/database/layer across warehouse sources must not be
    # catalog-scanned twice. Layer stays in the key so a mixed single-db
    # (wren_dw) still gets one prefix-filtered pass per layer.
    seen_scoped: set[tuple[str, int, str, str]] = set()
    cap = _explore_table_cap()

    for source_db in source_dbs:
        sdb = await _extract_db_params(source_db)
        db_id = sdb["db_id"]

        if not sdb["host"]:
            state["warnings"] = state.get("warnings", []) + [
                f"Warehouse source '{db_id}' missing host — skipping"
            ]
            continue

        if sdb["credential_source"] in ("missing", "lookup_error"):
            state["errors"] = state.get("errors", []) + [
                f"数仓源未注册或凭据缺失: id='{db_id}' name='{sdb['name']}' "
                f"({sdb['credential_source']}) — 跳过探索，请先在数据源管理中注册该数仓源"
            ]
            logger.warning(
                "Skipping warehouse exploration of '%s' (name='%s'): credential_source=%s",
                db_id, sdb["name"], sdb["credential_source"],
            )
            continue

        layers = (sdb.get("connection_params") or {}).get("layers") or {}
        if not isinstance(layers, dict) or not layers:
            state["warnings"] = state.get("warnings", []) + [
                f"Warehouse source '{db_id}' has no connection_params.layers — skipping"
            ]
            continue

        explorable_layers = (("ods",) if state.get("business_task_type") == "core_model"
                             else WAREHOUSE_EXPLORABLE_LAYERS)
        for layer in explorable_layers:
            # One layer may host multiple databases (auto-discovered business
            # domains, T-13) — aggregate all of them into one exploration.
            raw = layers.get(layer)
            layer_dbs = raw if isinstance(raw, list) else ([raw] if raw else [])
            layer_dbs = [str(d).strip() for d in layer_dbs if str(d).strip()]
            if not layer_dbs:
                continue  # layer not present on this cluster
            # Task-level exploration subset from the task payload
            # (connection_params.explore_layers, sent by the dashboard UI):
            # layers unchecked in the UI are skipped even if the cluster hosts
            # them. Absent/empty list = explore all explorable layers.
            subset = (sdb.get("connection_params") or {}).get("explore_layers")
            if isinstance(subset, list) and subset and layer not in subset:
                continue
            # Task-level DATABASE subset (connection_params.explore_databases,
            # sent by the dashboard 库级选择 UI): only checked databases are
            # explored. An empty list means "explore none of this layer's
            # databases"; the key ABSENT means "all mapped databases".
            subset_dbs = (sdb.get("connection_params") or {}).get("explore_databases")
            if isinstance(subset_dbs, list):
                layer_dbs = [d for d in layer_dbs if d in subset_dbs]
            if not layer_dbs:
                continue
            explore_key = f"{db_id}#{layer}"
            exploration = SchemaExploration(source_db_id=explore_key, phase="light")
            try:
                for layer_db in layer_dbs:
                    scoped = (sdb["host"], int(sdb["port"]), layer_db, layer)
                    if scoped in seen_scoped:
                        logger.info(
                            "Skip duplicate warehouse explore %s@%s:%s/%s",
                            explore_key, scoped[0], scoped[1], scoped[2],
                        )
                        continue
                    seen_scoped.add(scoped)

                    connector = DBConnector(
                        host=sdb["host"], port=sdb["port"],
                        user=sdb["user"], password=sdb["password"],
                        database=layer_db, db_type=sdb["db_type"],
                        connection_params=sdb["connection_params"],
                    )
                    try:
                        all_tables = await connector.list_tables()
                        # T-06: prefix-filter only for multi-layer shared DBs;
                        # dedicated layer DBs keep the full catalog (incl. fct_).
                        layer_tables = _tables_for_warehouse_layer(
                            all_tables, layer, layer_db, layers,
                        )
                        exploration.catalog_table_names.extend(layer_tables)
                        to_explore, truncated = _select_tables_to_explore(
                            layer_tables,
                            cap=cap,
                            business_task_type=state.get("business_task_type") or "full",
                        )
                        if truncated:
                            state["warnings"] = state.get("warnings", []) + [
                                f"'{explore_key}' db={layer_db}: exploring first "
                                f"{cap} of {len(layer_tables)} tables "
                                f"(AGENT_EXPLORE_MAX_TABLES)"
                            ]
                        tables = await _explore_tables_on_connector(
                            connector, to_explore, profile_nulls=True,
                        )
                        exploration.tables.extend(tables)

                        # Doris typically has no FK metadata; empty list is fine.
                        fks = await connector.get_foreign_keys()
                        explored_names = {t.name for t in exploration.tables}
                        for fk in fks:
                            if (
                                fk.source_table in explored_names
                                and fk.target_table in explored_names
                            ):
                                exploration.relationships.append(fk)

                        # Doris exposes no FK constraints, so the LLM had ZERO
                        # join hints and guessed keys (task bfa6c0d8: the
                        # shop-level DWS joined `operate_customer_contract_id =
                        # contract_id` — no overlap, materialized 0 rows).
                        # Probe candidate join keys by VALUE OVERLAP on the
                        # core tables (existence check, bounded budget) and
                        # attach the hits as verified relationships.
                        if not exploration.relationships:
                            joined = await _probe_join_relationships(
                                connector, tables, layer_db,
                            )
                            exploration.relationships.extend(joined)
                    finally:
                        await connector.aclose()

                explorations[explore_key] = exploration
                state["warnings"] = state.get("warnings", []) + [
                    f"Explored '{explore_key}': {len(exploration.tables)} tables, "
                    f"{len(exploration.relationships)} relationships"
                ]
            except Exception as e:
                state["errors"] = state.get("errors", []) + [
                    f"Failed to explore '{explore_key}': {e}"
                ]
                logger.error(
                    "Warehouse exploration of '%s' failed", explore_key, exc_info=True,
                )

    return explorations


async def detect_sensitive_fields(state: AgentState) -> AgentState:
    """Detect sensitive fields across all explored tables."""
    from wren_agent.tools.db_explorer import detect_sensitive_field

    now = datetime.now(timezone.utc)
    state["updated_at"] = now

    explorations = state.get("explorations", {})
    all_sensitive: list[SensorResult] = []

    for db_id, exploration in explorations.items():
        for table in exploration.tables:
            for field in table.fields:
                result = detect_sensitive_field(table.name, field.name)
                if result:
                    all_sensitive.append(result)
                    field.is_sensitive = True
                if field.is_sensitive:
                    # Defense for legacy checkpoints created before samples were
                    # classified during exploration.
                    field.sample_values = []

    state["sensitive_fields"] = all_sensitive

    if all_sensitive:
        state["warnings"] = state.get("warnings", []) + [
            f"Detected {len(all_sensitive)} potentially sensitive fields"
        ]

    return state


async def confirm_sensitive(state: AgentState) -> AgentState:
    """Present sensitive fields for human confirmation."""
    from wren_agent.models.task import ClarificationItem

    now = datetime.now(timezone.utc)
    state["updated_at"] = now

    if state.get("source_mode") == "warehouse":
        # T-07: warehouse mode keeps detection (see detect_sensitive_fields)
        # but skips the HITL confirmation — fields are recorded in warnings.
        return state

    sensitive = state.get("sensitive_fields", [])
    if not sensitive:
        return state

    clarifications = state.get("clarifications", [])
    existing_questions = {c.question for c in clarifications}
    for sf in sensitive:
        if not sf.human_confirmed:
            question = (
                f"'{sf.table_name}.{sf.field_name}' detected as "
                f"{sf.sensor_type.value}. Mask?"
            )
            if question in existing_questions:
                continue
            clarifications.append(ClarificationItem(
                question=question,
                context=f"Confidence: {sf.confidence:.0%}",
                candidates=["null_out", "hash", "partial_mask", "pass_through"],
                impact_level="high", blocking=False,
                clarification_type="sensitive_field",
            ))
            existing_questions.add(question)

    state["clarifications"] = clarifications
    state["pending_human_input"] = True
    state["human_input_type"] = "clarification"
    return state


async def deep_explore(state: AgentState) -> AgentState:
    """Phase 2: Deep exploration — creates temporary DBConnector, runs, releases."""
    from wren_agent.tools.db_explorer import DBConnector

    now = datetime.now(timezone.utc)
    state["updated_at"] = now
    state["phase"] = TaskPhase.SCHEMA_EXPLORE_DEEP

    pending = state.get("deep_explore_pending", [])
    if not pending:
        return state

    source_dbs = state.get("source_databases", [])
    connectors: dict[str, DBConnector] = {}

    # Create connectors on demand (one per source DB)
    for sdb in source_dbs:
        s = await _extract_db_params(sdb)
        db_id = s["db_id"]
        if s["credential_source"] in ("missing", "lookup_error"):
            state["warnings"] = state.get("warnings", []) + [
                f"Deep explore skipped for '{db_id}': credentials unavailable "
                f"({s['credential_source']})"
            ]
            continue
        if db_id not in connectors:
            connectors[db_id] = DBConnector(
                host=s["host"], port=s["port"],
                user=s["user"], password=s["password"],
                database=s["database"], db_type=s["db_type"],
                connection_params=s["connection_params"],
            )

    for item in pending:
        try:
            db_id = item.get("source_db_id", "")
            connector = connectors.get(db_id)
            if not connector:
                continue

            if item.get("type") == "count_distinct":
                cnt = await connector.count_distinct(item["table"], item["column"])
                explorations = state.get("explorations", {})
                exp = explorations.get(db_id)
                if exp:
                    for t in exp.tables:
                        if t.name == item["table"]:
                            for f in t.fields:
                                if f.name == item["column"]:
                                    f.distinct_count = cnt

            elif item.get("type") == "join_verification":
                src = item.get("source", "").split(".")
                tgt = item.get("target", "").split(".")
                if len(src) == 2 and len(tgt) == 2:
                    ratio = await connector.verify_join_cardinality(
                        src[0], src[1], tgt[0], tgt[1],
                    )
                    exp = state.get("explorations", {}).get(db_id)
                    if exp:
                        for rel in exp.relationships:
                            if rel.source_table == src[0] and rel.target_table == tgt[0]:
                                rel.verified = True
                                rel.verified_ratio = ratio

        except Exception as e:
            state["warnings"] = state.get("warnings", []) + [
                f"Deep explore failed: {item} — {e}"
            ]
            logger.warning("Deep explore item failed: %s", item, exc_info=True)

    state["deep_explore_pending"] = []
    for connector in connectors.values():
        await connector.aclose()
    return state


async def human_confirm_deep(state: AgentState) -> AgentState:
    """Require explicit approval before running deep exploration queries."""
    now = datetime.now(timezone.utc)
    state["updated_at"] = now
    # Flip phase before any HITL wait so the UI does not keep showing
    # "Schema 探索" (light) while blocked on deep-explore authorization.
    state["phase"] = TaskPhase.SCHEMA_EXPLORE_DEEP

    explorations = state.get("explorations", {})
    deep_items: list[dict] = []

    for db_id, exploration in explorations.items():
        for table in exploration.tables:
            candidate_cols = [
                f for f in table.fields
                if f.distinct_count is None and not f.is_sensitive
            ]
            for col in candidate_cols[:3]:
                deep_items.append({
                    "type": "count_distinct",
                    "table": table.name, "column": col.name,
                    "source_db_id": db_id,
                    "estimated_cost": "medium" if (table.row_count_estimate or 0) > 1e5 else "low",
                    "purpose": "Determine cardinality",
                })

        for rel in exploration.relationships:
            if not rel.verified:
                cost = "high" if any(
                    (t.row_count_estimate or 0) > 1e6
                    for t in exploration.tables
                    if t.name in (rel.source_table, rel.target_table)
                ) else "medium"
                deep_items.append({
                    "type": "join_verification",
                    "source": f"{rel.source_table}.{rel.source_column}",
                    "target": f"{rel.target_table}.{rel.target_column}",
                    "source_db_id": db_id,
                    "estimated_cost": cost,
                    "purpose": f"Verify {rel.source_table} ↔ {rel.target_table} cardinality",
                })

    state["deep_explore_pending"] = deep_items
    if not deep_items:
        state["pending_human_input"] = False
        state["human_input_type"] = ""
        state["deep_explore_decision"] = ""
        return state

    from wren_agent.state import is_auto_mode
    if is_auto_mode(state):
        state["deep_explore_pending"] = []
        state["pending_human_input"] = False
        state["human_input_type"] = ""
        state["deep_explore_decision"] = "skipped_auto"
        state["warnings"] = state.get("warnings", []) + [
            "Auto mode skipped deep exploration because no human authorization was available"
        ]
        state["audit_log"] = state.get("audit_log", []) + [{
            "phase": "schema_explore_deep",
            "action": "skipped_without_human_authorization",
            "items_count": len(deep_items),
            "timestamp": now.isoformat(),
        }]
        return state

    approval_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"wren:{state['task_id']}:deep-explore",
    ))
    state["deep_explore_approval_id"] = approval_id
    state["pending_human_input"] = True
    state["human_input_type"] = "deep_explore_confirm"

    description_lines = [
        f"将执行 {len(deep_items)} 项只读深度探索查询，批准后才会访问源库："
    ]
    for item in deep_items[:20]:
        target = (
            f"{item['table']}.{item['column']}"
            if item["type"] == "count_distinct"
            else f"{item['source']} -> {item['target']}"
        )
        description_lines.append(
            f"- [{item['estimated_cost']}] {item['type']}: {target}"
        )
    if len(deep_items) > 20:
        description_lines.append(f"- 另有 {len(deep_items) - 20} 项")

    await _persist_deep_explore_approval(
        state["task_id"], approval_id, "深度探索查询授权",
        "\n".join(description_lines),
    )

    decision = await _load_deep_explore_decision(state["task_id"], approval_id)
    if decision:
        await _finish_deep_explore_gate(
            state, approval_id, decision, len(deep_items), now,
        )
        return state

    from wren_agent.engine import (
        _task_pauses, update_task_pg_status, wait_for_resume,
    )
    from wren_agent.tools.event_emitter import emit_interrupt
    from wren_agent.tools.pg_store import pause_task

    await update_task_pg_status(
        state["task_id"], "awaiting_approval", "schema_explore_deep",
    )
    await pause_task(state["task_id"], "deep_explore")
    pause_ev = asyncio.Event()
    _task_pauses[state["task_id"]] = pause_ev

    emit_interrupt(state, "approval", {
        "items_count": 1,
        "items": [{
            "id": approval_id,
            "title": "深度探索查询授权",
            "artifact_type": "deep_explore",
        }],
    })

    decision = await _load_deep_explore_decision(state["task_id"], approval_id)
    payload = {} if decision else await wait_for_resume(
        state["task_id"], pause_ev, timeout=1800,
    )
    if decision is None and payload.get("aid") == approval_id:
        decision = "approved" if payload.get("approved", False) else "rejected"
    if decision is None:
        decision = "timeout"

    await _finish_deep_explore_gate(
        state, approval_id, decision, len(deep_items), now,
    )
    return state


def _apply_deep_explore_decision(
    state: AgentState,
    decision: str,
    items_count: int,
    now: datetime,
) -> None:
    """Apply a deep-query decision separately from final design approval."""
    approved = decision == "approved"
    if not approved:
        state["deep_explore_pending"] = []
        if decision == "timeout":
            state["warnings"] = state.get("warnings", []) + [
                "Deep exploration authorization timed out; expensive queries were skipped"
            ]
    state["deep_explore_decision"] = decision
    state["pending_human_input"] = False
    state["human_input_type"] = ""
    state["audit_log"] = state.get("audit_log", []) + [{
        "phase": "schema_explore_deep",
        "action": "authorized" if approved else "skipped",
        "decision": decision,
        "items_count": items_count,
        "timestamp": now.isoformat(),
    }]


async def _finish_deep_explore_gate(
    state: AgentState,
    approval_id: str,
    decision: str,
    items_count: int,
    now: datetime,
) -> None:
    """Apply the decision and clear pause/status state on every exit path."""
    _apply_deep_explore_decision(state, decision, items_count, now)

    from wren_agent.engine import (
        _task_pauses, persist_approval_response, update_task_pg_status,
    )
    from wren_agent.tools.pg_store import clear_task_pause

    if decision == "timeout":
        await persist_approval_response(
            state["task_id"], approval_id, False,
            "[timeout] 深度探索授权超时，已跳过查询",
        )

    _task_pauses.pop(state["task_id"], None)
    try:
        await clear_task_pause(state["task_id"])
    except Exception:
        logger.debug("clear deep-explore pause failed", exc_info=True)
    await update_task_pg_status(
        state["task_id"], "exploring", "schema_explore_deep",
    )


async def _persist_deep_explore_approval(
    task_id: str,
    approval_id: str,
    title: str,
    description: str,
) -> None:
    """Expose the gate through the existing approvals API."""
    from wren_agent.tools.pg_store import get_pg_pool

    pool = get_pg_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO task_approvals "
            "(id, task_id, title, description, artifact_type, status, created_at) "
            "VALUES ($1, $2, $3, $4, 'deep_explore', 'pending', NOW()) "
            "ON CONFLICT (id) DO NOTHING",
            approval_id, task_id, title, description,
        )


async def _load_deep_explore_decision(
    task_id: str,
    approval_id: str,
) -> str | None:
    """Load a decision persisted by this or another Agent process."""
    try:
        from wren_agent.tools.pg_store import get_pg_pool

        pool = get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status FROM task_approvals WHERE id=$1 AND task_id=$2",
                approval_id, task_id,
            )
        if row and row["status"] in ("approved", "rejected"):
            return str(row["status"])
    except Exception:
        logger.debug("deep-explore decision lookup failed", exc_info=True)
    return None


def _filter_relevant_tables(tables: list[str], goal: str) -> list[str]:
    """Filter tables to those relevant to the modeling goal.

    Uses keyword matching — the goal is Chinese, table names are English.
    Common financial table patterns are prioritized.
    """
    goal_lower = goal.lower()

    # Priority keywords → table name patterns
    keyword_map = {
        "银行": ["bank", "flow", "financial"],
        "资金": ["flow", "financial", "fund", "fincancial", "capital", "bank"],
        "流水": ["flow"],
        "融资": ["financ", "loan", "debt", "bond", "lend"],
        "日报": ["daily", "report"],
        "月报": ["month", "annual", "report"],
        "日": ["daily"],
        "资产": ["asset", "order", "product"],
        "投资": ["invest"],
        "债务": ["debt", "liability"],
        "合同": ["contract"],
        "车辆": ["car", "vehicle"],
        "开户": ["account", "open"],
        "交易": ["trans", "trade", "deal"],
        "金融": ["financial", "fincancial", "financ"],
        "银行流水": ["flow", "bank"],
        "资金流水": ["flow", "financial"],
        # Property domain (wayfinder: dwd_ecommerce_orders star schema —
        # fct_ facts / dim_ dimensions, English table names)
        "订单": ["order"],
        "商品": ["product", "sku", "category"],
        "房屋": ["store", "product", "shop", "order"],
        "楼栋": ["shop", "floor", "unit", "store"],
        "单元": ["unit", "product", "store"],
        "出租": ["payment", "lease", "contract", "outbound", "out_lease"],
        "租金": ["payment", "lease", "contract", "fee", "amount"],
        "车位": ["parking", "park", "car_space", "carspace", "space", "garage"],
        "停车": ["parking", "park", "car_space", "carspace", "garage"],
        "管理费": ["fee", "receivable", "receipt", "payment", "cycle", "order"],
        "欠费": ["arrears", "overdue", "unpaid", "receivable", "debt", "cycle"],
        "支付": ["receivable", "receipt", "payment", "paid", "collection", "cycle"],
        "面积": ["area", "acreage", "square"],
        "租约": ["contract", "lease"],
        "入住": ["live", "occup", "checkin", "movein"],
    }

    # Collect relevant keyword stems
    relevant_stems = set()
    for kw, stems in keyword_map.items():
        if kw in goal_lower:
            relevant_stems.update(stems)

    # Also include general financial tables
    general_stems = ["financial", "fincancial", "financ", "fund", "capital",
                     "flow", "account", "bank", "debt", "invest", "asset"]

    if not relevant_stems:
        relevant_stems = set(general_stems[:5])
    else:
        relevant_stems = relevant_stems | set(general_stems[:3])

    # Score each table
    scored = []
    for table in tables:
        t = table.lower()
        score = 0
        for stem in relevant_stems:
            if stem in t:
                score += 10  # Exact match
            elif any(stem.startswith(p) for p in [t[:4], t[:5]]):
                score += 2   # Prefix match

        # Prefer non-copy tables (no copy1, copy2 suffixes)
        if "copy" in t:
            score = max(0, score - 8)
        # Prefer tables with meaningful names (not test, _out, mongodb_replace)
        if t in ("test", "public_field", "public_field_copy1", "public_field_copy2",
                 "ecommerce_mongodb_replace", "ecommerce_heartbeat"):
            score = 0

        if score > 0:
            scored.append((table, score))

    scored.sort(key=lambda x: (-x[1], x[0]))
    result = [s[0] for s in scored]

    # Always include bank flow tables if goal is about flow
    transaction_flow_tables = [t for t in tables if
                        (("bank" in t.lower() or "flow" in t.lower()) and
                         "financial" in t.lower())]
    for bt in transaction_flow_tables:
        if bt not in result:
            result.append(bt)

    # Empty means "no vocabulary hit". Callers decide whether to fall back to
    # the full table list — do not hide that choice here.
    return result


async def _extract_db_params(source_db: Any) -> dict:
    """Extract connection params from a SourceDatabase.

    SourceDatabase is a metadata-only model (no credentials). When user/password
    are missing, we look them up from the source_db_registry PG table where they
    are stored encrypted (SM4) — first by id, then by name as fallback.

    The returned dict carries a `credential_source` key so callers can refuse
    to connect with empty credentials instead of letting the driver fall back
    to the OS username:
      inline       — credentials came with the source_db payload
      registry     — resolved from source_db_registry
      missing      — not registered (or registered with empty credentials)
      lookup_error — registry lookup raised (PG unavailable, etc.)
    """
    if hasattr(source_db, "model_dump"):
        sdb = source_db.model_dump()
    elif isinstance(source_db, dict):
        sdb = source_db
    else:
        sdb = source_db.__dict__

    db_id = sdb.get("id") or sdb.get("name", "unknown")
    db_name = sdb.get("name", "")
    user = sdb.get("user", "")
    password = sdb.get("password", "")
    connection_params = sdb.get("connection_params") or {}
    credential_source = "inline"

    # SourceDatabase model has no user/password — look up from registry
    if not user and not password:
        credential_source = "missing"
        try:
            from wren_agent.tools.pg_store import get_source_db_credentials
            creds = await get_source_db_credentials(db_id)
            if not creds and db_name and db_name != db_id:
                # id may be an ad-hoc uuid never registered — retry by name
                creds = await get_source_db_credentials(db_name)
            if creds and (creds["user"] or creds["password"]):
                user = creds["user"]
                password = creds["password"]
                credential_source = "registry"
                # Registry-level driver options (SSL toggle etc.) come along
                if not connection_params:
                    connection_params = creds.get("connection_params") or {}
        except Exception:
            credential_source = "lookup_error"
            logger.warning(
                "Credential lookup failed for source DB '%s' (name='%s')",
                db_id, db_name, exc_info=True,
            )

    return {
        "db_id": db_id,
        "name": db_name,
        "db_type": sdb.get("db_type", "mysql"),
        "host": sdb.get("host", ""),
        "port": sdb.get("port", 3306),
        "user": user,
        "password": password,
        "database": sdb.get("database", ""),
        "credential_source": credential_source,
        "connection_params": connection_params,
    }
