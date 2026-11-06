#!/usr/bin/env python
"""Wren Agent Launcher — sets Windows event loop policy before any imports."""

import sys

# MUST be first: Windows uses ProactorEventLoop but psycopg needs SelectorEventLoop
if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import asyncio
from wren_agent.config import settings

async def main():
    from wren_agent.api.app import app
    import uvicorn
    config = uvicorn.Config(
        app,
        host=settings.server.host,
        port=settings.server.port,
        log_level=settings.server.log_level.lower(),
        reload=False,  # reload not compatible with this approach
    )
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())
