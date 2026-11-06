"""Dimension Library API — list/CRUD + history scan.

The dimension_registry (PG) holds reusable DIM layer dimensions. Dimensions
come from three sources:
- manual creation (POST /api/v1/dimensions)
- approved-task registration (_register_approved_assets in approval.py)
- POST /api/v1/dimensions/scan — scan persisted design plans of historical
  tasks (task_artifacts) and register their DIM designs (idempotent).

The scan restores the registry for tasks completed before the approval
registration was introduced (task a1af4bed investigation found the registries
were never written; dimension_registry still has no rows from older tasks).
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/dimensions", tags=["Dimensions"])


# ── Request / response models ───────────────────────────────────


class DimensionCreateRequest(BaseModel):
    dim_name: str = Field(..., max_length=255)
    dim_table: str = Field(..., max_length=255)
    db_name: str = Field(default="", max_length=255)
    natural_keys: list[str] = Field(default_factory=list)
    grain: str = Field(default="", max_length=1000)
    fields: list[dict] = Field(default_factory=list)
    variant_of: Optional[str] = Field(default=None, max_length=255)


class DimensionUpdateRequest(BaseModel):
    dim_name: Optional[str] = Field(default=None, max_length=255)
    dim_table: Optional[str] = Field(default=None, max_length=255)
    db_name: Optional[str] = Field(default=None, max_length=255)
    natural_keys: Optional[list[str]] = None
    grain: Optional[str] = Field(default=None, max_length=1000)
    fields: Optional[list[dict]] = None
    variant_of: Optional[str] = Field(default=None, max_length=255)
    version: Optional[int] = Field(default=None, ge=1)


class DimensionScanRequest(BaseModel):
    limit: Optional[int] = Field(default=None, ge=1, le=500)


def _dimension_item(row: dict) -> dict:
    return {
        "dim_id": str(row["dim_id"]),
        "dim_name": row["dim_name"],
        "dim_table": row["dim_table"],
        "db_name": row.get("db_name") or "",
        "owner_task_id": row["owner_task_id"],
        "natural_keys": list(row.get("natural_keys") or []),
        "grain": row["grain"],
        "fields": row.get("fields") or [],
        "variant_of": row.get("variant_of"),
        "version": row["version"],
        "active": row["active"],
        "created_at": str(row["created_at"]) if row.get("created_at") else None,
        "updated_at": str(row["updated_at"]) if row.get("updated_at") else None,
    }


# ── CRUD ────────────────────────────────────────────────────────


@router.get("")
async def list_dimensions(
    search: str = Query(default=""),
    dim_table_like: str = Query(default=""),
    db_name: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
):
    """Paginated dimension list with search / dim-table / db filters."""
    from wren_agent.tools.pg_store import paginate_dimension_registry

    result = await paginate_dimension_registry(
        search=search, dim_table_like=dim_table_like, db_name=db_name,
        page=page, page_size=page_size,
    )
    return {
        "total": result["total"],
        "page": page,
        "page_size": page_size,
        "items": [_dimension_item(r) for r in result["items"]],
    }


@router.post("", status_code=201)
async def create_dimension(request: DimensionCreateRequest):
    """Manually register a dimension."""
    from wren_agent.tools.pg_store import register_dimension

    result = await register_dimension(
        dim_name=request.dim_name,
        dim_table=request.dim_table,
        owner_task_id="manual",
        natural_keys=list(request.natural_keys or []),
        grain=request.grain or "",
        fields=list(request.fields or []),
        variant_of=request.variant_of,
        db_name=request.db_name or "",
    )
    if result.get("status") == "conflict":
        raise HTTPException(409, f"Dimension table '{request.dim_table}' already exists")
    return {"dim_id": str(result["dim_id"]), "status": "created"}


@router.put("/{dim_id}")
async def update_dimension(dim_id: str, request: DimensionUpdateRequest):
    """Partial update of a dimension (non-None fields)."""
    from wren_agent.tools.pg_store import update_dimension as pg_update

    updated = await pg_update(dim_id, **request.model_dump())
    if not updated:
        raise HTTPException(404, f"Dimension '{dim_id}' not found")
    return {"dim_id": dim_id, "updated": True}


@router.delete("/{dim_id}")
async def delete_dimension(dim_id: str):
    """Soft-delete a dimension (active=FALSE)."""
    from wren_agent.tools.pg_store import soft_delete_dimension

    deleted = await soft_delete_dimension(dim_id)
    if not deleted:
        raise HTTPException(404, f"Dimension '{dim_id}' not found")
    return {"dim_id": dim_id, "deleted": True}


# ── History scan ────────────────────────────────────────────────


@router.post("/scan")
async def scan_history_dimensions(request: DimensionScanRequest | None = None):
    """Scan persisted design plans of historical tasks and register DIM designs.

    Reads task_artifacts.design_plan (DWDesignPlan), collects each task's
    dim_designs, and registers them idempotently (a dim_table already active
    in the registry is skipped). owner_task_id preserves task provenance.
    """
    from wren_agent.models.design import DWDesignPlan
    from wren_agent.tools.pg_store import (
        get_pg_pool,
        list_task_artifacts_designs,
        register_dimension,
    )

    limit = request.limit if request else None
    artifacts = await list_task_artifacts_designs(limit=limit)
    pool = get_pg_pool()

    # Existing active dim_tables — idempotency baseline
    async with pool.acquire() as conn:
        existing = {
            r["dim_table"]
            for r in await conn.fetch(
                "SELECT dim_table FROM dimension_registry WHERE active = TRUE"
            )
        }

    scanned_tasks = 0
    registered, skipped = 0, 0
    results: list[dict] = []
    for art in artifacts:
        try:
            plan = DWDesignPlan.model_validate(art["design_plan"])
        except Exception:
            logger.warning(
                "scan_history_dimensions: unparseable design_plan for task %s",
                art["task_id"][:8],
            )
            continue
        scanned_tasks += 1
        for dim in (plan.dim_designs or []):
            dim_table = (dim.dim_table or "").strip()
            if not dim_table:
                continue
            if dim_table in existing:
                skipped += 1
                results.append({
                    "dim_name": dim.dim_name,
                    "dim_table": dim_table,
                    "status": "skipped", "reason": "duplicate",
                })
                continue
            result = await register_dimension(
                dim_name=dim.dim_name or dim_table,
                dim_table=dim_table,
                owner_task_id=art["task_id"],
                natural_keys=list(dim.natural_key or []),
                grain=",".join(str(k) for k in (dim.natural_key or [])),
                fields=[f for f in (dim.fields or [])],
                variant_of=dim.dim_variant_of,
            )
            if result.get("status") == "created":
                existing.add(dim_table)
                registered += 1
                results.append({
                    "dim_name": dim.dim_name or dim_table,
                    "dim_table": dim_table,
                    "status": "created",
                })
            else:
                skipped += 1
                results.append({
                    "dim_name": dim.dim_name or dim_table,
                    "dim_table": dim_table,
                    "status": "skipped",
                    "reason": result.get("message", "conflict"),
                })

    return {
        "scanned_tasks": scanned_tasks,
        "registered": registered,
        "skipped": skipped,
        "dimensions": results,
    }
