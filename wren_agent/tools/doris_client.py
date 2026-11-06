"""Doris client — MySQL protocol via pymysql for DW operations.

Doris FE exposes MySQL-compatible protocol on query_port (default 9030).
All methods are async via asyncio.to_thread for thread safety.

Supports: DDL execution, DML queries, schema introspection, row count
         comparison (Harness validation Level 1 & 2).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from wren_agent.config import settings

logger = logging.getLogger(__name__)


class DorisClient:
    """Async Doris client over MySQL protocol (pymysql)."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        database: str | None = None,
    ):
        self._host = host or settings.doris.host
        self._port = port or settings.doris.query_port
        self._user = user or settings.doris.user
        self._password = password or settings.doris.password
        # None → settings default. Explicit "" means "no default catalog"
        # (needed for CREATE DATABASE when the registry catalog is gone).
        self._database = settings.doris.database if database is None else database

    def _connect(self):
        """Create a new pymysql connection to Doris."""
        import pymysql
        kwargs = dict(
            host=self._host,
            port=self._port,
            user=self._user,
            password=self._password,
            charset="utf8mb4",
            connect_timeout=15,
            read_timeout=300,
            autocommit=True,
        )
        if self._database:
            kwargs["database"] = self._database
        return pymysql.connect(**kwargs)

    async def _run(self, fn):
        """Run a sync operation in executor thread."""
        return await asyncio.get_running_loop().run_in_executor(None, fn)

    # ── DDL / DML ────────────────────────────────────────────

    async def execute(self, sql: str) -> dict:
        """Execute a DDL or DML statement. Returns {success, affected_rows}."""

        def _do():
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    return {"success": True, "affected_rows": cur.rowcount, "sql": sql[:200]}
            finally:
                conn.close()

        return await self._run(_do)

    async def execute_ddl(self, sql: str) -> dict:
        """Execute DDL — same as execute() with logging."""
        logger.info(f"Doris DDL: {sql[:120]}...")
        return await self.execute(sql)

    async def execute_many(self, sqls: list[str]) -> list[dict]:
        """Execute multiple SQL statements sequentially. Returns per-statement results."""
        results = []
        for sql in sqls:
            result = await self.execute(sql)
            results.append(result)
            if not result.get("success"):
                logger.error(f"Doris SQL failed: {sql[:120]}")
                break
        return results

    # ── Queries ──────────────────────────────────────────────

    async def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute a SELECT query, return rows as dicts."""

        def _do():
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    # Empty params must NOT be forwarded as `cur.execute(sql, ())`
                    # — pymysql's mogrify treats every `%` in the SQL as a
                    # printf placeholder and fails with `not enough arguments
                    # for format string` on perfectly valid statements
                    # (`DATE_FORMAT(x, '%Y-%m')`, `LIKE 'x%'`). No params →
                    # execute the raw SQL directly (task 3d19c031: the EXPLAIN
                    # probe rejected every DWS load SQL this way).
                    if params:
                        cur.execute(sql, params)
                    else:
                        cur.execute(sql)
                    if cur.description:
                        cols = [c[0] for c in cur.description]
                        rows = cur.fetchall()
                        return [dict(zip(cols, row)) for row in rows]
                    return []
            finally:
                conn.close()

        return await self._run(_do)

    async def query_one(self, sql: str, params: tuple = ()) -> dict | None:
        """Execute a SELECT query, return first row."""
        rows = await self.query(sql, params)
        return rows[0] if rows else None

    async def query_value(self, sql: str, params: tuple = ()) -> Any:
        """Execute a SELECT query, return single scalar."""
        row = await self.query_one(sql, params)
        if row:
            return list(row.values())[0]
        return None

    # ── Schema ───────────────────────────────────────────────

    async def get_table_schema(self, table_name: str) -> dict:
        """Get column definitions for a Doris table via DESCRIBE."""
        rows = await self.query(f"DESCRIBE `{table_name}`")
        columns = []
        primary_keys = []
        for r in rows:
            col = {
                "name": r.get("Field", ""),
                "type": r.get("Type", ""),
                "nullable": r.get("Null", "YES") == "YES",
                "key": r.get("Key", ""),
                "default": r.get("Default", ""),
                "extra": r.get("Extra", ""),
            }
            columns.append(col)
            if col["key"] == "PRI":
                primary_keys.append(col["name"])

        return {
            "table_name": table_name,
            "database": self._database,
            "columns": columns,
            "column_count": len(columns),
            "primary_keys": primary_keys,
        }

    async def table_exists(self, table_name: str) -> bool:
        """Check if a table exists in Doris."""
        count = await self.query_value(
            "SELECT COUNT(*) as cnt FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name = %s",
            (self._database, table_name),
        )
        return count > 0 if count else False

    async def list_tables(self) -> list[str]:
        """List all tables in the current database."""
        rows = await self.query(
            "SELECT TABLE_NAME FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type = 'BASE TABLE' "
            "ORDER BY TABLE_NAME",
            (self._database,),
        )
        return [r.get("TABLE_NAME", "") for r in rows]

    # ── Row count / validation ───────────────────────────────

    async def get_row_count(self, table_name: str) -> int:
        """Get exact row count."""
        val = await self.query_value(f"SELECT COUNT(*) as cnt FROM `{table_name}`")
        return int(val) if val else 0

    async def get_approx_row_count(self, table_name: str) -> int:
        """Get approximate row count (faster for large tables)."""
        val = await self.query_value(
            "SELECT TABLE_ROWS FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name = %s",
            (self._database, table_name),
        )
        return int(val) if val else 0

    async def compare_row_counts(
        self,
        source_table: str,
        target_table: str,
        tolerance: float = 0.0,
    ) -> dict:
        """Compare row counts between two tables (Harness Level 2 validation).

        Returns {match, source_count, target_count, diff, diff_pct}.

        For tolerance > 0, allows a percentage difference.
        """
        src = await self.get_row_count(source_table)
        tgt = await self.get_row_count(target_table)

        diff = abs(src - tgt)
        diff_pct = (diff / src * 100) if src > 0 else 0

        match = diff_pct <= tolerance * 100

        return {
            "match": match,
            "source_table": source_table,
            "source_count": src,
            "target_table": target_table,
            "target_count": tgt,
            "diff": diff,
            "diff_pct": round(diff_pct, 2),
        }

    async def compare_aggregation(
        self,
        detail_table: str,
        summary_table: str,
        measure: str,
        group_by: list[str] | None = None,
    ) -> dict:
        """Compare aggregated values (DWS vs DWD validation).

        Checks that SUM(detail.measure) == SUM(summary.measure)
        optionally grouped by dimensions.
        """
        # Sum from detail
        detail_val = await self.query_value(
            f"SELECT SUM(`{measure}`) as val FROM `{detail_table}`"
        )

        # Sum from summary
        summary_val = await self.query_value(
            f"SELECT SUM(`{measure}`) as val FROM `{summary_table}`"
        )

        detail_val = float(detail_val or 0)
        summary_val = float(summary_val or 0)
        diff = abs(detail_val - summary_val)
        match = diff < 0.01  # Within 0.01 tolerance

        return {
            "match": match,
            "measure": measure,
            "detail_table": detail_table,
            "detail_sum": round(detail_val, 4),
            "summary_table": summary_table,
            "summary_sum": round(summary_val, 4),
            "diff": round(diff, 4),
        }

    async def run_validation_query(self, sql: str, expected_col: str = "result") -> dict:
        """Run a Harness validation check query.

        Returns {passed, result, expected, sql}.
        The SQL should return rows with a 'result' column for boolean checks.
        """
        try:
            rows = await self.query(sql)
            passed = all(row.get(expected_col, False) for row in rows) if rows else True

            return {
                "passed": passed,
                "sql": sql[:200],
                "rows_returned": len(rows),
                "failed_rows": sum(1 for r in rows if not r.get(expected_col, False)) if rows else 0,
            }
        except Exception as e:
            return {
                "passed": False,
                "sql": sql[:200],
                "error": str(e)[:500],
            }

    # ── Health ───────────────────────────────────────────────

    async def health_check(self) -> dict:
        """Check Doris connectivity and return stats."""
        t0 = time.monotonic()
        try:
            version = await self.query_value("SELECT VERSION()")
            table_count = await self.query_value(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = %s AND table_type = 'BASE TABLE'",
                (self._database,),
            )
            return {
                "status": "healthy",
                "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                "version": version,
                "database": self._database,
                "table_count": table_count or 0,
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                "error": str(e)[:300],
            }
