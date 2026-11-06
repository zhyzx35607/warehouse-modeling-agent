"""Multi-candidate Doris DML repair (task 60a570e6).

``|| '-Q'`` is OR in Doris, not concat. Repair must try several patches
instead of one hardcoded rewrite, and must not diagnose dirty BOOLEAN columns.
"""

from wren_agent.models.design import DWDesignPlan, SQLArtifact
from wren_agent.models.task import ValidationLevel, ValidationResult
from wren_agent.tools.doris_dml_repair import (
    apply_next_dml_repair,
    apply_operator_cast_candidates,
    generate_dml_repair_candidates,
    is_operator_cast_error,
)
from wren_agent.tools.dolphin_scheduler import (
    classify_execution_error,
    reconcile_llm_and_keyword_classification,
)
from wren_agent.models.task import ErrorClassification, ErrorType

PIPE_SQL = """\
INSERT INTO `ads_gz_test`.`ads_stock_inventory_quality` (`stat_quarter`, `stat_month`)
SELECT
       SUBSTRING(a.stat_month, 1, 4) || '-Q' || CEIL(CAST(SUBSTRING(a.stat_month, 6, 2) AS INT) / 3.0) AS `stat_quarter`,
       a.stat_month AS `stat_month`
FROM `dws_gz_test`.`dws_stock_inventory_monthly` a
"""

BOOL_ERR = "-Q can't cast to bool in strict mode."


def test_is_operator_cast_error():
    assert is_operator_cast_error(BOOL_ERR)
    assert is_operator_cast_error("detailMessage = -Q can't cast to bool in strict mode.")
    assert not is_operator_cast_error("can't cast to decimal in strict mode")


def test_candidates_are_plural_and_include_concat():
    cands = generate_dml_repair_candidates(PIPE_SQL, BOOL_ERR, stats={})
    labels = [c.label for c in cands]
    sqls = [c.sql for c in cands]
    assert len(cands) >= 2
    assert "pipe_concat" in labels
    assert any("CONCAT(" in s and "||" not in s for s in sqls)
    blob = " ".join(sqls).lower()
    assert "in ('true'" not in blob
    assert "in (\"true\"" not in blob


def test_next_candidate_skips_tried_labels():
    first = apply_next_dml_repair(PIPE_SQL, BOOL_ERR, tried=[], stats={})
    assert first is not None
    second = apply_next_dml_repair(PIPE_SQL, BOOL_ERR, tried=[first.label], stats={})
    assert second is not None
    assert second.label != first.label
    assert second.sql != first.sql


def test_classify_bool_cast_is_not_empty_string_guard():
    log = (
        "execute sql error: errCode = 2, detailMessage = "
        "-Q can't cast to bool in strict mode."
    )
    c = classify_execution_error(task_instance_id="13378", raw_log=log)
    assert c.error_type == ErrorType.LOGIC
    assert "concat" in (c.fix_suggestion or "").lower()
    assert "IN ('true'" not in (c.fix_suggestion or "") or "never" in (c.fix_suggestion or "").lower()


def test_reconcile_keeps_keyword_for_bool_cast():
    keyword = classify_execution_error(
        "1", "detailMessage = -Q can't cast to bool in strict mode.",
    )
    llm = ErrorClassification(
        task_instance_id="1",
        error_type=ErrorType.LOGIC,
        raw_log=keyword.raw_log,
        summary="SQL中存在无法将字符串强制转换为布尔值的类型转换错误",
        fix_suggestion="WHERE col != '' AND col IN ('true', 'false')",
        llm_classified=True,
    )
    out = reconcile_llm_and_keyword_classification(llm, keyword)
    assert out.fix_suggestion == keyword.fix_suggestion
    assert "concat" in (out.fix_suggestion or "").lower()


def test_apply_patches_dml_artifact_and_records_label():
    plan = DWDesignPlan(
        task_id="t",
        modeling_goal="g",
        sql_artifacts=[
            SQLArtifact(
                layer="ADS",
                target_table="ads_gz_test.ads_stock_inventory_quality",
                sql_type="DML_INSERT",
                sql=PIPE_SQL,
            ),
        ],
    )
    failure = ValidationResult(
        level=ValidationLevel.STRUCTURE,
        layer="ADS",
        passed=False,
        checks=[],
        failures=[{"check": "doris_operator_cast", "detail": BOOL_ERR}],
    )
    tried: dict[str, list[str]] = {}
    label = apply_operator_cast_candidates(plan, failure, tried, stats={})
    assert label
    assert "||" not in plan.sql_artifacts[0].sql
    assert "CONCAT(" in plan.sql_artifacts[0].sql
    assert tried[plan.sql_artifacts[0].target_table] == [label]


def test_bool_cast_ranks_pipe_concat_highest():
    cands = generate_dml_repair_candidates(PIPE_SQL, BOOL_ERR, stats={})
    assert cands[0].label == "pipe_concat"
    assert cands[0].p_success >= cands[1].p_success


def test_history_can_outrank_heuristic():
    stats = {
        "pipe_concat": {"wins": 0, "losses": 12},
        "concat_ws": {"wins": 20, "losses": 0},
    }
    cands = generate_dml_repair_candidates(PIPE_SQL, BOOL_ERR, stats=stats)
    assert cands[0].label == "concat_ws"


def test_explain_fail_drops_top_candidate():
    cands = generate_dml_repair_candidates(
        PIPE_SQL, BOOL_ERR, stats={},
        explain={"pipe_concat": False, "concat_ceil_int": True},
    )
    assert cands[0].label != "pipe_concat"
    assert cands[0].label == "concat_ceil_int"
