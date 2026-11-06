"""Dependency health checker — tests connectivity + latency for all services."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import httpx

from wren_agent.config import settings

# ── Single service check ────────────────────────────────────────


async def _check_with_latency(
    name: str,
    url: str,
    fn,
    *args,
    **kwargs,
) -> dict[str, Any]:
    """Run a check function and wrap with latency measurement."""
    start = time.monotonic()
    try:
        result = await fn(*args, **kwargs)
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        return {
            "status": "healthy",
            "url": url,
            "latency_ms": latency_ms,
            "details": result,
        }
    except Exception as e:
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        return {
            "status": "unhealthy",
            "url": url,
            "latency_ms": latency_ms,
            "error": str(e)[:500],
        }


# ── Per-service checks ──────────────────────────────────────────


async def check_postgres() -> dict[str, Any]:
    """Check PostgreSQL connectivity and version."""
    from wren_agent.tools.pg_store import get_pg_pool

    async def _check():
        pool = get_pg_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT version(), pg_database_size(current_database())")
            return {
                "version": row[0].split(",")[0] if row else "unknown",
                "database_size_bytes": row[1] if row else 0,
            }

    dsn_display = settings.pg.dsn.split("@")[-1] if "@" in settings.pg.dsn else settings.pg.dsn
    return await _check_with_latency("postgres", dsn_display, _check)


async def check_dolphin_scheduler() -> dict[str, Any]:
    """Check Dolphin Scheduler connectivity via REST API."""

    async def _check():
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{settings.ds.base_url}/projects?pageSize=5&pageNo=1",
                headers={"token": settings.ds.api_key},
            )
            resp.raise_for_status()
            data = resp.json()
            projects_data = data.get("data", data)
            if isinstance(projects_data, dict):
                total = projects_data.get("total", 0)
                projects = projects_data.get("totalList", [])
            elif isinstance(projects_data, list):
                projects = projects_data
                total = len(projects)
            else:
                total = 0
                projects = []
            return {"projects_count": total, "http_status": resp.status_code}

    return await _check_with_latency("dolphin_scheduler", settings.ds.base_url, _check)


async def check_neo4j() -> dict[str, Any]:
    """Check Neo4j connectivity and version."""
    if not settings.neo4j.password:
        return {
            "status": "unconfigured",
            "url": settings.neo4j.uri,
            "latency_ms": 0,
            "error": "Neo4j password not set (check NEO4J_PASSWORD in .env)",
        }

    async def _check():
        from neo4j import AsyncGraphDatabase
        driver = AsyncGraphDatabase.driver(
            settings.neo4j.uri,
            auth=(settings.neo4j.user, settings.neo4j.password),
        )
        try:
            async with driver.session(database=settings.neo4j.database) as session:
                result = await session.run("CALL dbms.components() YIELD name, versions, edition RETURN name, versions, edition LIMIT 1")
                record = await result.single()
                return {
                    "name": record["name"] if record else "unknown",
                    "versions": record["versions"] if record else [],
                    "edition": record["edition"] if record else "unknown",
                }
        finally:
            await driver.close()

    return await _check_with_latency("neo4j", settings.neo4j.uri, _check)


async def check_doris() -> dict[str, Any]:
    """Check Doris connectivity and version."""
    if not settings.doris.password or settings.doris.password == "yourpassword":
        return {
            "status": "unconfigured",
            "url": f"{settings.doris.host}:{settings.doris.query_port}",
            "latency_ms": 0,
            "error": "Doris password not configured (check DORIS_PASSWORD in .env)",
        }

    async def _check():
        import pymysql
        conn = pymysql.connect(
            host=settings.doris.host,
            port=settings.doris.query_port,
            user=settings.doris.user,
            password=settings.doris.password,
            database=settings.doris.database or "information_schema",
            connect_timeout=10,
        )
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION()")
                version = cur.fetchone()
                cur.execute(f"SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = '{settings.doris.database}'")
                table_count = cur.fetchone()
                return {
                    "version": version[0] if version else "unknown",
                    "tables_in_db": table_count[0] if table_count else 0,
                    "database": settings.doris.database,
                }
        finally:
            conn.close()

    return await _check_with_latency(
        "doris",
        f"{settings.doris.host}:{settings.doris.query_port}/{settings.doris.database}",
        _check,
    )


async def check_secret_manager() -> dict[str, Any]:
    """Check Secret Manager availability and SM4 fallback."""
    import time
    t0 = time.monotonic()
    try:
        from wren_agent.tools.secret_manager import get_credential_provider
        provider = get_credential_provider()
        health = await provider.health_check()
        health["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        return health
    except Exception as e:
        return {
            "status": "unhealthy",
            "backend": settings.secret.backend,
            "error": str(e)[:300],
            "latency_ms": round((time.monotonic() - t0) * 1000, 1),
        }


async def check_llm() -> dict[str, Any]:
    """Check LLM API connectivity via a minimal completion call."""
    if not settings.llm.api_key:
        return {
            "status": "unconfigured",
            "url": settings.llm.api_base,
            "latency_ms": 0,
            "error": "LLM API key not configured (check LLM_API_KEY in .env)",
        }

    # Only send a tiny completion to verify auth + latency
    async def _check():
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{settings.llm.api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.llm.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.llm.model,
                    "messages": [{"role": "user", "content": "1+1=?"}],
                    "max_tokens": 5,
                    "temperature": 0,
                },
            )
            if resp.status_code != 200:
                raise Exception(f"HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return {
                "model": settings.llm.model,
                "provider": settings.llm.provider,
                "response": content.strip()[:50],
            }

    return await _check_with_latency("llm", f"{settings.llm.provider}:{settings.llm.model}", _check)


# ── Aggregate health check ──────────────────────────────────────


async def check_all() -> dict[str, Any]:
    """Run all health checks in parallel and aggregate results."""

    # Run all checks concurrently
    import asyncio
    results = await asyncio.gather(
        check_postgres(),
        check_dolphin_scheduler(),
        check_neo4j(),
        check_doris(),
        check_secret_manager(),
        check_llm(),
        return_exceptions=True,
    )

    # Circuit breakers
    try:
        from wren_agent.tools.circuit_breaker import get_all_stats
        cb_stats = get_all_stats()
        if cb_stats:
            services["circuit_breakers"] = {
                "count": len(cb_stats),
                "breakers": cb_stats,
            }
    except Exception:
        pass

    names = ["postgresql", "dolphin_scheduler", "neo4j", "doris", "secret_manager", "llm"]
    services = {}
    all_healthy = True

    for name, result in zip(names, results):
        if isinstance(result, Exception):
            services[name] = {"status": "unhealthy", "error": str(result)[:500], "latency_ms": 0}
            all_healthy = False
        else:
            services[name] = result
            if result.get("status") == "unhealthy":
                all_healthy = False

    healthy_count = sum(1 for s in services.values() if s.get("status") == "healthy")
    total = len(names)

    return {
        "status": "ok" if all_healthy else "degraded",
        "service": "wren-modeling-agent",
        "version": "0.1.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": f"{healthy_count}/{total} services healthy",
        "services": services,
    }
