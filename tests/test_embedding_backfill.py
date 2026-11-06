"""Regression: embedding backfill must not hang schema exploration."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_backfill_stops_when_embeds_fail(monkeypatch):
    """Full batch of failed embeds used to loop forever (same NULL rows)."""
    from wren_agent.tools import embedding as emb

    class FakeConn:
        def __init__(self):
            self.fetch_calls = 0

        async def fetch(self, sql, *args):
            self.fetch_calls += 1
            # Always return a "full" batch so the old code would never break
            return [
                {
                    "id": i,
                    "table_name": "t",
                    "field_name": f"c{i}",
                    "chinese_meaning": "含义",
                    "field_role": "",
                }
                for i in range(5)
            ]

        async def execute(self, *args, **kwargs):
            raise AssertionError("must not UPDATE when all embeds fail")

    class FakeAcquire:
        def __init__(self, conn):
            self.conn = conn

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, *args):
            return False

    class FakePool:
        def __init__(self, conn):
            self.conn = conn

        def acquire(self):
            return FakeAcquire(self.conn)

    conn = FakeConn()
    monkeypatch.setattr(
        "wren_agent.tools.pg_store.get_pg_pool", lambda: FakePool(conn)
    )

    async def fail_all(texts):
        return [None] * len(texts)

    monkeypatch.setattr(emb, "embed_texts", fail_all)

    updated = await emb.backfill_pending_embeddings(batch_size=5)
    assert updated == 0
    assert conn.fetch_calls == 1  # stopped after no-progress round


@pytest.mark.asyncio
async def test_backfill_respects_max_rows(monkeypatch):
    from wren_agent.tools import embedding as emb

    class FakeConn:
        def __init__(self):
            self.updated = []

        async def fetch(self, sql, *args):
            limit = args[-1]
            start = len(self.updated)
            return [
                {
                    "id": start + i,
                    "table_name": "t",
                    "field_name": f"c{start + i}",
                    "chinese_meaning": "含义",
                    "field_role": "",
                }
                for i in range(limit)
            ]

        async def execute(self, sql, vec, row_id):
            self.updated.append(row_id)

    class FakeAcquire:
        def __init__(self, conn):
            self.conn = conn

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, *args):
            return False

    class FakePool:
        def __init__(self, conn):
            self.conn = conn

        def acquire(self):
            return FakeAcquire(self.conn)

    conn = FakeConn()
    monkeypatch.setattr(
        "wren_agent.tools.pg_store.get_pg_pool", lambda: FakePool(conn)
    )

    async def ok(texts):
        return [[0.1] * 4 for _ in texts]

    monkeypatch.setattr(emb, "embed_texts", ok)

    updated = await emb.backfill_pending_embeddings(batch_size=10, max_rows=3)
    assert updated == 3
    assert conn.updated == [0, 1, 2]


@pytest.mark.asyncio
async def test_schema_explore_does_not_await_full_backfill(monkeypatch):
    """Harvest may schedule a background embed, but must not block on corpus drain."""
    from wren_agent.nodes import explore as explore_mod
    from wren_agent.models.schema import FieldSchema, SchemaExploration, TableSchema
    from wren_agent.state import create_initial_state

    async def fake_business(state):
        return {
            "db1": SchemaExploration(
                source_db_id="db1",
                phase="light",
                tables=[
                    TableSchema(
                        name="orders",
                        db_type="mysql",
                        fields=[
                            FieldSchema(
                                name="amt",
                                data_type="DECIMAL",
                                comment="金额",
                            )
                        ],
                    )
                ],
            )
        }

    monkeypatch.setattr(explore_mod, "_explore_business_dbs", fake_business)

    upsert_calls = []

    async def fake_upsert(rows):
        upsert_calls.append(len(rows))
        return len(rows)

    monkeypatch.setattr(
        "wren_agent.tools.pg_store.upsert_field_semantics_from_comments",
        fake_upsert,
    )

    import asyncio

    backfill_started = []
    release = asyncio.Event()

    async def gated_backfill(**kwargs):
        backfill_started.append(kwargs)
        await release.wait()  # would hang explore if awaited on critical path
        return 0

    monkeypatch.setattr(
        "wren_agent.tools.embedding.backfill_pending_embeddings",
        gated_backfill,
    )

    state = create_initial_state("t-fast", "goal", [{"id": "db1"}], {})
    result = await asyncio.wait_for(
        explore_mod.schema_explore_light(state), timeout=2.0,
    )
    assert upsert_calls == [1]
    assert sum(len(e.tables) for e in result["explorations"].values()) == 1
    await asyncio.sleep(0)  # let create_task start
    assert backfill_started
    assert backfill_started[0].get("table_names") == ["orders"]
    assert backfill_started[0].get("max_rows") == 1
    release.set()
    await asyncio.sleep(0)
