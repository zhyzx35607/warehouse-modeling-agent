"""Neo4j GraphStore — KG persistence layer with real Cypher queries.

Uses Neo4j 4.4 Community (sync driver, thread-safe via asyncio.to_thread).
Stores nodes with UUID-based identity for stable cross-referencing.

Architecture: ADR-0005 (Neo4j as dedicated graph store)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from wren_agent.config import settings

logger = logging.getLogger(__name__)

# ── Node & Relationship labels ──────────────────────────────────


class KGNodeType:
    BUSINESS_CONCEPT = "BusinessConcept"
    SOURCE_DATABASE = "SourceDatabase"
    SOURCE_TABLE = "SourceTable"
    SOURCE_FIELD = "SourceField"
    DIM_TABLE = "DimTable"
    FACT_TABLE = "FactTable"
    DW_TABLE = "DWTable"
    METRIC = "Metric"
    DAG_TASK = "DAGTask"
    CLARIFICATION = "ClarificationItem"
    MODELING_TASK = "ModelingTask"

    # Additional labels for query purposes
    ALL_TABLE_TYPES = [SOURCE_TABLE, DIM_TABLE, FACT_TABLE, DW_TABLE]


class KGRelationshipType:
    BELONGS_TO = "BELONGS_TO"
    HAS_FIELD = "HAS_FIELD"
    JOINS_ON = "JOINS_ON"
    SOURCED_FROM = "SOURCED_FROM"
    AGGREGATES_FROM = "AGGREGATES_FROM"
    COMPUTES_METRIC = "COMPUTES_METRIC"
    REFERENCES_DIM = "REFERENCES_DIM"
    DEPENDS_ON = "DEPENDS_ON"
    SAME_AS = "SAME_AS"
    VARIANT_OF = "VARIANT_OF"
    CLARIFIES = "CLARIFIES"
    PRODUCED_BY = "PRODUCED_BY"


class GraphStore:
    """GraphStore backed by Neo4j 4.4 Community.

    Uses sync driver wrapped in asyncio.to_thread for thread safety.
    All nodes have a 'uuid' order for stable external references.
    """

    def __init__(self, uri: str | None = None, user: str | None = None, password: str | None = None):
        self._uri = uri or settings.neo4j.uri
        self._user = user or settings.neo4j.user
        self._password = password or settings.neo4j.password
        self._driver = None
        self._loop = None

    # ── Driver lifecycle ────────────────────────────────────

    def _ensure_driver(self):
        """Lazy-init sync driver."""
        if self._driver is None:
            from neo4j import GraphDatabase
            self._driver = GraphDatabase.driver(
                self._uri,
                auth=(self._user, self._password),
                max_connection_lifetime=settings.neo4j.max_connection_lifetime,
                max_connection_pool_size=settings.neo4j.max_connection_pool_size,
            )
            logger.info(f"Neo4j driver connected to {self._uri}")
        return self._driver

    def _session(self):
        return self._ensure_driver().session(database=settings.neo4j.database)

    async def close(self):
        if self._driver:
            self._driver.close()
            self._driver = None
            logger.info("Neo4j driver closed")

    async def _run_async(self, fn, *args, **kwargs):
        """Run a sync operation in a thread."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    # ── Node operations ─────────────────────────────────────

    async def create_node(
        self,
        node_type: str,
        properties: dict[str, Any],
        labels: list[str] | None = None,
    ) -> str:
        """Create or merge a KG node. Returns the node UUID.

        Uses MERGE on uuid to make the operation idempotent.
        """
        node_uuid = properties.get("uuid") or str(uuid.uuid4())
        all_labels = [node_type] + (labels or [])
        label_str = ":".join(all_labels)

        props = {**properties, "uuid": node_uuid}
        if "created_at" not in props:
            props["created_at"] = datetime.now(timezone.utc).isoformat()

        def _create():
            with self._session() as session:
                result = session.run(
                    f"MERGE (n:{label_str} {{uuid: $uuid}}) "
                    "SET n += $props "
                    "RETURN n.uuid as node_uuid, id(n) as internal_id",
                    uuid=node_uuid, props=props,
                )
                record = result.single()
                if record:
                    logger.debug(f"Created/updated node [{node_type}]: {node_uuid} (internal: {record['internal_id']})")
                return record["node_uuid"] if record else node_uuid

        return await self._run_async(_create)

    async def create_relationship(
        self,
        from_uuid: str,
        to_uuid: str,
        rel_type: str,
        properties: dict[str, Any] | None = None,
    ) -> bool:
        """Create a relationship between two nodes (matched by uuid).

        Uses MERGE to avoid duplicate relationships.
        """
        props = properties or {}
        if "created_at" not in props:
            props["created_at"] = datetime.now(timezone.utc).isoformat()

        def _create():
            with self._session() as session:
                result = session.run(
                    "MATCH (a {uuid: $from_uuid}) "
                    "MATCH (b {uuid: $to_uuid}) "
                    f"MERGE (a)-[r:{rel_type}]->(b) "
                    "SET r += $props "
                    "RETURN type(r) as rel_type, a.uuid as from_node, b.uuid as to_node",
                    from_uuid=from_uuid, to_uuid=to_uuid, props=props,
                )
                record = result.single()
                if record:
                    logger.debug(f"Created relationship: {record['from_node']} -[{rel_type}]-> {record['to_node']}")
                return record is not None

        return await self._run_async(_create)

    async def get_node(self, node_uuid: str) -> dict[str, Any] | None:
        """Get a node by UUID."""
        def _get():
            with self._session() as session:
                result = session.run(
                    "MATCH (n {uuid: $uuid}) "
                    "RETURN n, labels(n) as labels",
                    uuid=node_uuid,
                )
                record = result.single()
                if record:
                    node = dict(record["n"])
                    node["labels"] = record["labels"]
                    node["internal_id"] = record["n"].id
                    return node
                return None

        return await self._run_async(_get)

    async def get_nodes_by_type(
        self,
        node_type: str,
        filters: dict[str, Any] | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Get nodes of a given type, optionally filtered."""
        def _get():
            where_clauses = []
            params: dict = {"limit": limit}

            if filters:
                for i, (k, v) in enumerate(filters.items()):
                    pname = f"filter_{i}"
                    where_clauses.append(f"n.{k} = ${pname}")
                    params[pname] = v

            where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

            with self._session() as session:
                result = session.run(
                    f"MATCH (n:{node_type}) {where} "
                    "RETURN n, labels(n) as labels "
                    "LIMIT $limit",
                    **params,
                )
                nodes = []
                for record in result:
                    node = dict(record["n"])
                    node["labels"] = record["labels"]
                    nodes.append(node)
                return nodes

        return await self._run_async(_get)

    # ── Lineage ─────────────────────────────────────────────

    async def get_lineage(
        self,
        start_uuid: str,
        direction: str = "downstream",
        max_depth: int = 10,
    ) -> list[dict]:
        """Trace data lineage from a node.

        direction: 'downstream' (data flows away from start) | 'upstream' (toward start) | 'both'
        Uses undirected matching + relationship type filtering for Neo4j 4.4 compat.
        """
        lineage_rel_types = [
            "SOURCED_FROM", "AGGREGATES_FROM", "DEPENDS_ON",
            "REFERENCES_DIM", "COMPUTES_METRIC", "BELONGS_TO",
        ]

        def _trace():
            with self._session() as session:
                result = session.run(
                    f"MATCH path = (start {{uuid: $uuid}})-[r*1..{max_depth}]-(end) "
                    "WHERE all(rel IN relationships(path) WHERE type(rel) IN $rel_types) "
                    "RETURN path, nodes(path) as path_nodes, relationships(path) as path_rels, "
                    "length(path) as depth "
                    "ORDER BY depth",
                    uuid=start_uuid,
                    rel_types=lineage_rel_types,
                )
                paths = []
                for record in result:
                    path_data = {
                        "depth": record["depth"],
                        "nodes": [],
                        "relationships": [],
                    }
                    for node in record["path"].nodes:
                        nd = dict(node)
                        nd["labels"] = list(node.labels)
                        path_data["nodes"].append(nd)
                    for rel in record["path"].relationships:
                        path_data["relationships"].append({
                            "type": rel.type,
                            "properties": dict(rel),
                        })
                    paths.append(path_data)
                return paths

        return await self._run_async(_trace)

    async def get_impact_analysis(
        self,
        dim_table: str,
    ) -> list[dict]:
        """Find all downstream artifacts affected by a DIM change.

        Traces DIM → DWD → DWS → ADS → Metric dependency chains.
        The chain follows INCOMING relationships:
          DIM ←[REFERENCES_DIM]— DWD ←[AGGREGATES_FROM]— DWS ←[DEPENDS_ON]— ADS ←[COMPUTES_METRIC]— Metric
        """
        def _analyze():
            with self._session() as session:
                result = session.run(
                    "MATCH (dim:DimTable {dim_table: $dim_table}) "
                    "OPTIONAL MATCH path = (dim)<-[:REFERENCES_DIM|AGGREGATES_FROM|DEPENDS_ON|COMPUTES_METRIC*1..5]-(downstream) "
                    "RETURN dim, collect(distinct {"
                    "  type: labels(downstream)[0], "
                    "  uuid: downstream.uuid, "
                    "  name: coalesce(downstream.dim_table, downstream.fact_table, "
                    "    downstream.summary_table, downstream.ads_table, "
                    "    downstream.metric_name, downstream.name, downstream.table_name), "
                    "  distance: length(path)"
                    "}) as affected",
                    dim_table=dim_table,
                )
                record = result.single()
                if record and record["affected"]:
                    affected = record["affected"]
                    return {
                        "dim_table": dim_table,
                        "affected_count": len(affected),
                        "affected_artifacts": sorted(affected, key=lambda x: x.get("distance") or 0),
                    }
                return {"dim_table": dim_table, "affected_count": 0, "affected_artifacts": []}

        return await self._run_async(_analyze)

    # ── Similarity & matching ───────────────────────────────

    async def find_field_semantics(
        self,
        field_names: list[str],
        table_prefixes: list[str] | None = None,
        db_names: list[str] | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """KG fallback lookup: SourceField nodes for the given field names.

        Used when the PG semantic dictionary misses — grill/design adopt
        meanings harvested by earlier tasks. Nodes from tables sharing a
        layer prefix with the target (e.g. dws_*) rank first; deduplicated
        client-side by (field_name, chinese_meaning).

        `db_names` scopes the lookup to the current task's relevant databases
        when provided. Legacy nodes (db_name '' / null / '-') stay visible so
        pre-db-scope data is never hidden by the filter.
        """
        def _find():
            with self._session() as session:
                if not field_names:
                    return []
                prefixes = [p for p in (table_prefixes or []) if p]
                prefix_clause = (
                    "CASE WHEN any(p IN $prefixes WHERE n.table_name STARTS WITH p) "
                    "THEN 0 ELSE 1 END AS rank"
                    if prefixes else "0 AS rank"
                )
                db_clause = ""
                db_params: dict = {}
                if db_names:
                    db_clause = (" AND (n.db_name IN $db_names "
                                 "OR n.db_name IN ['', '-'] OR n.db_name IS NULL) ")
                    db_params["db_names"] = list(db_names)
                result = session.run(
                    "MATCH (n:SourceField) "
                    "WHERE n.field_name IN $fields AND n.chinese_meaning <> '' "
                    f"{db_clause}"
                    f"WITH n, {prefix_clause} "
                    "ORDER BY rank, n.table_name "
                    "RETURN n.table_name AS table_name, n.field_name AS field_name, "
                    "       n.chinese_meaning AS chinese_meaning, "
                    "       n.field_role AS field_role, n.enum_values AS enum_values "
                    "LIMIT $limit",
                    fields=list(field_names),
                    prefixes=prefixes,
                    **db_params,
                    limit=limit,
                )
                return [dict(r) for r in result]

        return await self._run_async(_find)

    async def find_similar_nodes(
        self,
        node_type: str,
        properties: dict[str, Any],
        limit: int = 5,
    ) -> list[dict]:
        """Find nodes similar to given properties.

        Matches on table name patterns and shared properties.
        Used for blueprint matching and dimension reuse detection.
        """
        def _find():
            with self._session() as session:
                # Build a flexible match query
                # Match on name-like properties with partial string matching
                name_props = [v for k, v in properties.items()
                              if isinstance(v, str) and len(v) > 2
                              and k in ("name", "dim_table", "fact_table", "table_name", "dim_name")]

                if not name_props:
                    return []

                # Use CONTAINS for substring matching (Neo4j 4.4 syntax)
                where_parts = []
                params: dict = {"limit": limit, "node_type": node_type}
                for i, name_val in enumerate(name_props):
                    pname = f"q{i}"
                    where_parts.append(f"n.uuid CONTAINS ${pname} OR n.name CONTAINS ${pname} OR n.dim_table CONTAINS ${pname} OR n.fact_table CONTAINS ${pname}")
                    # Extract core word for matching
                    core = name_val.lower().replace("_", "").replace("dim_", "").replace("fact_", "").replace("ods_", "")
                    params[pname] = core

                where = " OR ".join(where_parts)

                result = session.run(
                    f"MATCH (n:{node_type}) "
                    f"WHERE {where} "
                    "RETURN n, labels(n) as labels "
                    "LIMIT $limit",
                    **params,
                )
                nodes = []
                for record in result:
                    node = dict(record["n"])
                    node["labels"] = record["labels"]
                    nodes.append(node)
                return nodes

        return await self._run_async(_find)

    # ── Export ──────────────────────────────────────────────

    async def export_subgraph(self, task_id: str) -> dict:
        """Export the complete graph for a ModelingTask as a data asset map.

        Collects all nodes and relationships linked to a task_id,
        returns a structured JSON suitable for visualization.
        """
        def _export():
            with self._session() as session:
                # Get all nodes linked to this task
                result = session.run(
                    "MATCH (task:ModelingTask {uuid: $task_id}) "
                    "OPTIONAL MATCH (task)-[:BELONGS_TO*0..]-(related) "
                    "OPTIONAL MATCH (related)-[r]-(other) "
                    "WHERE other:SourceTable OR other:DWTable OR other:DimTable "
                    "   OR other:FactTable OR other:Metric OR other:DAGTask "
                    "RETURN task, collect(distinct related) + collect(distinct other) as nodes, "
                    "       collect(distinct {type: type(r), from: startNode(r).uuid, to: endNode(r).uuid}) as edges",
                    task_id=task_id,
                )
                record = result.single()
                if not record:
                    return {"task_id": task_id, "nodes": [], "edges": []}

                nodes = []
                seen = set()

                # Task node
                task_node = dict(record["task"])
                task_node["labels"] = list(record["task"].labels)
                nodes.append(task_node)
                seen.add(task_node["uuid"])

                # Related nodes
                for n in record["nodes"]:
                    nd = dict(n)
                    if nd.get("uuid") not in seen:
                        nd["labels"] = list(n.labels)
                        nodes.append(nd)
                        seen.add(nd["uuid"])

                edges = []
                for e in record["edges"]:
                    if e and e.get("from") and e.get("to"):
                        edges.append(e)

                # Summary counts
                type_counts = {}
                for n in nodes:
                    for lbl in n.get("labels", []):
                        type_counts[lbl] = type_counts.get(lbl, 0) + 1

                return {
                    "task_id": task_id,
                    "total_nodes": len(nodes),
                    "total_edges": len(edges),
                    "node_types": type_counts,
                    "layers": {
                        "source": [n for n in nodes if "SourceTable" in n.get("labels", [])],
                        "ods": [n for n in nodes if "DWTable" in n.get("labels", []) and n.get("layer") == "ODS"],
                        "dim": [n for n in nodes if "DimTable" in n.get("labels", [])],
                        "dwd": [n for n in nodes if "FactTable" in n.get("labels", []) or (n.get("layer") == "DWD")],
                        "dws": [n for n in nodes if n.get("layer") == "DWS"],
                        "ads": [n for n in nodes if n.get("layer") == "ADS"],
                        "metrics": [n for n in nodes if "Metric" in n.get("labels", [])],
                    },
                    "edges": edges,
                }

        return await self._run_async(_export)

    # ── Bulk operations ─────────────────────────────────────

    async def create_modeling_task_subgraph(
        self,
        task_id: str,
        design_plan: Any,  # DWDesignPlan
        runtime_info: dict[str, Any] | None = None,
    ) -> str:
        """Create the complete KG subgraph for a modeling task.

        This is called from the export_kg LangGraph node.
        Creates all nodes (sources, ODS, DIM, DWD, DWS, ADS, metrics, DAGs)
        and relationships (lineage, dependencies, dimension references).

        When runtime_info is provided, also creates:
        - Execution result nodes with success/failure status
        - Quality check nodes (row counts, NULL ratios, etc.)
        - Schema drift nodes (breaking/additive changes)
        - Clarification answer nodes linked to resolved concepts
        """
        import json

        task_uuid = await self.create_node(KGNodeType.MODELING_TASK, {
            "uuid": task_id,
            "name": f"ModelingTask_{task_id[:8]}",
            "modeling_goal": getattr(design_plan, "modeling_goal", ""),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })

        source_db_uuids: dict[str, str] = {}
        table_uuids: dict[str, dict[str, str]] = {}  # layer -> {table_name: uuid}

        # ── ODS tables ───────────────────────────────────────
        for ods in (getattr(design_plan, "ods_designs", None) or []):
            src_db_id = getattr(ods, "source_db_id", "unknown")

            # Source DB node
            if src_db_id not in source_db_uuids:
                sdb_uuid = f"src_db_{src_db_id}"
                await self.create_node(KGNodeType.SOURCE_DATABASE, {
                    "uuid": sdb_uuid,
                    "name": src_db_id,
                    "comment": "源数据库",
                })
                await self.create_relationship(task_uuid, sdb_uuid, KGRelationshipType.BELONGS_TO)
                source_db_uuids[src_db_id] = sdb_uuid

            # Source table node
            src_table_uuid = f"src_table_{ods.source_table}"
            await self.create_node(KGNodeType.SOURCE_TABLE, {
                "uuid": src_table_uuid,
                "name": ods.source_table,
                "table_name": ods.source_table,
                "db_id": src_db_id,
                "comment": "源数据表",
            })
            await self.create_relationship(src_table_uuid, source_db_uuids[src_db_id], KGRelationshipType.BELONGS_TO)

            # ODS table node
            ods_uuid = f"ods_{ods.ods_table}"
            await self.create_node(KGNodeType.DW_TABLE, {
                "uuid": ods_uuid,
                "name": ods.ods_table,
                "table_name": ods.ods_table,
                "layer": "ODS",
                "sync_method": getattr(ods, "sync_method", "datax"),
                "comment": "ODS 原始数据层表",
            }, labels=["DWTable"])
            await self.create_relationship(ods_uuid, src_table_uuid, KGRelationshipType.SOURCED_FROM)
            await self.create_relationship(ods_uuid, task_uuid, KGRelationshipType.BELONGS_TO)
            table_uuids.setdefault("ODS", {})[ods.ods_table] = ods_uuid

        # ── DIM tables ───────────────────────────────────────
        for dim in (getattr(design_plan, "dim_designs", None) or []):
            dim_uuid = f"dim_{dim.dim_table}"
            await self.create_node(KGNodeType.DIM_TABLE, {
                "uuid": dim_uuid,
                "name": dim.dim_name,
                "dim_name": dim.dim_name,
                "dim_table": dim.dim_table,
                "layer": "DIM",
                "natural_keys": getattr(dim, "natural_key", []),
                "source": getattr(dim, "source", ""),
                "variant_of": getattr(dim, "dim_variant_of", None),
                "version": getattr(dim, "version", 1),
                "comment": "公共维度表",
            }, labels=["DimTable", "DWTable"])
            await self.create_relationship(dim_uuid, task_uuid, KGRelationshipType.BELONGS_TO)

            # Source from ODS table if applicable
            src_table = getattr(dim, "source_table", None)
            if src_table and "ODS" in table_uuids:
                ods_uuid = table_uuids["ODS"].get(f"ods_{src_table}")
                if ods_uuid:
                    await self.create_relationship(dim_uuid, ods_uuid, KGRelationshipType.SOURCED_FROM)

            # Variant relationship — stores variant_of name as order on self-loop
            variant_of = getattr(dim, "dim_variant_of", None)
            if variant_of:
                # Create a relationship to self with variant metadata
                # (Neo4j 4.4 workaround: store variant info on node properties)
                await self.create_node(KGNodeType.DIM_TABLE, {
                    "uuid": dim_uuid,
                    "variant_of": variant_of,
                })

            table_uuids.setdefault("DIM", {})[dim.dim_table] = dim_uuid

        # ── DWD tables ───────────────────────────────────────
        for dwd in (getattr(design_plan, "dwd_designs", None) or []):
            dwd_uuid = f"dwd_{dwd.fact_table}"
            await self.create_node(KGNodeType.FACT_TABLE, {
                "uuid": dwd_uuid,
                "name": dwd.fact_name,
                "fact_table": dwd.fact_table,
                "layer": "DWD",
                "grain": getattr(dwd, "grain", []),
                "business_process": getattr(dwd, "business_process", ""),
                "comment": "明细事实层表",
            }, labels=["FactTable", "DWTable"])
            await self.create_relationship(dwd_uuid, task_uuid, KGRelationshipType.BELONGS_TO)

            # Dependencies: ODS source tables
            for src_table in getattr(dwd, "source_ods_tables", []):
                if "ODS" in table_uuids and src_table in table_uuids["ODS"]:
                    await self.create_relationship(dwd_uuid, table_uuids["ODS"][src_table], KGRelationshipType.SOURCED_FROM)

            # Dimension references
            for dim_ref in getattr(dwd, "dimension_refs", []):
                await self.create_relationship(dwd_uuid, f"dim_{dim_ref}", KGRelationshipType.REFERENCES_DIM,
                    {"referenced_dim": dim_ref})

            table_uuids.setdefault("DWD", {})[dwd.fact_table] = dwd_uuid

        # ── DWS tables ───────────────────────────────────────
        for dws in (getattr(design_plan, "dws_designs", None) or []):
            dws_uuid = f"dws_{dws.summary_table}"
            await self.create_node(KGNodeType.DW_TABLE, {
                "uuid": dws_uuid,
                # The node name must be the table name — a Chinese summary as
                # the name made the lineage graph unreadable (task d580a14f).
                "name": dws.summary_table,
                "summary_name": getattr(dws, "summary_name", ""),
                "table_name": dws.summary_table,
                "layer": "DWS",
                "grain": getattr(dws, "grain", []),
                "refresh_logic": getattr(dws, "refresh_logic", "incremental"),
                "comment": "公共汇总层表",
            }, labels=["DWTable"])
            await self.create_relationship(dws_uuid, task_uuid, KGRelationshipType.BELONGS_TO)

            # Sources: business mode aggregates plan-internal DWD designs;
            # warehouse mode anchors on EXISTING warehouse tables via
            # `source_tables` (possibly db-qualified) — previously only
            # `source_dwd_table` was consulted, and only when the DWD was
            # created by this plan, so warehouse lineage had no edges at all.
            dws_sources = list(getattr(dws, "source_tables", None) or [])
            src_dwd = getattr(dws, "source_dwd_table", "")
            if src_dwd and src_dwd not in dws_sources:
                dws_sources.append(src_dwd)
            for src in dws_sources:
                bare = src.split(".")[-1]
                plan_dwd = table_uuids.get("DWD", {}).get(bare)
                if plan_dwd is not None:
                    await self.create_relationship(
                        dws_uuid, plan_dwd, KGRelationshipType.AGGREGATES_FROM)
                    continue
                src_uuid = f"src_table_{src}"
                await self.create_node(KGNodeType.SOURCE_TABLE, {
                    "uuid": src_uuid,
                    "name": src,
                    "table_name": bare,
                    "comment": "数仓既有来源表",
                })
                await self.create_relationship(
                    dws_uuid, src_uuid, KGRelationshipType.SOURCED_FROM)

            table_uuids.setdefault("DWS", {})[dws.summary_table] = dws_uuid

        # ── ADS tables ───────────────────────────────────────
        for ads in (getattr(design_plan, "ads_designs", None) or []):
            ads_uuid = f"ads_{ads.ads_table}"
            await self.create_node(KGNodeType.DW_TABLE, {
                "uuid": ads_uuid,
                # Table name as the node name — application_name can be a
                # meaningless fallback like "Modeling Result".
                "name": ads.ads_table,
                "application_name": getattr(ads, "application_name", ""),
                "table_name": ads.ads_table,
                "layer": "ADS",
                "target_audience": getattr(ads, "target_audience", ""),
                "comment": "应用数据层表",
            }, labels=["DWTable"])
            await self.create_relationship(ads_uuid, task_uuid, KGRelationshipType.BELONGS_TO)

            # ADS → DWS dependency edges (deduped). Previously only created
            # via metrics, so a metric-less ADS had NO lineage at all, and
            # warehouse-mode ADS anchored on EXISTING DWS tables (not in this
            # plan) was silently skipped.
            seen_dws_srcs: set[str] = set()
            for dws_table in getattr(ads, "source_dws_tables", []):
                bare = dws_table.split(".")[-1]
                if bare in seen_dws_srcs:
                    continue
                seen_dws_srcs.add(bare)
                target = table_uuids.get("DWS", {}).get(bare)
                if target is None:
                    target = f"dws_{bare}"
                    await self.create_node(KGNodeType.DW_TABLE, {
                        "uuid": target,
                        "name": bare,
                        "table_name": bare,
                        "layer": "DWS",
                        "comment": "数仓既有汇总表",
                    }, labels=["DWTable"])
                await self.create_relationship(
                    ads_uuid, target, KGRelationshipType.DEPENDS_ON)

            # Metrics
            metrics = getattr(ads, "metrics", []) or []
            for metric in metrics:
                metric_name = metric.get("name", "unknown_metric") if isinstance(metric, dict) else str(metric)
                metric_uuid = f"metric_{metric_name}"
                await self.create_node(KGNodeType.METRIC, {
                    "uuid": metric_uuid,
                    "name": metric_name,
                    "metric_name": metric_name,
                    "expression": metric.get("expression", "") if isinstance(metric, dict) else "",
                    "comment": "业务指标",
                })
                await self.create_relationship(ads_uuid, metric_uuid, KGRelationshipType.COMPUTES_METRIC)

                # Metric → DWS dependency
                for dws_table in getattr(ads, "source_dws_tables", []):
                    bare = dws_table.split(".")[-1]
                    if "DWS" in table_uuids and bare in table_uuids["DWS"]:
                        await self.create_relationship(metric_uuid, table_uuids["DWS"][bare], KGRelationshipType.DEPENDS_ON)

            table_uuids.setdefault("ADS", {})[ads.ads_table] = ads_uuid

        # ── Field semantics (B-side): mirror the semantic dictionary as
        # SourceField nodes + HAS_FIELD edges on the task's tables.
        # Business mode attaches to existing src_table nodes; warehouse mode
        # creates SourceTable nodes for the explored existing tables, which
        # also closes the gap where existing warehouse tables had no KG node.
        from wren_agent.tools.pg_store import query_field_semantics

        explored_names = [
            t.name
            for exp in (runtime_info or {}).get("explorations", {}).values()
            for t in getattr(exp, "tables", []) or []
        ]
        ods_sources = [
            getattr(ods, "source_table", "")
            for ods in (getattr(design_plan, "ods_designs", None) or [])
        ]
        semantics_rows = await query_field_semantics(explored_names + ods_sources) \
            if (explored_names or ods_sources) else []
        created_src: set[str] = set()
        for row in semantics_rows:
            table_name = row["table_name"]
            if not table_name:
                continue
            db_name = row.get("db_name") or ""
            src_uuid = f"src_table_{table_name}"
            if src_uuid not in created_src:
                created_src.add(src_uuid)
                await self.create_node(KGNodeType.SOURCE_TABLE, {
                    "uuid": src_uuid,
                    "name": table_name,
                    "table_name": table_name,
                    "db_id": "semantics",
                    "db_name": db_name,
                    "comment": "字段语义字典来源表",
                }, labels=["SourceTable"])
            field_uuid = f"field_{table_name}.{row['field_name']}"
            await self.create_node(KGNodeType.SOURCE_FIELD, {
                "uuid": field_uuid,
                "name": row["field_name"],
                "field_name": row["field_name"],
                "table_name": table_name,
                "db_name": db_name,
                "chinese_meaning": row.get("chinese_meaning", ""),
                "field_role": row.get("field_role", ""),
                "enum_values": row.get("enum_values") or [],
                "comment": "字段语义字典",
            }, labels=["SourceField"])
            await self.create_relationship(src_uuid, field_uuid, KGRelationshipType.HAS_FIELD)

        # ── Runtime Information (execution, quality, drifts, clarifications) ──
        if runtime_info:
            await self._add_runtime_nodes(task_uuid, table_uuids, runtime_info)

        logger.info(f"KG subgraph created for task {task_id}: "
                     f"ODS={len(table_uuids.get('ODS',{}))}, "
                     f"DIM={len(table_uuids.get('DIM',{}))}, "
                     f"DWD={len(table_uuids.get('DWD',{}))}, "
                     f"DWS={len(table_uuids.get('DWS',{}))}, "
                     f"ADS={len(table_uuids.get('ADS',{}))}")

        return task_uuid

    async def _add_runtime_nodes(
        self,
        task_uuid: str,
        table_uuids: dict[str, dict[str, str]],
        runtime_info: dict[str, Any],
    ) -> None:
        """Add runtime-enriched nodes to the KG: execution results, quality
        checks, schema drifts, and clarification answers.

        These complement the static design nodes with actual runtime outcomes,
        making the KG a complete traceability graph (design → execute → verify).
        """
        now = datetime.now(timezone.utc).isoformat()

        # ── Execution Results ──────────────────────────────────
        exec_results = runtime_info.get("execution_results", []) or []
        for er in exec_results:
            success = getattr(er, "success", er.get("success", False)) if not isinstance(er, dict) else er.get("success", False)
            instance_id = getattr(er, "task_instance_id", er.get("task_instance_id", "unknown")) if not isinstance(er, dict) else er.get("task_instance_id", "unknown")
            rows = getattr(er, "rows_written", er.get("rows_written", {})) if not isinstance(er, dict) else er.get("rows_written", {})

            exec_uuid = f"exec_{instance_id}"
            await self.create_node("DAGTask", {
                "uuid": exec_uuid,
                "task_instance_id": str(instance_id),
                "success": bool(success),
                "rows_written": sum(rows.values()) if isinstance(rows, dict) else 0,
                "recorded_at": now,
            }, labels=["DAGTask", "ExecutionResult"])
            await self.create_relationship(exec_uuid, task_uuid, KGRelationshipType.PRODUCED_BY)

            # Link execution to affected tables via row counts
            if isinstance(rows, dict):
                for table_name, row_count in rows.items():
                    for layer_tables in table_uuids.values():
                        if table_name in layer_tables:
                            await self.create_relationship(
                                exec_uuid, layer_tables[table_name],
                                "WROTE_TO",
                                {"rows": row_count},
                            )

        # ── Quality Checks ─────────────────────────────────────
        quality = runtime_info.get("quality_checks", {}) or {}
        checks = quality.get("checks", []) if isinstance(quality, dict) else []
        for check in checks[:50]:  # Cap at 50 to avoid graph bloat
            check_type = check.get("type", "unknown")
            table_name = check.get("table", "unknown")
            status = check.get("status", "unknown")
            detail = check.get("detail", "")[:200]

            check_uuid = f"quality_{check_type}_{table_name}_{status}"
            await self.create_node("QualityCheck", {
                "uuid": check_uuid,
                "check_type": check_type,
                "table": table_name,
                "layer": check.get("layer", ""),
                "status": status,
                "value": str(check.get("value", ""))[:100],
                "detail": detail,
                "recorded_at": now,
            }, labels=["QualityCheck"])
            await self.create_relationship(check_uuid, task_uuid, KGRelationshipType.PRODUCED_BY)

            # Link to the table node if known
            layer_key = check.get("layer", "").upper()
            if layer_key in table_uuids and table_name in table_uuids[layer_key]:
                await self.create_relationship(
                    check_uuid, table_uuids[layer_key][table_name],
                    "CHECKS",
                    {"status": status},
                )

        # ── Schema Drifts ──────────────────────────────────────
        drifts = runtime_info.get("schema_drifts", []) or []
        for drift in drifts:
            table_name = getattr(drift, "table_name", drift.get("table_name", "unknown")) if not isinstance(drift, dict) else drift.get("table_name", "unknown")
            drift_type = getattr(drift, "drift_type", drift.get("drift_type", "unknown")) if not isinstance(drift, dict) else drift.get("drift_type", "unknown")
            is_breaking = getattr(drift, "is_breaking", drift.get("is_breaking", False)) if not isinstance(drift, dict) else drift.get("is_breaking", False)
            details = getattr(drift, "details", drift.get("details", "")) if not isinstance(drift, dict) else drift.get("details", "")

            drift_type_str = str(drift_type.value) if hasattr(drift_type, "value") else str(drift_type)

            drift_uuid = f"drift_{table_name}_{drift_type_str}"
            await self.create_node("SchemaDrift", {
                "uuid": drift_uuid,
                "table_name": table_name,
                "drift_type": drift_type_str,
                "is_breaking": bool(is_breaking),
                "details": str(details)[:300],
                "recorded_at": now,
            }, labels=["SchemaDrift"])
            await self.create_relationship(drift_uuid, task_uuid, KGRelationshipType.PRODUCED_BY)

        # ── Clarification Answers ──────────────────────────────
        clarifications = runtime_info.get("clarifications", []) or []
        for c in clarifications:
            status_val = getattr(c, "status", c.get("status", "pending")) if not isinstance(c, dict) else c.get("status", "pending")
            status_str = str(status_val.value) if hasattr(status_val, "value") else str(status_val)
            if status_str not in ("answered",):
                continue

            question = getattr(c, "question", c.get("question", "")) if not isinstance(c, dict) else c.get("question", "")
            answer = getattr(c, "answer", c.get("answer", "")) if not isinstance(c, dict) else c.get("answer", "")
            cl_type = getattr(c, "clarification_type", c.get("clarification_type", "")) if not isinstance(c, dict) else c.get("clarification_type", "")
            cl_id = getattr(c, "id", c.get("id", str(uuid.uuid4()))) if not isinstance(c, dict) else c.get("id", str(uuid.uuid4()))

            cl_uuid = f"clarification_{cl_id}"
            await self.create_node(KGNodeType.CLARIFICATION, {
                "uuid": cl_uuid,
                "question": str(question)[:300],
                "answer": str(answer)[:500],
                "clarification_type": str(cl_type),
                "status": status_str,
                "recorded_at": now,
            }, labels=["ClarificationItem"])
            await self.create_relationship(cl_uuid, task_uuid, KGRelationshipType.CLARIFIES)

    async def health_check(self) -> dict[str, Any]:
        """Check Neo4j connectivity and return status."""
        import time
        t0 = time.monotonic()

        def _check():
            with self._session() as session:
                result = session.run(
                    "MATCH (n) RETURN count(n) as total_nodes, "
                    "labels(n) as labels LIMIT 1"
                )
                total = session.run("MATCH (n) RETURN count(n) as cnt").single()
                rel_total = session.run("MATCH ()-[r]->() RETURN count(r) as cnt").single()
                return {
                    "total_nodes": total["cnt"] if total else 0,
                    "total_relationships": rel_total["cnt"] if rel_total else 0,
                }

        try:
            stats = await self._run_async(_check)
            latency_ms = round((time.monotonic() - t0) * 1000, 1)
            return {
                "status": "healthy",
                "latency_ms": latency_ms,
                **stats,
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                "error": str(e)[:300],
            }


    async def sync_field_semantics_to_kg(self, table_names: list[str] | None = None) -> int:
        """Mirror PG field_semantics rows into SourceField nodes (idempotent).

        MERGE by uuid so re-runs never duplicate; used by the backfill script
        and by KG export for tasks that have no prior graph entries.
        Returns the number of field nodes written/updated.
        """
        from wren_agent.tools.pg_store import query_field_semantics

        rows = await query_field_semantics(table_names)

        def _sync():
            with self._session() as session:
                count = 0
                for r in rows:
                    src_uuid = f"src_table_{r['table_name']}"
                    field_uuid = f"field_{r['table_name']}.{r['field_name']}"
                    db_name = r.get("db_name") or ""
                    session.run(
                        "MERGE (t:SourceTable {uuid: $uuid}) "
                        "SET t.name = $name, t.table_name = $name, "
                        "t.db_name = $db, t.comment = '字段语义字典来源表'",
                        uuid=src_uuid, name=r["table_name"], db=db_name,
                    )
                    session.run(
                        "MERGE (f:SourceField {uuid: $uuid}) "
                        "SET f.name = $field, f.field_name = $field, f.table_name = $table, "
                        "f.db_name = $db, "
                        "f.chinese_meaning = $meaning, f.field_role = $role, "
                        "f.enum_values = $enum, f.comment = '字段语义字典'",
                        uuid=field_uuid, field=r["field_name"], table=r["table_name"],
                        db=db_name,
                        meaning=r.get("chinese_meaning") or "",
                        role=r.get("field_role") or "",
                        enum=r.get("enum_values") or [],
                    )
                    session.run(
                        "MATCH (t:SourceTable {uuid: $su}), (f:SourceField {uuid: $fu}) "
                        "MERGE (t)-[:HAS_FIELD]->(f)",
                        su=src_uuid, fu=field_uuid,
                    )
                    count += 1
                return count

        return await self._run_async(_sync)


# ── KG semantics fallback (T-14) ─────────────────────────────────
# When the PG semantic dictionary misses a table.field, grill/design query
# the KG for meanings harvested by earlier tasks: SourceField nodes with the
# same field name, preferring tables sharing the target's layer prefix.
# Merged rows are tagged source='kg_similar' with provenance (kg_from).


def merge_kg_semantics(
    semantics: dict[str, dict[str, dict]],
    kg_rows: list[dict],
    target_tables: list[str],
) -> int:
    """Merge KG fallback rows into the semantics dict (pure, testable).

    Only fields absent from `semantics` are adopted; the first meaning per
    (field) wins (rows arrive prefix-ranked). Returns the number of fields
    merged.
    """
    merged = 0
    for r in kg_rows:
        table = r.get("table_name")
        field = r.get("field_name")
        if not table or not field:
            continue
        meaning = (r.get("chinese_meaning") or "").strip()
        if not meaning:
            continue
        # adopt into every target table that has this field missing
        for target in target_tables:
            fields = semantics.setdefault(target, {})
            if field in fields:
                continue
            fields[field] = {
                "chinese_meaning": meaning,
                "field_role": r.get("field_role") or "",
                "enum_values": r.get("enum_values") or [],
                "source": "kg_similar",
                "kg_from": table,
            }
            merged += 1
    return merged


async def kg_field_semantics_fallback(
    table_field_pairs: list[tuple[str, str]],
    table_prefixes: list[str] | None = None,
    db_names: list[str] | None = None,
) -> dict[str, dict[str, dict]]:
    """Query the KG for field meanings of uncovered (table, field) pairs.

    Returns a semantics-shaped dict (table -> field -> row) tagged
    source='kg_similar'. `db_names` scopes the lookup to the task's relevant
    databases when provided. Never raises: KG unavailability yields {}.
    """
    if not table_field_pairs:
        return {}
    field_names = list({f for _, f in table_field_pairs})
    target_tables = list({t for t, _ in table_field_pairs})
    try:
        gs = GraphStore()
        try:
            rows = await gs.find_field_semantics(
                field_names, table_prefixes, db_names=db_names)
        finally:
            await gs.close()
    except Exception:
        logger.warning("KG field semantics fallback unavailable", exc_info=True)
        return {}
    out: dict[str, dict[str, dict]] = {}
    merge_kg_semantics(out, rows, target_tables)
    return out
