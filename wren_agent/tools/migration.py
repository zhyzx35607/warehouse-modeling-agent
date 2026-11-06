"""Database migration runner.

Runs SQL migration files in order against the PostgreSQL database.
Tracks applied migrations in a pg_migrations table.
"""

from __future__ import annotations

import logging
from pathlib import Path

from wren_agent.config import settings

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "migrations"


async def ensure_migration_table(pool) -> None:
    """Create the migration tracking table if it doesn't exist."""
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pg_migrations (
                id          SERIAL PRIMARY KEY,
                filename    TEXT NOT NULL UNIQUE,
                applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)


async def get_applied_migrations(pool) -> set[str]:
    """Get the set of already-applied migration filenames."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT filename FROM pg_migrations ORDER BY id")
        return {row["filename"] for row in rows}


async def apply_migration(pool, filename: str, sql: str) -> None:
    """Apply a single migration and record it."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(sql)
            await conn.execute(
                "INSERT INTO pg_migrations (filename) VALUES ($1)",
                filename,
            )
    logger.info(f"Migration applied: {filename}")


async def run_migrations() -> None:
    """Run all pending migrations in order."""
    if not MIGRATIONS_DIR.exists():
        logger.warning(f"Migrations directory not found: {MIGRATIONS_DIR}")
        return

    from wren_agent.tools.pg_store import get_pg_pool

    try:
        pool = get_pg_pool()
    except RuntimeError:
        logger.warning("PG pool not initialized — skipping migrations")
        return

    await ensure_migration_table(pool)
    applied = await get_applied_migrations(pool)

    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    pending = [f for f in migration_files if f.name not in applied]

    if not pending:
        logger.info("All migrations up to date")
        return

    logger.info(f"Running {len(pending)} pending migration(s)...")

    for mf in pending:
        sql = mf.read_text(encoding="utf-8")
        await apply_migration(pool, mf.name, sql)

    logger.info(f"All {len(pending)} migrations applied successfully")
