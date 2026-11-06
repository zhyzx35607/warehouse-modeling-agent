"""CLI interface for the Wren Modeling Agent.

Connects via the same REST + WebSocket backend as the Dashboard.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Optional

import click
import httpx
import websockets


@click.group()
def cli():
    """Wren Modeling Agent CLI — create and monitor data warehouse modeling tasks."""
    pass


# ── Task management ─────────────────────────────────────────────


@cli.command()
@click.option("--goal", "-g", required=True, help="Modeling goal description")
@click.option("--source", "-s", multiple=True, help="Source DB config JSON string")
@click.option("--dw-target", "-d", required=True, help="Doris target config JSON string")
@click.option("--api-url", default="http://localhost:8000", help="API server URL")
def create(goal: str, source: tuple, dw_target: str, api_url: str):
    """Create a new modeling task."""
    source_dbs = [json.loads(s) for s in source]
    response = httpx.post(
        f"{api_url}/api/v1/tasks",
        json={
            "modeling_goal": goal,
            "source_databases": source_dbs,
            "dw_target": json.loads(dw_target),
        },
        timeout=30,
    )
    result = response.json()
    click.echo(f"Task created: {result['task_id']}")


@cli.command()
@click.argument("task_id")
@click.option("--api-url", default="http://localhost:8000", help="API server URL")
def status(task_id: str, api_url: str):
    """Get task status."""
    response = httpx.get(f"{api_url}/api/v1/tasks/{task_id}", timeout=10)
    result = response.json()
    click.echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command()
@click.argument("task_id")
@click.option("--api-url", default="http://localhost:8000", help="API server URL")
def cancel(task_id: str, api_url: str):
    """Cancel a task."""
    response = httpx.post(f"{api_url}/api/v1/tasks/{task_id}/cancel", timeout=10)
    click.echo(f"Task {task_id} cancellation: {response.json()}")


# ── Clarifications ──────────────────────────────────────────────


@cli.command()
@click.argument("task_id")
@click.option("--api-url", default="http://localhost:8000", help="API server URL")
def clarifications(task_id: str, api_url: str):
    """List pending clarifications."""
    response = httpx.get(f"{api_url}/api/v1/tasks/{task_id}/clarifications", timeout=10)
    items = response.json()
    if not items:
        click.echo("No pending clarifications.")
        return
    for item in items:
        blocking = " [BLOCKING]" if item.get("blocking") else ""
        click.echo(f"\n--- {item['id']}{blocking} ---")
        click.echo(f"Q: {item['question']}")
        click.echo(f"Context: {item['context']}")
        if item.get("candidates"):
            for i, c in enumerate(item["candidates"], 1):
                click.echo(f"  [{i}] {c}")


@cli.command()
@click.argument("task_id")
@click.argument("clarification_id")
@click.argument("answer")
@click.option("--api-url", default="http://localhost:8000", help="API server URL")
def answer(task_id: str, clarification_id: str, answer: str, api_url: str):
    """Answer a clarification question."""
    response = httpx.post(
        f"{api_url}/api/v1/tasks/{task_id}/clarifications",
        json={"clarification_id": clarification_id, "answer": answer},
        timeout=10,
    )
    click.echo(response.json())


# ── Approval ────────────────────────────────────────────────────


@cli.command()
@click.argument("task_id")
@click.option("--api-url", default="http://localhost:8000", help="API server URL")
def approvals(task_id: str, api_url: str):
    """List pending approval items."""
    response = httpx.get(f"{api_url}/api/v1/tasks/{task_id}/approvals", timeout=10)
    items = response.json()
    if not items:
        click.echo("No pending approvals.")
        return
    for item in items:
        click.echo(f"\n--- {item['id']} ---")
        click.echo(f"Title: {item['title']}")
        click.echo(f"Description: {item['description']}")
        click.echo(f"Type: {item['artifact_type']}")


@cli.command()
@click.argument("task_id")
@click.argument("approval_id")
@click.option("--approve/--reject", default=True, help="Approve or reject")
@click.option("--comment", default="", help="Approval comment")
@click.option("--api-url", default="http://localhost:8000", help="API server URL")
def approve(task_id: str, approval_id: str, approve: bool, comment: str, api_url: str):
    """Approve or reject an approval item."""
    response = httpx.post(
        f"{api_url}/api/v1/tasks/{task_id}/approvals",
        json={"approval_id": approval_id, "approved": approve, "comment": comment},
        timeout=10,
    )
    click.echo(response.json())


# ── Live monitoring (WebSocket) ─────────────────────────────────


@cli.command()
@click.argument("task_id")
@click.option("--ws-url", default="ws://localhost:8000", help="WebSocket server URL")
def watch(task_id: str, ws_url: str):
    """Watch a task in real-time via WebSocket stream."""
    asyncio.run(_watch_task(task_id, ws_url))


async def _watch_task(task_id: str, ws_url: str):
    """Async WebSocket listener that prints events as they arrive."""
    url = f"{ws_url}/ws/tasks/{task_id}"
    try:
        async with websockets.connect(url) as ws:
            # Print connected confirmation
            msg = await ws.recv()
            click.echo(json.loads(msg)["message"])

            # Listen for events
            async for message in ws:
                event = json.loads(message)
                event_type = event.get("type", "unknown")
                data = event.get("data", {})

                if event_type == "thought_stream":
                    click.echo(f"  [thinking] {data.get('content', '')}", nl=False)
                elif event_type == "sql_execution":
                    click.echo(f"\n[SQL] {data.get('sql', '')[:200]}...")
                elif event_type == "task_status_change":
                    click.echo(f"\n[STATUS] {data.get('old')} -> {data.get('new')}")
                elif event_type == "phase_change":
                    click.echo(f"\n[PHASE] {data.get('phase')}")
                elif event_type == "clarification_added":
                    click.echo(f"\n[QUESTION] {data.get('question', '')}")
                elif event_type == "error":
                    click.echo(f"\n[ERROR] {data.get('message', '')}")
                elif event_type == "warning":
                    click.echo(f"\n[WARN] {data.get('message', '')}")
                else:
                    # Generic event
                    click.echo(f"\n[{event_type}] {json.dumps(data, ensure_ascii=False)[:200]}")

    except websockets.exceptions.ConnectionClosed:
        click.echo("\nConnection closed.")
    except Exception as e:
        click.echo(f"\nError: {e}")


# ── Artifacts ───────────────────────────────────────────────────


@cli.command()
@click.argument("task_id")
@click.option("--api-url", default="http://localhost:8000", help="API server URL")
def report(task_id: str, api_url: str):
    """Get the construction report."""
    response = httpx.get(f"{api_url}/api/v1/tasks/{task_id}/report", timeout=10)
    click.echo(response.text)


@cli.command()
@click.argument("task_id")
@click.option("--api-url", default="http://localhost:8000", help="API server URL")
def design(task_id: str, api_url: str):
    """Get the DW design plan."""
    response = httpx.get(f"{api_url}/api/v1/tasks/{task_id}/design-plan", timeout=10)
    click.echo(json.dumps(response.json(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    cli()
