"""Tests for approval narratives — background LLM blocks + risk block +
card summary + narratives endpoint (migration 012, wayfinder T-01/T-02)."""

import asyncio
import json

import pytest

from wren_agent.models.design import (
    ADSDesign,
    DWDesignPlan,
    DWSDesign,
    ODSDesign,
    SQLArtifact,
)
from wren_agent.state import create_initial_state

GOAL = "基于已有 DWD 资金流水明细，产出资金净流入/流出周报指标，支撑财务周会经营分析"


def _sample_plan() -> DWDesignPlan:
    return DWDesignPlan(
        task_id="t1",
        modeling_goal=GOAL,
        ods_designs=[ODSDesign(
            source_table="src_flow", source_db_id="db1",
            ods_table="ods_transaction_flow_raw",
            fields=[{"name": "a"}, {"name": "b"}],
        )],
        dws_designs=[DWSDesign(
            summary_name="payment_flow_daily",
            summary_table="dws_payment_flow_daily",
            source_dwd_table="dwd_flow",
            measures=[{"name": "net_inflow",
                       "expression": "SUM(in_amount) - SUM(out_amount)",
                       "type": "sum"}],
            group_by_dimensions=["corp"], grain=["corp", "day"],
        )],
        ads_designs=[ADSDesign(
            application_name="weekly_report",
            ads_table="ads_payment_flow_weekly_report",
            metrics=[
                {"name": "net_inflow",
                 "expression": "SUM(in_amount) - SUM(out_amount)"},
                {"name": "avg_daily_inflow",
                 "expression": "AVG(net_inflow) PARTITION BY week",
                 "metric_registry_id": "r1"},
            ],
            target_audience="财务周会经营看板",
        )],
        sql_artifacts=[SQLArtifact(
            layer="DWS", target_table="dws_payment_flow_daily",
            sql_type="DDL", sql="CREATE TABLE ...",
        )],
    )


def _state(**overrides) -> dict:
    state = create_initial_state("t1", GOAL, [], {})
    state["design_plan"] = _sample_plan()
    state.update(overrides)
    return state


# ── Snapshot & risk block ────────────────────────────────────────


class TestSnapshot:
    def test_layer_summaries_and_metrics_dedup(self):
        from wren_agent.tools.approval_narratives import build_narrative_snapshot
        snap = build_narrative_snapshot(_state())
        layers = {ls["layer"]: ls["tables"] for ls in snap["layer_summaries"]}
        assert "ODS" in layers and "DWS" in layers and "ADS" in layers
        # net_inflow appears in both DWS measures and ADS metrics → deduped
        names = [m["name"] for m in snap["metrics"]]
        assert names == ["net_inflow", "avg_daily_inflow"]
        assert snap["metrics"][0]["layer"] == "DWS"  # first occurrence wins
        assert snap["metrics"][1]["registry_id"] == "r1"
        assert snap["sql_artifact_count"] == 1

    def test_validation_failures_collected(self):
        from wren_agent.models.task import ValidationResult, ValidationLevel
        from wren_agent.tools.approval_narratives import build_narrative_snapshot
        v = ValidationResult(level=ValidationLevel.SEMANTIC, layer="ADS",
                             passed=False, failures=[{"msg": "口径歧义"}])
        snap = build_narrative_snapshot(
            _state(validation_results=[v], all_validations_passed=False))
        assert snap["validation_failures"][0]["layer"] == "ADS"
        assert snap["validation_failures"][0]["failures"] == [{"msg": "口径歧义"}]


class TestRiskContent:
    def test_risks_markers(self):
        from wren_agent.tools.approval_narratives import (
            build_narrative_snapshot, build_risk_content,
        )
        snap = build_narrative_snapshot(_state(
            all_validations_passed=False, design_revision_count=2,
            insufficient_gaps=[{"table": "x"}], schema_drifts=[{"t": "y"}],
        ))
        risk = build_risk_content(snap)
        assert risk["revision_count"] == 2
        assert risk["insufficient_gap_count"] == 1
        assert risk["schema_drift_count"] == 1
        text = " | ".join(risk["risks"])
        assert "校验 0 项未通过" in text  # no validation rows but flag set
        assert "数仓不足以支撑缺口 1 项" in text
        assert "Schema drift 1 项" in text

    def test_no_risks_when_clean(self):
        from wren_agent.tools.approval_narratives import (
            build_narrative_snapshot, build_risk_content,
        )
        snap = build_narrative_snapshot(_state(all_validations_passed=True))
        assert build_risk_content(snap)["risks"] == []


# ── Card summary template (approval.py) ──────────────────────────


class TestDesignPlanSummary:
    def test_template_shape(self):
        from wren_agent.nodes.approval import _build_design_plan_summary
        text = _build_design_plan_summary(
            _state(all_validations_passed=True), _sample_plan())
        assert "目标: " in text and "…" not in text.splitlines()[0]
        assert "ODS(1)+DIM(0)+DWD(0)+DWS(1)+ADS(1) 共 3 表" in text
        assert "SQL 产物: 1 条" in text
        assert "新指标: 2 个" in text
        assert "⚠" not in text  # clean state → no risk markers

    def test_goal_truncated_and_risk_markers(self):
        from wren_agent.nodes.approval import _build_design_plan_summary
        long_goal = "字" * 200
        text = _build_design_plan_summary(
            _state(modeling_goal=long_goal, all_validations_passed=False,
                   design_revision_count=1, insufficient_gaps=[{"t": "x"}]),
            _sample_plan())
        first = text.splitlines()[0]
        assert len(first) == 65  # "目标: " 4 字符 + 60 字 + …
        assert first.endswith("…")
        assert "⚠ 校验未通过已修订 1 轮 / 数仓缺口 1 项" in text


# ── LLM block call ───────────────────────────────────────────────


class FakeClient:
    """generate_json inspects the user message to fake the right block."""

    def __init__(self, goal=None, metric=None, impact=None, raise_block=None):
        self.goal = goal or json.dumps({"business": "b", "technical": "t"}, ensure_ascii=False)
        self.metric = metric or json.dumps({"interpretations": [{"metric_name": "net_inflow", "interpretation": "净流入口径"}]}, ensure_ascii=False)
        self.impact = impact or json.dumps({"narrative": "n", "quantified": {"new_tables": 3}}, ensure_ascii=False)
        self.raise_block = raise_block

    async def generate_json(self, system_prompt, user_message, **kwargs):
        if "建模目标" in user_message:
            block = "goal_interpretation"
            out = self.goal
        elif "目标应用" in user_message:  # impact prompt also lists metrics
            block = "expected_impact"
            out = self.impact
        elif "指标清单" in user_message:
            block = "metric_interpretation"
            out = self.metric
        else:
            block = "expected_impact"
            out = self.impact
        if block == self.raise_block:
            raise RuntimeError("LLM timeout")
        return out


@pytest.fixture
def fake_client(monkeypatch):
    def _install(client):
        monkeypatch.setattr(
            "wren_agent.tools.approval_narratives.get_llm_client",
            lambda: client)
        return client
    return _install


class TestCallBlock:
    @pytest.mark.asyncio
    async def test_parses_json(self, fake_client):
        from wren_agent.tools.approval_narratives import (
            _call_block, build_narrative_snapshot,
        )
        fake_client(FakeClient())
        snap = build_narrative_snapshot(_state())
        content = await _call_block("goal_interpretation", snap)
        assert content == {"business": "b", "technical": "t"}

    @pytest.mark.asyncio
    async def test_bad_json_raises(self, fake_client):
        from wren_agent.tools.approval_narratives import (
            _call_block, build_narrative_snapshot,
        )
        fake_client(FakeClient(goal="not json"))
        with pytest.raises(ValueError):
            await _call_block("goal_interpretation", build_narrative_snapshot(_state()))


# ── Background generation ────────────────────────────────────────


class TestGenerateBlocks:
    @pytest.mark.asyncio
    async def test_all_blocks_ready(self, fake_client, monkeypatch):
        from wren_agent.tools import approval_narratives as m

        async def _sleep(delay):
            return None
        monkeypatch.setattr("asyncio.sleep", _sleep)

        written = []
        async def _upsert(task_id, approval_id, block, status, content=None,
                          error="", attempts=0):
            written.append((block, status, attempts, error))
        monkeypatch.setattr(
            "wren_agent.tools.approval_narratives.upsert_approval_narrative",
            _upsert)

        fake_client(FakeClient())
        snap = m.build_narrative_snapshot(_state())
        await m.generate_approval_narratives("t1", "a1", snap)
        assert {w[0] for w in written} == {
            "goal_interpretation", "metric_interpretation", "expected_impact"}
        assert all(w[1] == "ready" and w[2] == 1 for w in written)

    @pytest.mark.asyncio
    async def test_failing_block_degrades_others_ready(self, fake_client, monkeypatch):
        from wren_agent.tools import approval_narratives as m

        async def _sleep(delay):
            return None
        monkeypatch.setattr("asyncio.sleep", _sleep)

        written = []
        async def _upsert(task_id, approval_id, block, status, content=None,
                          error="", attempts=0):
            written.append((block, status, attempts, error))
        monkeypatch.setattr(
            "wren_agent.tools.approval_narratives.upsert_approval_narrative",
            _upsert)

        fake_client(FakeClient(raise_block="metric_interpretation"))
        snap = m.build_narrative_snapshot(_state())
        await m.generate_approval_narratives("t1", "a1", snap)
        by_block = {w[0]: w for w in written}
        assert by_block["metric_interpretation"][1] == "failed"
        assert by_block["metric_interpretation"][2] == 3  # 1 + 2 retries
        assert "LLM timeout" in by_block["metric_interpretation"][3]  # error kept
        assert by_block["goal_interpretation"][1] == "ready"
        assert by_block["expected_impact"][1] == "ready"


class TestFire:
    @pytest.mark.asyncio
    async def test_risk_persisted_and_task_launched(self, monkeypatch):
        from wren_agent.tools import approval_narratives as m

        risk_calls = []
        async def _upsert(task_id, approval_id, block, status, content=None,
                          error="", attempts=0):
            risk_calls.append((approval_id, block, status, content))
        monkeypatch.setattr(
            "wren_agent.tools.approval_narratives.upsert_approval_narrative",
            _upsert)

        launched = []
        def _fake_task(coro):
            launched.append(coro)
            return coro
        monkeypatch.setattr(
            "wren_agent.tools.approval_narratives.asyncio.create_task",
            _fake_task)

        await m.fire_approval_narratives(_state(), "a1")
        assert risk_calls and risk_calls[0][1] == "risk"
        assert risk_calls[0][2] == "ready"
        assert risk_calls[0][3]["revision_count"] == 0
        assert launched, "background generation task must be scheduled"
        # Consume the captured coroutine so pytest sees no un-awaited coro
        await launched[0]


# ── Narratives endpoint ──────────────────────────────────────────


class FakeConn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def __init__(self, exists):
        self._exists = exists

    async def fetchrow(self, query, *args):
        return self._exists


class FakePool:
    def __init__(self, exists):
        self._exists = exists

    def acquire(self):
        return FakeConn(self._exists)


class TestNarrativesEndpoint:
    @pytest.mark.asyncio
    async def test_returns_blocks_and_metric_status(self, monkeypatch):
        from wren_agent.api import routes
        from wren_agent.tools import pg_store

        monkeypatch.setattr(pg_store, "get_pg_pool", lambda: FakePool(exists={"1": 1}))

        async def _load_narratives(aid):
            return [
                {"block": "risk", "status": "ready",
                 "content_json": {"risks": ["x"]}, "error": "", "attempts": 0},
                {"block": "expected_impact", "status": "ready",
                 "content_json": {"narrative": "n"}, "error": "", "attempts": 1},
            ]
        monkeypatch.setattr(pg_store, "load_approval_narratives", _load_narratives)

        async def _registry(name=None):
            return [
                {"metric_name": "net_inflow", "metric_id": "mid1", "active": True},
            ]
        monkeypatch.setattr(pg_store, "query_metric_registry", _registry)

        # Real path: the checkpointer returns the pydantic MODEL, not a dict —
        # the endpoint must normalize (regression: AttributeError on .get()).
        async def _load(task_id):
            return _sample_plan()
        monkeypatch.setattr(routes, "_load_design_plan", _load)

        result = await routes.get_approval_narratives("t1", "a1")
        by_block = {b["block"]: b for b in result["blocks"]}
        assert by_block["risk"]["status"] == "ready"
        assert by_block["expected_impact"]["status"] == "ready"
        # Missing blocks default to pending
        assert by_block["goal_interpretation"]["status"] == "pending"
        assert by_block["metric_interpretation"]["status"] == "pending"
        status = {m["name"]: m for m in result["metric_status"]}
        assert status["net_inflow"]["registered"] is True
        assert status["net_inflow"]["registry_id"] == "mid1"
        # ADS metric with no registry row but design-time registry_id → registered
        assert status["avg_daily_inflow"]["registered"] is True

    @pytest.mark.asyncio
    async def test_404_when_approval_missing(self, monkeypatch):
        from wren_agent.api import routes
        from wren_agent.tools import pg_store
        from fastapi import HTTPException

        monkeypatch.setattr(pg_store, "get_pg_pool", lambda: FakePool(exists=None))
        with pytest.raises(HTTPException) as exc:
            await routes.get_approval_narratives("t1", "missing")
        assert exc.value.status_code == 404
