"""Tests for agent runtime performance optimizations."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from wren_agent.models.design import DWDesignPlan, ODSDesign, SQLArtifact
from wren_agent.models.schema import FieldSchema, SchemaExploration, TableSchema
from wren_agent.state import create_initial_state


def test_skill_load_is_cached():
    from wren_agent.skills import clear_skill_cache, load_skill

    clear_skill_cache()
    a = load_skill("grill-expert")
    b = load_skill("grill-expert")
    assert a and b
    assert a["content"] == b["content"]
    assert a is not b  # defensive copy via dict rebuild
    from wren_agent.skills import _load_skill_cached

    assert _load_skill_cached.cache_info().hits >= 1


def test_tokens_for_kinds():
    from wren_agent.config import settings

    assert settings.llm.tokens_for("review") == settings.llm.max_tokens_review
    assert settings.llm.tokens_for("design") == settings.llm.max_tokens_design
    assert settings.llm.tokens_for("unknown") == settings.llm.max_tokens


def test_schema_prompt_is_cached_and_capped():
    from wren_agent.nodes.design_layers import (
        _MAX_FIELDS_PER_TABLE,
        _serialize_explorations_for_prompt,
    )

    fields = [
        FieldSchema(name=f"c{i}", data_type="INT", nullable=True)
        for i in range(_MAX_FIELDS_PER_TABLE + 10)
    ]
    fields[0].primary_key = True
    table = TableSchema(
        name="big_table",
        db_type="mysql",
        fields=fields,
    )
    state = create_initial_state("t-cache", "goal", [], {})
    state["explorations"] = {
        "db1": SchemaExploration(source_db_id="db1", phase="light", tables=[table]),
    }

    first = _serialize_explorations_for_prompt(state)
    assert state.get("_schema_prompt_cache") == first
    assert "… (10 more columns omitted)" in first
    second = _serialize_explorations_for_prompt(state)
    assert second is first or second == first


@pytest.mark.asyncio
async def test_layer_review_gated_off_by_default(monkeypatch):
    from wren_agent.config import settings
    from wren_agent.nodes.design_layers import _llm_review_layer

    monkeypatch.setattr(settings.llm, "layer_review", False)
    called = []

    def boom():
        called.append(True)
        raise AssertionError("LLM must not be called when layer_review is false")

    monkeypatch.setattr("wren_agent.tools.llm_client.get_llm_client", boom)

    state = create_initial_state("t-review", "goal", [], {})
    state["design_plan"] = DWDesignPlan(
        task_id="t-review",
        modeling_goal="goal",
        ods_designs=[
            ODSDesign(
                ods_table="ods_t",
                source_table="t",
                source_db_id="db",
                sync_method="datax",
            )
        ],
    )
    await _llm_review_layer(state, "ODS")
    assert called == []


@pytest.mark.asyncio
async def test_inject_field_semantics_reuses_grill_cache(monkeypatch):
    from wren_agent.nodes.design_layers import _inject_field_semantics

    async def should_not_query(*_a, **_k):
        raise AssertionError("must not re-query PG when field_semantics is present")

    monkeypatch.setattr(
        "wren_agent.tools.pg_store.query_field_semantics", should_not_query
    )

    state = create_initial_state("t-sem", "goal", [], {})
    state["field_semantics"] = {
        "orders": {
            "amt": {
                "chinese_meaning": "金额",
                "field_role": "measure",
            }
        }
    }
    context = {"user_message": "base"}
    await _inject_field_semantics(state, context)
    assert "金额" in context["user_message"]
    assert state.get("_field_semantics_prompt")
    # Second inject uses prompt cache only
    context2 = {"user_message": "base2"}
    await _inject_field_semantics(state, context2)
    assert context2["user_message"].endswith(state["_field_semantics_prompt"])


@pytest.mark.asyncio
async def test_semantic_assertions_batch_one_llm_call(monkeypatch):
    from wren_agent.nodes import validation as validation_mod

    calls = []

    class FakeLLM:
        async def generate_json(self, *args, **kwargs):
            calls.append(kwargs.get("token_kind"))
            return (
                '{"by_artifact": ['
                '{"index": 0, "assertions": [{"metric": "m0", "expected_range": ">0", "source": "x"}]},'
                '{"index": 1, "assertions": [{"metric": "m1", "expected_range": ">0", "source": "y"}]}'
                "]}"
            )

    monkeypatch.setattr(
        "wren_agent.tools.llm_client.get_llm_client", lambda: FakeLLM()
    )

    arts = [
        SQLArtifact(layer="DWS", sql_type="DDL", target_table="dws_a", sql="SELECT 1"),
        SQLArtifact(layer="ADS", sql_type="DDL", target_table="ads_b", sql="SELECT 1"),
    ]
    state = create_initial_state("t-batch", "goal", [], {})
    results = await validation_mod._generate_semantic_assertions_batch(arts, state)
    assert len(calls) == 1
    assert calls[0] == "semantic"
    assert len(results) == 2
    assert results[0].semantic_assertions[0]["metric"] == "m0"
    assert results[1].semantic_assertions[0]["metric"] == "m1"


@pytest.mark.asyncio
async def test_explore_tables_reuses_one_connector(monkeypatch):
    from wren_agent.nodes import explore as explore_mod

    created = []

    class FakeConnector:
        def __init__(self, **kwargs):
            created.append(1)

        async def get_tables_info(self, table_names):
            return [
                TableSchema(
                    name=table_name,
                    db_type="mysql",
                    fields=[FieldSchema(
                        name="id", data_type="INT", nullable=False, primary_key=True,
                    )],
                )
                for table_name in table_names
            ]

        async def aclose(self):
            return None

    monkeypatch.setattr(
        "wren_agent.tools.db_explorer.DBConnector", FakeConnector
    )

    sdb = {
        "host": "h", "port": 1, "user": "u", "password": "p",
        "database": "db", "db_type": "mysql", "connection_params": {},
    }
    tables = await explore_mod._explore_tables_for_db(
        sdb, [f"t{i}" for i in range(5)],
    )
    assert len(tables) == 5
    assert len(created) == 1  # one connector for the whole DB
