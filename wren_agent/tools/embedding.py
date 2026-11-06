"""Embedding client + field-semantics vectorization helpers.

Uses the DashScope OpenAI-compatible /embeddings endpoint (text-embedding-v3,
1024-dim) with the existing LLM API key — no extra infrastructure.

The embedding text for a dictionary row is:
    "{table_name}.{field_name}: {chinese_meaning} [{field_role}]"
so both the identifier and its Chinese meaning contribute to the vector.
"""

from __future__ import annotations

import logging

import httpx

from wren_agent.config import settings

logger = logging.getLogger(__name__)

_BATCH_SIZE = 10  # DashScope compatible-mode rejects batches > 10 (400)
_cache: dict[str, list[float]] = {}  # process-local, exact-text cache


def embedding_text(table_name: str, field_name: str,
                   chinese_meaning: str = "", field_role: str = "") -> str:
    """Canonical text embedded for one field-semantics row / query."""
    parts = [f"{table_name}.{field_name}"]
    if chinese_meaning:
        parts.append(f": {chinese_meaning}")
    if field_role:
        parts.append(f" [{field_role}]")
    return "".join(parts)


def registry_embedding_text(entry_type: str, **parts: str) -> str:
    """Canonical embedding text for a registry entry (metric/dimension).

    metric:     "metric_name: grain [definition] [tags]"
    dimension:  "dim_name dim_table: grain"
    The Chinese name + definition carry the semantics; the table/type adds
    context for distinguishing same-named entries across sources.
    """
    if entry_type == "metric":
        pieces = [parts.get("metric_name") or parts.get("name") or ""]
        if parts.get("grain"):
            pieces.append(f"({parts['grain']})")
        if parts.get("definition_sql"):
            pieces.append(f": {parts['definition_sql']}")
        if parts.get("tags"):
            pieces.append(f" [{parts['tags']}]")
        return " ".join(p for p in pieces if p).strip()
    # dimension
    pieces = [parts.get("dim_name") or parts.get("name") or ""]
    if parts.get("dim_table"):
        pieces.append(parts["dim_table"])
    if parts.get("grain"):
        pieces.append(f": {parts['grain']}")
    return " ".join(p for p in pieces if p).strip()


async def embed_texts(texts: list[str]) -> list[list[float] | None]:
    """Embed texts via DashScope. Returns one vector (or None on failure) per
    input, preserving order. Never raises — embedding is an enhancement, not
    a hard dependency of the pipeline.
    """
    if not settings.llm.embedding_api_base:
        logger.info("Semantic enrichment disabled: LLM_EMBEDDING_API_BASE is not configured")
        return [None] * len(texts)
    results: list[list[float] | None] = [None] * len(texts)
    todo: list[tuple[int, str]] = []
    for i, t in enumerate(texts):
        t = (t or "").strip()
        if not t:
            continue
        if t in _cache:
            results[i] = _cache[t]
        else:
            todo.append((i, t))

    if not todo:
        return results

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for start in range(0, len(todo), _BATCH_SIZE):
                batch = todo[start:start + _BATCH_SIZE]
                resp = await client.post(
                    f"{settings.llm.embedding_api_base.rstrip('/')}/embeddings",
                    headers={"Authorization": f"Bearer {settings.llm.embedding_api_key}"},
                    json={
                        "model": settings.llm.embedding_model,
                        "input": [t for _, t in batch],
                    },
                )
                resp.raise_for_status()
                data = resp.json()["data"]  # sorted by index per OpenAI spec
                by_index = {d["index"]: d["embedding"] for d in data}
                for local_idx, (orig_i, text) in enumerate(batch):
                    vec = by_index.get(local_idx)
                    if vec is not None:
                        _cache[text] = vec
                        results[orig_i] = vec
    except Exception as e:
        logger.warning("Embedding call failed (%s) — semantic fallback disabled for this batch", e)

    return results


async def backfill_pending_embeddings(
    batch_size: int = 200,
    *,
    max_rows: int | None = None,
    table_names: list[str] | None = None,
) -> int:
    """Embed field_semantics rows whose embedding is NULL. Returns rows updated.

    Called after exploration harvests new dictionary rows, and once manually
    to backfill the existing corpus.

    Guards:
      - ``max_rows`` caps work so the explore hot path cannot drain a large
        historical backlog (or hang forever).
      - ``table_names`` scopes work to tables just harvested.
      - Stops immediately when a round makes no progress (e.g. embedding API
        down) — previously a full batch of failed embeds looped forever.
    """
    from wren_agent.tools.pg_store import get_pg_pool

    pool = get_pg_pool()
    total = 0
    remaining = max_rows if max_rows is not None else None
    async with pool.acquire() as conn:
        while True:
            limit = batch_size if remaining is None else min(batch_size, remaining)
            if limit <= 0:
                break
            if table_names:
                rows = await conn.fetch(
                    "SELECT id, table_name, field_name, chinese_meaning, field_role "
                    "FROM field_semantics WHERE embedding IS NULL "
                    "AND chinese_meaning <> '' AND table_name = ANY($1::text[]) "
                    "ORDER BY id LIMIT $2",
                    table_names, limit,
                )
            else:
                rows = await conn.fetch(
                    "SELECT id, table_name, field_name, chinese_meaning, field_role "
                    "FROM field_semantics WHERE embedding IS NULL "
                    "AND chinese_meaning <> '' ORDER BY id LIMIT $1",
                    limit,
                )
            if not rows:
                break
            texts = [
                embedding_text(r["table_name"], r["field_name"],
                               r["chinese_meaning"], r["field_role"] or "")
                for r in rows
            ]
            vectors = await embed_texts(texts)
            updated = 0
            for r, vec in zip(rows, vectors):
                if vec is None:
                    continue
                await conn.execute(
                    "UPDATE field_semantics SET embedding = $1::vector WHERE id = $2",
                    str(vec), r["id"],
                )
                updated += 1
            total += updated
            if remaining is not None:
                remaining -= len(rows)
            # No progress → stop (avoids infinite retry when the API is down
            # and a full batch keeps returning NULL vectors).
            if updated == 0:
                logger.warning(
                    "Embedding backfill made no progress on %d rows — stopping",
                    len(rows),
                )
                break
            if len(rows) < limit:
                break
    if total:
        logger.info("Backfilled %d field_semantics embeddings", total)
    return total
