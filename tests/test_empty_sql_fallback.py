"""Regression: LLM designs without DDL must fall back to programmatic SQL."""

from __future__ import annotations

import pytest

from wren_agent.models.design import DWDesignPlan, ODSDesign, SQLArtifact
from wren_agent.models.schema import FieldSchema, SchemaExploration, TableSchema
from wren_agent.state import create_initial_state


def _exploration_with_table() -> dict:
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
                            name="id", data_type="BIGINT", primary_key=True,
                            nullable=False,
                        ),
                        FieldSchema(
                            name="amount", data_type="DECIMAL(10,2)",
                        ),
                        FieldSchema(
                            name="updated_at", data_type="DATETIME",
                        ),
                    ],
                    row_count_estimate=1000,
                )
            ],
        )
    }


def test_require_llm_sql_raises_when_explored_but_empty():
    from wren_agent.nodes.design_layers import _require_llm_sql_artifacts

    state = create_initial_state("t1", "goal", [], {})
    state["explorations"] = _exploration_with_table()
    with pytest.raises(ValueError, match="no designs"):
        _require_llm_sql_artifacts(state, "ODS", [], designs=[])


def test_require_llm_sql_raises_on_shell_designs():
    from wren_agent.nodes.design_layers import _require_llm_sql_artifacts

    state = create_initial_state("t1", "goal", [], {})
    state["explorations"] = _exploration_with_table()
    with pytest.raises(ValueError, match="without SQL"):
        _require_llm_sql_artifacts(
            state, "DIM", [], designs=[ODSDesign(
                source_table="t", source_db_id="db", ods_table="ods_t",
            )],
        )


def test_require_llm_sql_allows_empty_dim():
    from wren_agent.nodes.design_layers import _require_llm_sql_artifacts

    state = create_initial_state("t1", "goal", [], {})
    state["explorations"] = _exploration_with_table()
    # No dimension candidates is legitimate — do not force fallback.
    _require_llm_sql_artifacts(state, "DIM", [], designs=[])


def test_require_llm_sql_ok_when_artifacts_present():
    from wren_agent.nodes.design_layers import _require_llm_sql_artifacts

    state = create_initial_state("t1", "goal", [], {})
    state["explorations"] = _exploration_with_table()
    arts = [SQLArtifact(
        layer="ODS", target_table="ods_orders", sql_type="DDL", sql="CREATE TABLE x",
    )]
    _require_llm_sql_artifacts(state, "ODS", arts, designs=[object()])


@pytest.mark.asyncio
async def test_design_ods_falls_back_when_llm_omits_ddl(monkeypatch):
    """LLM returned design metadata without ddl → must use programmatic SQL."""
    from wren_agent.nodes import design_layers

    async def fake_llm(state, layer):
        return {
            "designs": [{
                "source_table": "orders",
                "source_db_id": "db1",
                "ods_table": "ods_orders",
                "fields": [{"name": "id", "doris_type": "BIGINT"}],
                "sync_method": "datax",
                "incremental_strategy": "full",
                # ddl intentionally missing
            }],
            "warnings": [],
        }

    async def noop_sandbox(state, layer):
        return None

    async def noop_review(state, layer):
        return None

    monkeypatch.setattr(design_layers, "_llm_generate_layer", fake_llm)
    monkeypatch.setattr(design_layers, "_sandbox_validate", noop_sandbox)
    monkeypatch.setattr(design_layers, "_llm_review_layer", noop_review)

    state = create_initial_state("t-ods-ddl", "订单汇总", [], {})
    state["explorations"] = _exploration_with_table()

    result = await design_layers.design_ods(state)
    plan = result["design_plan"]
    assert isinstance(plan, DWDesignPlan)
    assert plan.sql_artifacts, "programmatic fallback must emit ODS DDL"
    assert any(a.layer == "ODS" and a.sql_type == "DDL" for a in plan.sql_artifacts)
    assert any("programmatic fallback" in w for w in result.get("warnings", []))


@pytest.mark.asyncio
async def test_explore_light_skips_sampling_by_default(monkeypatch):
    from wren_agent.nodes import explore as explore_mod

    class FakeConnector:
        def __init__(self, **kwargs):
            pass

        async def get_tables_info(self, table_names):
            return [
                TableSchema(
                    name=table_name,
                    db_type="mysql",
                    fields=[FieldSchema(name="id", data_type="INT", primary_key=True)],
                )
                for table_name in table_names
            ]

        async def aclose(self):
            return None

    sampled = []

    async def track_sample(*_a, **_k):
        sampled.append(1)

    monkeypatch.setattr(
        "wren_agent.tools.db_explorer.DBConnector", FakeConnector,
    )
    monkeypatch.setattr(explore_mod, "_attach_safe_samples", track_sample)

    tables = await explore_mod._explore_tables_for_db(
        {
            "host": "h", "port": 1, "user": "u", "password": "p",
            "database": "db", "db_type": "mysql", "connection_params": {},
        },
        ["orders"],
    )
    assert len(tables) == 1
    assert tables[0].name == "orders"
    assert sampled == []


@pytest.mark.asyncio
async def test_explore_with_samples_propagates_sample_errors(monkeypatch):
    from wren_agent.nodes import explore as explore_mod

    class FakeConnector:
        def __init__(self, **kwargs):
            pass

        async def get_tables_info(self, table_names):
            return [
                TableSchema(
                    name=table_name,
                    db_type="mysql",
                    fields=[FieldSchema(name="id", data_type="INT", primary_key=True)],
                )
                for table_name in table_names
            ]

        async def aclose(self):
            return None

    async def boom_sample(*_a, **_k):
        raise RuntimeError("sample timeout")

    monkeypatch.setattr(
        "wren_agent.tools.db_explorer.DBConnector", FakeConnector,
    )
    monkeypatch.setattr(explore_mod, "_attach_safe_samples", boom_sample)

    with pytest.raises(RuntimeError, match="sample timeout"):
        await explore_mod._explore_tables_for_db(
            {
                "host": "h", "port": 1, "user": "u", "password": "p",
                "database": "db", "db_type": "mysql", "connection_params": {},
            },
            ["orders"],
            with_samples=True,
        )
