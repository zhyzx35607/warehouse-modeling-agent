"""Regression tests for sensitive sample handling."""

import json

import pytest

from wren_agent.models.schema import FieldSchema, SchemaExploration, TableSchema
from wren_agent.state import create_initial_state


SENSITIVE_VALUE = "sensitive-value-must-not-leak"


def _exploration() -> SchemaExploration:
    return SchemaExploration(
        source_db_id="source-1",
        tables=[
            TableSchema(
                name="customer",
                db_type="mysql",
                fields=[
                    FieldSchema(
                        name="phone",
                        data_type="varchar(32)",
                        sample_values=[SENSITIVE_VALUE],
                    ),
                    FieldSchema(
                        name="status",
                        data_type="varchar(16)",
                        sample_values=["active"],
                    ),
                ],
            )
        ],
    )


@pytest.mark.asyncio
async def test_exploration_queries_only_non_sensitive_columns():
    from wren_agent.nodes.explore import _attach_safe_samples

    schema = _exploration().tables[0]
    captured = {}

    class Connector:
        async def sample_data(self, table_name, limit=5, columns=None):
            captured["columns"] = columns
            return {"phone": SENSITIVE_VALUE, "status": "active"},

    await _attach_safe_samples(Connector(), schema, "customer")

    assert captured["columns"] == ["status"]
    assert schema.fields[0].is_sensitive is True
    assert schema.fields[0].sample_values == []
    assert schema.fields[1].sample_values == ["active"]


@pytest.mark.asyncio
async def test_sampling_error_does_not_retry_with_all_columns():
    from wren_agent.nodes.explore import _attach_safe_samples

    schema = _exploration().tables[0]
    calls = []

    class Connector:
        async def sample_data(self, table_name, limit=5, columns=None):
            calls.append(columns)
            raise TypeError("driver conversion failed")

    with pytest.raises(TypeError, match="driver conversion failed"):
        await _attach_safe_samples(Connector(), schema, "customer")

    assert calls == [["status"]]


@pytest.mark.asyncio
async def test_detection_clears_sensitive_samples_from_legacy_state():
    from wren_agent.nodes.explore import detect_sensitive_fields

    state = create_initial_state("task-1", "goal", [], {})
    state["explorations"] = {"source-1": _exploration()}

    result = await detect_sensitive_fields(state)

    phone = result["explorations"]["source-1"].tables[0].fields[0]
    assert phone.is_sensitive is True
    assert phone.sample_values == []
    assert SENSITIVE_VALUE not in json.dumps(
        result["explorations"]["source-1"].model_dump(), default=str,
    )


def test_design_prompt_omits_sensitive_samples_even_without_flag():
    from wren_agent.nodes.design_layers import _serialize_explorations_for_prompt

    state = create_initial_state("task-1", "goal", [], {})
    state["explorations"] = {"source-1": _exploration()}

    prompt = _serialize_explorations_for_prompt(state)

    assert SENSITIVE_VALUE not in prompt
    assert "[SENSITIVE]" in prompt
    assert "active" in prompt


def test_persistence_serialization_redacts_sensitive_samples():
    from wren_agent.tools.pg_store import _redact_sensitive_samples

    serialized = _redact_sensitive_samples({"source-1": _exploration()})
    payload = json.dumps(serialized, ensure_ascii=False, default=str)

    phone = serialized["source-1"]["tables"][0]["fields"][0]
    assert phone["is_sensitive"] is True
    assert phone["sample_values"] == []
    assert SENSITIVE_VALUE not in payload
