"""Data Lineage & Asset Map REST API.

GET /api/v1/lineage/{task_id}     — full lineage graph for D3/ECharts
GET /api/v1/asset-map/{task_id}   — data asset map summary
GET /api/v1/impact/{dim_table}    — dim change impact analysis
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from wren_agent.config import settings

router = APIRouter(prefix="/api/v1", tags=["Lineage & Assets"])


@router.get("/lineage/{task_id}")
async def get_lineage(task_id: str, depth: int = Query(default=5, le=10)):
    """Get the full data lineage graph for visualization.

    Returns nodes + edges in a format compatible with ECharts/D3 force graph.
    """
    try:
        from wren_agent.tools.neo4j_store import GraphStore
        gs = GraphStore()
        export = await gs.export_subgraph(task_id)
        await gs.close()

        # Transform to ECharts-compatible format
        nodes = []
        edges = []
        node_map = {}

        for layer_key, layer_nodes in export.get("layers", {}).items():
            for n in layer_nodes:
                uid = n.get("uuid", "")
                nodes.append({
                    "id": uid,
                    "name": n.get("name") or n.get("table_name") or n.get("dim_name") or n.get("metric_name") or uid,
                    "label": layer_key,
                    "category": _category_map.get(layer_key, 0),
                    "properties": {
                        k: v for k, v in n.items()
                        if k not in ("labels", "uuid") and v is not None
                        and not isinstance(v, (list, dict))
                    },
                })
                node_map[uid] = layer_key

        for e in export.get("edges", []):
            edges.append({
                "source": e.get("from", ""),
                "target": e.get("to", ""),
                "label": e.get("type", ""),
            })

        return {
            "task_id": task_id,
            "nodes": nodes,
            "edges": edges,
            "summary": {
                "total_nodes": export.get("total_nodes", 0),
                "total_edges": export.get("total_edges", 0),
                "node_types": export.get("node_types", {}),
            },
        }
    except Exception as e:
        raise HTTPException(500, f"Failed to get lineage: {e}")


@router.get("/asset-map/{task_id}")
async def get_asset_map(task_id: str):
    """Get the data asset map summary for a task."""
    try:
        from wren_agent.tools.neo4j_store import GraphStore
        gs = GraphStore()
        export = await gs.export_subgraph(task_id)
        await gs.close()

        # Build layer-by-layer asset map
        asset_map = {
            "task_id": task_id,
            "total_nodes": export.get("total_nodes", 0),
            "total_edges": export.get("total_edges", 0),
            "layers": {},
        }

        for layer_key, layer_nodes in export.get("layers", {}).items():
            items = []
            for n in layer_nodes:
                item = {"name": n.get("name") or n.get("table_name") or n.get("dim_name") or n.get("metric_name", "?")}
                # Add layer-specific metadata
                if layer_key == "ods":
                    item["sync_method"] = n.get("sync_method", "?")
                elif layer_key == "dim":
                    item["key"] = n.get("natural_keys", [])
                elif layer_key == "dwd":
                    item["grain"] = n.get("grain", [])
                elif layer_key == "ads" or layer_key == "metrics":
                    item["expression"] = n.get("expression", "")
                items.append(item)

            asset_map["layers"][layer_key] = {
                "count": len(items),
                "items": items,
            }

        return asset_map
    except Exception as e:
        raise HTTPException(500, f"Failed to get asset map: {e}")


@router.get("/impact/{dim_table}")
async def get_impact_analysis(dim_table: str):
    """Get downstream impact of a DIM table change."""
    try:
        from wren_agent.tools.neo4j_store import GraphStore
        gs = GraphStore()
        impact = await gs.get_impact_analysis(dim_table)
        await gs.close()
        return impact
    except Exception as e:
        raise HTTPException(500, f"Failed to get impact: {e}")


@router.get("/tasks/{task_id}/quality")
async def get_quality_report(task_id: str):
    """Get the data quality report for a task."""
    # This would read from PG — for now returns placeholder
    return {
        "task_id": task_id,
        "message": "Quality report available from task state — query /task/{id} for live data",
    }


_category_map = {
    "source": 0,
    "ods": 1,
    "dim": 2,
    "dwd": 3,
    "dws": 4,
    "ads": 5,
    "metrics": 6,
}
