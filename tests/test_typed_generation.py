"""Tests for the structured-output hardening (建议2 / ADR-0015 Phase 0).

Covers:
- `LLMClient.generate_typed_json`: schema validation + one regeneration with
  validation-error feedback (an off-schema response must not silently parse
  into an empty design).
- `_require_recognized_output`: design parsers reject non-empty responses
  with none of the layer's known keys instead of swallowing them.
- `LayerPlanOutput` / `_plan_layer_tables`: plan-call aliases flatten, and a
  full-design response (task ab8fadcf regression) is detected and passed
  through instead of parsing to an empty plan.
"""

import asyncio

import pytest

from wren_agent.models.llm_outputs import LayerPlanOutput, PlanTableEntry, RepairOutput


# ── LayerPlanOutput schema behaviors ────────────────────────────────


class TestLayerPlanOutput:
    def test_table_names_flatten_strings_and_entries(self):
        data = LayerPlanOutput(
            dws_tables=[
                "dws_a",
                PlanTableEntry(table="dws_b", grain=["g1"]),
            ],
            ads_tables=[{"ads_table": "ads_x"}],
        )
        assert data.table_names() == ["dws_a", "dws_b", "ads_x"]

    def test_garbage_entries_rejected_by_schema(self):
        with pytest.raises(Exception):
            LayerPlanOutput.model_validate({"dws_tables": [{"table": 123}]})

    def test_complete_design_detection(self):
        plan_only = LayerPlanOutput(dws_tables=[PlanTableEntry(table="dws_a")])
        assert plan_only.complete_designs_present is False
        full = LayerPlanOutput(
            dws_designs=[{"summary_table": "dws_a", "ddl": "CREATE ...", "load_sql": "INSERT ..."}]
        )
        assert full.complete_designs_present is True


# ── _require_recognized_output (parser guard) ──────────────────────


class TestRequireRecognizedOutput:
    @pytest.fixture
    def guard(self):
        from wren_agent.nodes.design_layers import _require_recognized_output
        return _require_recognized_output

    def test_unknown_keys_raise(self, guard):
        with pytest.raises(ValueError, match="no recognized keys"):
            guard({"dws": [{"summary_table": "x"}]}, "DWS")

    def test_empty_and_warnings_only_are_valid(self, guard):
        guard({}, "DWS")
        guard({"warnings": ["nothing to design"]}, "DWS")

    def test_known_keys_pass(self, guard):
        guard({"dws_designs": []}, "DWS")
        guard({"designs": []}, "ADS")
        guard({"skip": True}, "ADS")


# ── generate_typed_json: validation + regeneration ─────────────────


class FakeStreamModel:
    """Stub ChatModel whose astream yields canned responses in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def astream(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        resp = self.responses.pop(0) if self.responses else ""
        yield type("Chunk", (), {"content": resp})()

    async def ainvoke(self, messages, **kwargs):
        raise AssertionError("ainvoke must not be used when astream works")


@pytest.fixture
def llm(monkeypatch):
    from wren_agent.tools.llm_client import LLMClient
    client = LLMClient()
    model = FakeStreamModel([])
    client._model = model
    return client, model


class TestGenerateTypedJson:
    @pytest.mark.asyncio
    async def test_valid_first_response(self, llm):
        client, model = llm
        model.responses = ['{"repairs": [{"target_table": "t1", "sql_type": "DDL", "sql": "CREATE TABLE t1"}]}']
        result = await client.generate_typed_json(
            "sys", "user", RepairOutput, token_kind="repair",
        )
        assert result.repairs[0].target_table == "t1"
        assert len(model.calls) == 1  # no regeneration

    @pytest.mark.asyncio
    async def test_off_schema_response_regenerates_with_feedback(self, llm):
        client, model = llm
        # First response: valid JSON but wrong shape (not {"repairs": [...]})
        model.responses = [
            '{"dws_designs": [{"summary_table": "x"}]}',
            '{"repairs": [{"target_table": "t1", "metadata": {"fields": ["a"]}}]}',
        ]
        result = await client.generate_typed_json(
            "sys", "user", RepairOutput, token_kind="repair",
        )
        assert result.repairs[0].target_table == "t1"
        assert len(model.calls) == 2
        # the regeneration user message carries the validation error
        second_user = model.calls[1][0][-1].content
        assert "failed validation" in second_user

    @pytest.mark.asyncio
    async def test_regenerate_disabled_raises(self, llm):
        client, model = llm
        model.responses = ['{"unrelated": true}']
        with pytest.raises(Exception):
            await client.generate_typed_json(
                "sys", "user", RepairOutput, token_kind="repair", regenerate=False,
            )
        assert len(model.calls) == 1


# ── _plan_layer_tables: full-design passthrough (task ab8fadcf) ─────


class FakePlanLLM:
    """Fake get_llm_client result whose generate_typed_json returns the plan
    output the test configures — mirrors the real typed call."""

    def __init__(self, output: LayerPlanOutput):
        self.output = output
        self.calls = 0

    async def generate_typed_json(self, system_prompt, user_message, schema, **kwargs):
        self.calls += 1
        return self.output


@pytest.mark.asyncio
async def test_plan_layer_tables_passes_through_full_design(monkeypatch):
    """Regression ab8fadcf: the LLM returns a COMPLETE design instead of the
    small plan — it must be detected and returned for direct use, not parsed
    as an empty plan (which used to raise insufficient_warehouse)."""
    from wren_agent.nodes import design_layers as dl
    from wren_agent.state import create_initial_state

    state = create_initial_state("t-plan-full", "goal", [], {}, source_mode="warehouse")
    fake = FakePlanLLM(LayerPlanOutput(
        dws_designs=[{
            "summary_table": "dws_transaction_flow_daily",
            "summary_name": "daily flow",
            "grain": ["trans_date", "company_id"],
            "ddl": "CREATE TABLE IF NOT EXISTS dws_transaction_flow_daily (...) UNIQUE KEY(trans_date, company_id)",
            "load_sql": "INSERT INTO dws_transaction_flow_daily (trans_date, company_id) SELECT ...",
        }],
    ))
    monkeypatch.setattr("wren_agent.tools.llm_client.get_llm_client", lambda: fake)
    monkeypatch.setattr(dl, "_inject_field_semantics", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(dl, "_inject_registry_context", lambda *a, **k: asyncio.sleep(0))

    names, full = await dl._plan_layer_tables(state, "DWS")

    assert names == ["dws_transaction_flow_daily"]
    assert full  # complete designs present → used directly
    assert full["dws_designs"][0]["ddl"].startswith("CREATE TABLE")
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_plan_layer_tables_names_only(monkeypatch):
    """The normal planning path returns names and no full result."""
    from wren_agent.nodes import design_layers as dl
    from wren_agent.state import create_initial_state

    state = create_initial_state("t-plan-names", "goal", [], {}, source_mode="warehouse")
    fake = FakePlanLLM(LayerPlanOutput(
        dws_tables=[{"table": "dws_a", "grain": ["g1"]}, "dws_b"],
    ))
    monkeypatch.setattr("wren_agent.tools.llm_client.get_llm_client", lambda: fake)
    monkeypatch.setattr(dl, "_inject_field_semantics", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(dl, "_inject_registry_context", lambda *a, **k: asyncio.sleep(0))

    names, full = await dl._plan_layer_tables(state, "DWS")

    assert names == ["dws_a", "dws_b"]
    assert full == {}


# ── Regression: real extraction path is reachable (no stale names) ───


class FakeJsonLLM:
    """Fake get_llm_client whose generate_json returns valid JSON — the real
    `_llm_generate_layer` / `_generate_with_compact_retry` paths must extract
    it without NameError (the extractor was moved to tools/json_utils and a
    stale call-site name broke every real task while unit tests mocked the
    LLM layer)."""

    def __init__(self, payload: str):
        self.payload = payload

    async def generate_json(self, *args, **kwargs):
        return self.payload


@pytest.mark.asyncio
async def test_llm_generate_layer_real_extraction_path(monkeypatch):
    from wren_agent.nodes import design_layers as dl
    from wren_agent.state import create_initial_state

    state = create_initial_state("t-extract", "goal", [], {})
    monkeypatch.setattr(
        "wren_agent.tools.llm_client.get_llm_client",
        lambda: FakeJsonLLM('{"designs": [{"ods_table": "ods_x"}], "warnings": []}'),
    )
    result = await dl._llm_generate_layer(state, "ODS")
    assert result["designs"][0]["ods_table"] == "ods_x"


@pytest.mark.asyncio
async def test_generate_with_compact_retry_real_extraction_path(monkeypatch):
    from wren_agent.nodes import design_layers as dl
    from wren_agent.state import create_initial_state

    state = create_initial_state("t-compact", "goal", [], {})
    monkeypatch.setattr(
        "wren_agent.tools.llm_client.get_llm_client",
        lambda: FakeJsonLLM('{"dws_designs": [], "warnings": []}'),
    )
    result = await dl._generate_with_compact_retry(state, "DWS")
    assert result == {"dws_designs": [], "warnings": []}
