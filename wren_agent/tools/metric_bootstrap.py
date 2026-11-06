"""Bootstrap metric_registry from warehouse table/field comments.

Numeric columns of dim/dwd/dws/ads tables become atomic metrics:
metric_name = the field's Chinese COMMENT (or the column name), definition =
SUM(col), source_table = schema.table, grain = the table's key columns.
Idempotent per (source_table, metric_name).

Single write path: register_metric_rows() — used by the metric-library
generation API (user-triggered). NOT run automatically during exploration
(the user chose an explicit wizard flow).
"""

import logging
import re

logger = logging.getLogger(__name__)

# Columns that never become metrics (IDs / keys / status / flags / metadata).
# Count/num columns ARE metrics (收款笔数, 合同数) — deliberately not excluded.
_NON_METRIC_RE = re.compile(
    r"(_id$|_key$|_no$|_code$|_sk$|^id$|_status$|_flag$|_state$|_type$|"
    r"^is_|^has_|_version$|_date$|_time$|_at$|etl_|dq_|"
    r"^row_|_sort$|_ordx?$|_is$|_word$|batch$|remark$|name$|_bk$|_pk$)",
    re.IGNORECASE,
)

_NUMERIC_TYPE_RE = re.compile(
    r"^(decimal|numeric|double|float|bigint|int|integer|smallint|tinyint)\b",
    re.IGNORECASE,
)


def is_metric_candidate(column_name: str, data_type: str) -> bool:
    """True when a column qualifies as an atomic-metric source."""
    return bool(
        _NUMERIC_TYPE_RE.match((data_type or "").strip())
        and not _NON_METRIC_RE.search(column_name or "")
    )


def _bootstrap_metric_rows_from_columns(columns: list[dict]) -> list[dict]:
    """Build metric-row dicts from generic column descriptors.

    Each column dict: {source_table, column_name, data_type, comment,
    pk_names (list[str]), layer (optional str)}.
    """
    rows = []
    for col in columns:
        column_name = str(col.get("column_name") or "")
        data_type = str(col.get("data_type") or "")
        if not is_metric_candidate(column_name, data_type):
            continue
        comment = (col.get("comment") or "").strip()
        source_table = str(col.get("source_table") or "")
        if not source_table:
            continue
        rows.append({
            "source_table": source_table,
            "metric_name": (comment or column_name)[:120],
            "definition_sql": f"SUM(`{column_name}`)",
            "grain": ",".join(str(k) for k in (col.get("pk_names") or [])),
            "layer": str(col.get("layer") or ""),
        })
    return rows


async def register_metric_rows(rows: list[dict], owner: str = "manual-generation") -> dict:
    """Batch-register atomic metrics, idempotent per (source_table, metric_name).

    Single write path for wizard-style metric generation. Rows are column
    dicts as produced by _bootstrap_metric_rows_from_columns (layer becomes a
    tag). Returns {"registered": int, "skipped_items": [dict,...],
    "inserted": [dict,...]}.
    """
    from wren_agent.tools.pg_store import get_pg_pool

    if not rows:
        return {"registered": 0, "skipped_items": [], "inserted": []}

    pool = get_pg_pool()
    async with pool.acquire() as conn:
        existing = {
            (r["source_table"], r["metric_name"])
            for r in await conn.fetch(
                "SELECT source_table, metric_name FROM metric_registry "
                "WHERE active = TRUE"
            )
        }
    fresh, skipped = [], []
    for row in rows:
        key = (row["source_table"], row["metric_name"])
        if key in existing:
            skipped.append({
                "metric_name": row["metric_name"],
                "source_table": row["source_table"],
                "status": "skipped", "reason": "duplicate",
            })
        else:
            fresh.append(row)
            existing.add(key)

    inserted: list[dict] = []
    if fresh:
        # Per-row INSERT. The previous batch path used
        # `unnest($7::text[][], $8::text[][])` for the TEXT[] columns —
        # PG has no real 2-D array type (text[][] IS text[]), so the arrays
        # were encoded as empty 1-D arrays and unnest emitted `text`
        # elements, failing with `column "parent_metrics" is of type text[]
        # but expression is of type text` on every real run (the unit test
        # mocked the pool, so the path was never exercised). asyncpg encodes
        # Python lists into TEXT[] natively — pass them directly.
        async with pool.acquire() as conn:
            async with conn.transaction():
                for row in fresh:
                    db_row = await conn.fetchrow(
                        "INSERT INTO metric_registry "
                        "(metric_name, metric_type, definition_sql, source_table, "
                        " owner_task_id, grain, parent_metrics, tags) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
                        "RETURNING metric_id",
                        row["metric_name"], "atomic", row["definition_sql"],
                        row["source_table"], owner, row["grain"],
                        [], [row.get("layer", ""), owner],
                    )
                    inserted.append({
                        "metric_id": str(db_row["metric_id"]),
                        "metric_name": row["metric_name"],
                        "source_table": row["source_table"],
                        "definition_sql": row["definition_sql"],
                        "status": "created",
                    })
    return {"registered": len(inserted), "skipped_items": skipped, "inserted": inserted}
