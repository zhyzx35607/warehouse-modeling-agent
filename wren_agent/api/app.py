"""FastAPI application entry point for the Wren Modeling Agent."""

from __future__ import annotations

# Fix: Windows uses ProactorEventLoop but psycopg needs SelectorEventLoop
import sys
if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import logging
from contextlib import asynccontextmanager

from pathlib import Path as _Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from wren_agent.api.routes import router as rest_router
from wren_agent.api.source_dbs import router as source_db_router
from wren_agent.api.lineage import router as lineage_router
from wren_agent.api.catalog import router as catalog_router
from wren_agent.api.websocket import ws_router
from wren_agent.config import settings

logger = logging.getLogger(__name__)

# Application logs were silently dropped (no root handler) — only uvicorn
# access logs reached agent.log, leaving node-level hangs undiagnosable.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Init PG, runner, and MCP clients on startup; clean up on shutdown."""
    # Startup
    logger.info("Starting Wren Agent services...")

    if not settings.llm.is_configured:
        raise RuntimeError("Live mode requires LLM_API_KEY (or LLM_PROVIDER=ollama). For offline mode use python -m warehouse_demo --serve.")
    # 0. Verify critical runtime dependencies are importable.
    #    A missing module here fails fast with a clear message instead of
    #    surfacing mid-task as "No module named 'cli_anything'" after
    #    the graph has already consumed hundreds of steps.
    _CRITICAL_MODULES = [
        ("cli_anything.dolphinscheduler.core", "cli-anything-dolphinscheduler"),
        ("langgraph", "langgraph"),
        ("fastapi", "fastapi"),
        ("neo4j", "neo4j"),
    ]
    for _mod, _pkg in _CRITICAL_MODULES:
        try:
            __import__(_mod)
        except ImportError as exc:
            logger.critical(
                "CRITICAL: Module '%s' is not importable. "
                "Install it with: uv pip install %s",
                _mod, _pkg,
            )
            raise RuntimeError(
                f"Critical runtime dependency '{_mod}' is unavailable"
            ) from exc

    # 1. Init PostgreSQL
    try:
        from wren_agent.tools.pg_store import init_pg_pool
        await init_pg_pool()
        logger.info(f"PG pool initialized")

        # Run migrations
        from wren_agent.tools.migration import run_migrations
        await run_migrations()
    except Exception:
        logger.exception("PG init failed — pool or migrations unavailable")
        raise

    # 2. Init AgentRunner (LangGraph + Checkpointer)
    try:
        from wren_agent.engine import init_runner
        await init_runner()
        logger.info("AgentRunner initialized (Graph + PG Checkpointer)")
    except Exception:
        logger.exception(
            "AgentRunner init failed — task execution and recovery unavailable"
        )
        raise

    # 2. Init DB MCP client
    try:
        from wren_agent.mcp_servers.db_client import get_db_client
        get_db_client()
        logger.info("DB MCP client initialized")
    except Exception as e:
        logger.warning(f"DB MCP client init failed: {e}")

    # 3. DS connectivity verified via health check REST API (token-based)

    yield

    # Shutdown
    logger.info("Shutting down Wren Agent services...")
    # DS MCP not used for health — REST API direct connection instead

    try:
        from wren_agent.engine import shutdown_runner
        await shutdown_runner()
        logger.info("AgentRunner shut down")
    except Exception:
        pass

    try:
        from wren_agent.tools.pg_store import close_pg_pool
        await close_pg_pool()
    except Exception:
        pass


# ── FastAPI app ─────────────────────────────────────────────────


app = FastAPI(
    title="Wren Modeling Agent API",
    description="AI-powered data warehouse modeling agent REST + WebSocket API",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS for Dashboard
_cors_origins = settings.server.cors_origins or []
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials="*" not in _cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount routes
app.include_router(rest_router)
app.include_router(source_db_router)
app.include_router(lineage_router)
app.include_router(catalog_router)
from wren_agent.api.metrics import router as metrics_router
app.include_router(metrics_router)
from wren_agent.api.dimensions import router as dimensions_router
app.include_router(dimensions_router)
app.include_router(ws_router)

# ── Dashboard static files (served via nginx in production, disabled in dev) ──
# To serve: set SERVE_DASHBOARD=true or use nginx reverse proxy


@app.get("/health")
async def health_check():
    """Health check endpoint — tests all dependent services with latency.

    Returns per-service: status (healthy/unhealthy/unconfigured), latency_ms, details or error.
    Aggregate status: ok (all healthy) | degraded (some unhealthy).
    """
    from wren_agent.api.health_check import check_all
    result = await check_all()
    status_code = 200 if result.get("status") == "ok" else 503
    return JSONResponse(status_code=status_code, content=result)


@app.get("/health/{service}")
async def health_check_service(service: str):
    """Health check for a single service.

    Valid service names: postgresql | dolphin_scheduler | neo4j | doris | llm
    """
    from wren_agent.api.health_check import (
        check_postgres,
        check_dolphin_scheduler,
        check_neo4j,
        check_doris,
        check_llm,
    )

    checkers = {
        "postgresql": check_postgres,
        "dolphin_scheduler": check_dolphin_scheduler,
        "neo4j": check_neo4j,
        "doris": check_doris,
        "llm": check_llm,
    }

    checker = checkers.get(service)
    if not checker:
        from fastapi import HTTPException
        raise HTTPException(404, f"Unknown service: {service}. Valid: {', '.join(checkers.keys())}")

    return await checker()


if __name__ == "__main__":
    import sys, asyncio
    import uvicorn

    if sys.platform == "win32" and not settings.server.reload:
        # psycopg async cannot run on ProactorEventLoop, and uvicorn ≥0.51
        # ignores the event-loop policy (Server.run hardcodes ProactorEventLoop
        # via its loop_factory on Windows). Bypass Server.run and drive
        # serve() on a SelectorEventLoop ourselves. Safe here: the server
        # process spawns no asyncio subprocesses (DB MCP runs in-process).
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        config = uvicorn.Config(
            app,
            host=settings.server.host,
            port=settings.server.port,
            log_level=settings.server.log_level.lower(),
        )
        asyncio.run(uvicorn.Server(config).serve())
    else:
        uvicorn.run(
            app,
            host=settings.server.host,
            port=settings.server.port,
            log_level=settings.server.log_level.lower(),
            reload=settings.server.reload,
        )
