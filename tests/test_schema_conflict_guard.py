"""Regression tests for the existing-table schema-conflict guards.

Task 3034d2f8: `CREATE TABLE IF NOT EXISTS` no-oped over an already-existing
`dws_category_sales_daily_monthly` (built by an earlier run with a
different schema), so the DML INSERT referencing `monthly_charge_count`
failed at execution with `column ... is not found in table ...`, and the
error classifier escalated it as an upstream schema change instead of the
agent's own SQL vs an existing table.

Three guards:
1. `_probe_execute_layer_ddls` + `_detect_existing_table_conflict` — compare
   the REAL table against the planned DDL columns. Identifier stripping
   must survive backticked qualified names (task d3760a4f).
2. `_doris_explain_load_sqls` + `_check_insert_target_columns` — DESC the
   INSERT target and diff the explicit target columns, including
   plan-internal names that already exist (task d3760a4f leftover table).
3. `classify_errors` + `_verify_column_not_found_against_real_schema` —
   re-check DEPENDENCY against the real schema before escalating.
"""

from __future__ import annotations

import pytest

from wren_agent.models.design import DWDesignPlan, SQLArtifact
from wren_agent.models.task import ErrorClassification, ErrorType
from wren_agent.state import create_initial_state


def _warehouse_state(task_id: str = "t") -> dict:
    return create_initial_state(task_id, "goal", [{
        "source_db_id": "wh",
        "connection_params": {
            "layers": {"dws": ["dws_ecommerce_orders_sh"], "ads": ["db_ads"]},
        },
    }], {}, source_mode="warehouse")


def _real_columns(*names: str) -> dict:
    return {
        "table_name": "t",
        "database": "db",
        "columns": [{"name": n, "type": "VARCHAR(255)"} for n in names],
        "column_count": len(names),
        "primary_keys": [],
    }


# ── Guard 1: DDL probe schema-conflict detection ─────────────


class _ConflictDoris:
    """Fake DorisClient for the existing-table conflict probes."""

    def __init__(self, database=None, *, existing: dict[str, set[str]] | None = None):
        self.database = database
        self._existing = existing or {}
        self.calls = []

    async def execute(self, sql: str) -> dict:
        self.calls.append(("execute", sql))
        return {"success": True, "affected_rows": 0}

    async def table_exists(self, table_name: str) -> bool:
        self.calls.append(("table_exists", table_name))
        return table_name in self._existing

    async def get_table_schema(self, table_name: str) -> dict:
        self.calls.append(("get_table_schema", table_name))
        return _real_columns(*self._existing[table_name])

    async def query(self, sql, params=()):
        self.calls.append(("query", sql))
        return []


@pytest.mark.asyncio
async def test_ddl_probe_flags_existing_table_missing_column(monkeypatch):
    """IF NOT EXISTS over an existing table missing a planned column must
    surface as ddl_probe_schema_conflict with the real columns in detail."""
    from wren_agent.nodes.validation import _probe_execute_layer_ddls

    fake = _ConflictDoris(
        existing={"dws_category_sales_daily_monthly": {
            "org_id", "dept_id", "group_id", "stat_month",
            "monthly_water_receivable", "monthly_total_paid",
        }},
    )
    monkeypatch.setattr(
        "wren_agent.tools.doris_client.DorisClient",
        lambda database=None: fake,
    )

    state = _warehouse_state()
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.sql_artifacts = [
        SQLArtifact(
            layer="DWS", target_table="dws_category_sales_daily_monthly",
            sql_type="DDL",
            sql=(
                "CREATE TABLE IF NOT EXISTS "
                "db_dws.dws_category_sales_daily_monthly (\n"
                "  stat_month VARCHAR(255),\n"
                "  org_id VARCHAR(255),\n"
                "  dept_id VARCHAR(255),\n"
                "  monthly_charge_count BIGINT COMMENT '当月费用单据总数'\n"
                ")\nUNIQUE KEY(stat_month, org_id, dept_id)"
            ),
        ),
    ]

    failures = await _probe_execute_layer_ddls(state, plan)
    conflict = [f for f in failures if f["check"] == "ddl_probe_schema_conflict"]
    assert len(conflict) == 1, f"expected schema conflict, got {failures}"
    assert conflict[0]["layer"] == "DWS"
    assert "monthly_charge_count" in conflict[0]["detail"]
    assert "already exists" in conflict[0]["detail"]


@pytest.mark.asyncio
async def test_ddl_probe_no_conflict_when_columns_match(monkeypatch):
    """Existing table whose real columns match the planned DDL → no failure."""
    from wren_agent.nodes.validation import _probe_execute_layer_ddls

    fake = _ConflictDoris(existing={"dws_ok": {"a", "b"}})
    monkeypatch.setattr(
        "wren_agent.tools.doris_client.DorisClient",
        lambda database=None: fake,
    )
    state = _warehouse_state()
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.sql_artifacts = [
        SQLArtifact(
            layer="DWS", target_table="dws_ok", sql_type="DDL",
            sql="CREATE TABLE IF NOT EXISTS db_dws.dws_ok (a VARCHAR(10), b VARCHAR(10))",
        ),
    ]
    failures = await _probe_execute_layer_ddls(state, plan)
    assert not any(
        f["check"] == "ddl_probe_schema_conflict" for f in failures
    ), failures


@pytest.mark.asyncio
async def test_ddl_probe_skips_when_table_not_yet_created(monkeypatch):
    """New table (not yet existing) must NOT trigger a schema conflict."""
    from wren_agent.nodes.validation import _probe_execute_layer_ddls

    fake = _ConflictDoris(existing={})  # table absent → execute creates it
    monkeypatch.setattr(
        "wren_agent.tools.doris_client.DorisClient",
        lambda database=None: fake,
    )
    state = _warehouse_state()
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.sql_artifacts = [
        SQLArtifact(
            layer="DWS", target_table="dws_fresh", sql_type="DDL",
            sql="CREATE TABLE IF NOT EXISTS db_dws.dws_fresh (a VARCHAR(10))",
        ),
    ]
    failures = await _probe_execute_layer_ddls(state, plan)
    assert failures == []


@pytest.mark.asyncio
async def test_ddl_probe_flags_conflict_on_backticked_qualified_name(monkeypatch):
    """task d3760a4f: compiler DDL is `CREATE TABLE IF NOT EXISTS `db`.`t``.
    `(qualifier).strip('`').strip('.')` left a trailing backtick
    (`dws_gz_test\\``), DorisClient failed, and the exception was swallowed
    so a leftover table from an earlier harness probe (task 971a8151:
    `inventory_month` vs this run's `stat_month`) shipped to INSERT."""
    from wren_agent.nodes.validation import _probe_execute_layer_ddls

    existing = {"dws_stock_inventory_monthly": {
        "org_id", "inventory_month", "sku_code", "sku_name",
        "monthly_before_number_sum", "monthly_after_number_sum",
        "monthly_diff_number_sum", "monthly_inventory_row_count",
    }}

    def factory(database=None):
        if database and "`" in str(database):
            raise ValueError(f"invalid database name {database!r}")
        return _ConflictDoris(database=database, existing=existing)

    monkeypatch.setattr("wren_agent.tools.doris_client.DorisClient", factory)

    state = _warehouse_state()
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.sql_artifacts = [
        SQLArtifact(
            layer="DWS",
            target_table="dws_gz_test.dws_stock_inventory_monthly",
            sql_type="DDL",
            sql=(
                "-- 粒度: (org_id, stat_month)\n"
                "CREATE TABLE IF NOT EXISTS "
                "`dws_gz_test`.`dws_stock_inventory_monthly` (\n"
                "  `org_id` VARCHAR(255) COMMENT '管理单位ID',\n"
                "  `stat_month` VARCHAR(7) COMMENT '统计月份',\n"
                "  `card_code` VARCHAR(255) COMMENT '资产卡片编号'\n"
                ")\nUNIQUE KEY(`org_id`, `stat_month`)"
            ),
        ),
    ]

    failures = await _probe_execute_layer_ddls(state, plan)
    conflict = [f for f in failures if f["check"] == "ddl_probe_schema_conflict"]
    assert len(conflict) == 1, f"expected schema conflict, got {failures}"
    assert "stat_month" in conflict[0]["detail"]
    assert "inventory_month" in conflict[0]["detail"]


# ── Guard 2: INSERT target columns vs real table ──────────────


@pytest.mark.asyncio
async def test_insert_target_columns_flagged_against_existing_table(monkeypatch):
    """EXPLAIN covers the SELECT body only — an INSERT whose explicit target
    list references a column the existing table lacks must be flagged."""
    from wren_agent.nodes.validation import _doris_explain_load_sqls

    class FakeDoris:
        def __init__(self, database=None):
            self.database = database

        async def query(self, sql, params=()):
            # EXPLAIN of the SELECT body parses fine — the problem is only
            # in the INSERT target list.
            return []

        async def table_exists(self, table_name: str) -> bool:
            return table_name == "dws_category_sales_daily_monthly"

        async def get_table_schema(self, table_name: str) -> dict:
            return _real_columns(
                "org_id", "dept_id", "stat_month", "monthly_water_receivable",
            )

    monkeypatch.setattr(
        "wren_agent.tools.doris_client.DorisClient",
        lambda database=None: FakeDoris(database=database),
    )
    state = _warehouse_state()
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.sql_artifacts = [
        SQLArtifact(
            layer="DWS", target_table="dws_category_sales_daily_monthly",
            sql_type="DML_INSERT",
            sql=(
                "INSERT INTO db_dws.dws_category_sales_daily_monthly "
                "(stat_month, org_id, dept_id, monthly_charge_count) "
                "SELECT DATE_FORMAT(approved_time, '%Y%m'), org_id, dept_id, "
                "COUNT(*) FROM dwd_ecommerce_orders_sh.dwd_charge_event "
                "GROUP BY DATE_FORMAT(approved_time, '%Y%m'), org_id, dept_id"
            ),
        ),
    ]

    failures = await _doris_explain_load_sqls(state, plan)
    col = [f for f in failures if f["check"] == "doris_dml_insert_columns"]
    assert len(col) == 1, f"expected insert-column failure, got {failures}"
    assert "monthly_charge_count" in col[0]["detail"]


@pytest.mark.asyncio
async def test_insert_target_skips_when_target_table_absent(monkeypatch):
    """Plan-internal / not-yet-created target tables are skipped — the DDL
    creates them at deploy, so there is nothing to compare yet."""
    from wren_agent.nodes.validation import _doris_explain_load_sqls

    class FakeDoris:
        def __init__(self, database=None):
            self.database = database

        async def query(self, sql, params=()):
            return []

        async def table_exists(self, table_name: str) -> bool:
            return False

        async def get_table_schema(self, table_name: str) -> dict:
            raise AssertionError("must not be called for an absent table")

    monkeypatch.setattr(
        "wren_agent.tools.doris_client.DorisClient",
        lambda database=None: FakeDoris(database=database),
    )
    state = _warehouse_state()
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.sql_artifacts = [
        SQLArtifact(
            layer="DWS", target_table="dws_fresh", sql_type="DML_INSERT",
            sql=(
                "INSERT INTO db_dws.dws_fresh (a, b) "
                "SELECT a, b FROM db_dwd.src"
            ),
        ),
    ]
    failures = await _doris_explain_load_sqls(state, plan)
    assert failures == []


@pytest.mark.asyncio
async def test_insert_target_columns_flag_existing_plan_table(monkeypatch):
    """task d3760a4f: `_check_insert_target_columns` skipped any INSERT whose
    bare target was in this plan's dws_designs ('created at deploy'). A
    leftover table from an earlier run's DDL probe already existed with a
    different schema (`inventory_month` vs `stat_month`), so the check never
    DESCRIBEd it and the same broken INSERT redeployed until the revision
    budget expired. Plan-internal names that already exist must be compared."""
    from wren_agent.models.design import DWSDesign
    from wren_agent.nodes.validation import _doris_explain_load_sqls

    class FakeDoris:
        def __init__(self, database=None):
            self.database = database

        async def query(self, sql, params=()):
            return []

        async def table_exists(self, table_name: str) -> bool:
            return table_name == "dws_stock_inventory_monthly"

        async def get_table_schema(self, table_name: str) -> dict:
            return _real_columns(
                "org_id", "inventory_month", "sku_code",
                "sku_name", "monthly_before_number_sum",
            )

    monkeypatch.setattr(
        "wren_agent.tools.doris_client.DorisClient",
        lambda database=None: FakeDoris(database=database),
    )
    state = _warehouse_state()
    plan = DWDesignPlan(task_id="t", modeling_goal="goal")
    plan.dws_designs = [DWSDesign(
        summary_name="盘点月汇总",
        summary_table="dws_gz_test.dws_stock_inventory_monthly",
        source_dwd_table="",
        grain=["org_id", "stat_month"],
    )]
    plan.sql_artifacts = [
        SQLArtifact(
            layer="DWS",
            target_table="dws_gz_test.dws_stock_inventory_monthly",
            sql_type="DML_INSERT",
            sql=(
                "INSERT INTO `dws_gz_test`.`dws_stock_inventory_monthly` "
                "(`org_id`, `stat_month`, `card_code`, `card_name`, "
                "`monthly_before_number_sum`)\n"
                "SELECT a.org_id AS `org_id`, "
                "DATE_FORMAT(a.inventory_date, '%Y-%m') AS `stat_month`, "
                "MAX(b.dim_code) AS `card_code`, MAX(b.dim_name) AS `card_name`, "
                "SUM(a.before_number) AS `monthly_before_number_sum` "
                "FROM dwd_gz_test.dwd_stock_inventory_info a "
                "JOIN dim_gz_test.dim_sku b "
                "ON a.sku_id = b.sku_id "
                "GROUP BY `org_id`, `stat_month`;"
            ),
        ),
    ]

    failures = await _doris_explain_load_sqls(state, plan)
    col = [f for f in failures if f["check"] == "doris_dml_insert_columns"]
    assert len(col) == 1, f"expected insert-column failure, got {failures}"
    assert "stat_month" in col[0]["detail"]


# ── Guard 3: re-classify column-not-found against real schema ─


def _dependency_classification(task_instance_id: str = "1588") -> ErrorClassification:
    return ErrorClassification(
        task_instance_id=task_instance_id,
        error_type=ErrorType.DEPENDENCY,
        raw_log="",
        summary="Schema dependency error (missing column/table)",
        action="escalate",
    )


_RAW_LOG_COLUMN_NOT_FOUND = (
    "[LOG-PATH]: /opt/dolphinscheduler/logs/20260814/181498461573920/1/1588/15260.log\n"
    "execute sql error: errCode = 2, detailMessage = "
    "column monthly_charge_count is not found in table "
    "dws_category_sales_daily_monthly\n"
    "INSERT INTO db_dws.dws_category_sales_daily_monthly "
    "(stat_month, org_id, dept_id, monthly_charge_count) SELECT ..."
)


@pytest.mark.asyncio
async def test_column_not_found_reclassified_to_logic_when_table_missing_col(monkeypatch):
    """Target table exists and lacks the referenced column → the agent's own
    SQL vs an existing table → LOGIC (design_revise), not DEPENDENCY."""
    from wren_agent.nodes.deployment import _verify_column_not_found_against_real_schema

    class FakeDoris:
        def __init__(self, database=None):
            self.database = database

        async def table_exists(self, table_name: str) -> bool:
            return table_name == "dws_category_sales_daily_monthly"

        async def get_table_schema(self, table_name: str) -> dict:
            return _real_columns(
                "org_id", "dept_id", "stat_month", "monthly_water_receivable",
            )

    monkeypatch.setattr(
        "wren_agent.tools.doris_client.DorisClient",
        lambda database=None: FakeDoris(database=database),
    )
    state = _warehouse_state()
    result = await _verify_column_not_found_against_real_schema(
        _RAW_LOG_COLUMN_NOT_FOUND, _dependency_classification(), state,
    )
    assert result.error_type == ErrorType.LOGIC
    assert result.action == "escalate"  # repairable → design_revise
    assert "monthly_charge_count" in result.summary


@pytest.mark.asyncio
async def test_column_not_found_keeps_dependency_when_column_exists(monkeypatch):
    """The missing column actually exists in the real table → genuine
    upstream change → keep DEPENDENCY."""
    from wren_agent.nodes.deployment import _verify_column_not_found_against_real_schema

    class FakeDoris:
        def __init__(self, database=None):
            self.database = database

        async def table_exists(self, table_name: str) -> bool:
            return True

        async def get_table_schema(self, table_name: str) -> dict:
            return _real_columns("monthly_charge_count", "org_id")

    monkeypatch.setattr(
        "wren_agent.tools.doris_client.DorisClient",
        lambda database=None: FakeDoris(database=database),
    )
    state = _warehouse_state()
    result = await _verify_column_not_found_against_real_schema(
        _RAW_LOG_COLUMN_NOT_FOUND, _dependency_classification(), state,
    )
    assert result.error_type == ErrorType.DEPENDENCY


@pytest.mark.asyncio
async def test_column_not_found_keeps_dependency_when_doris_unreachable(monkeypatch):
    """Doris unreachable / query failure → keep the original classification."""
    from wren_agent.nodes.deployment import _verify_column_not_found_against_real_schema

    class FakeDoris:
        def __init__(self, database=None):
            self.database = database

        async def table_exists(self, table_name: str) -> bool:
            raise RuntimeError("connection refused")

        async def get_table_schema(self, table_name: str) -> dict:
            raise AssertionError("must not be called")

    monkeypatch.setattr(
        "wren_agent.tools.doris_client.DorisClient",
        lambda database=None: FakeDoris(database=database),
    )
    state = _warehouse_state()
    result = await _verify_column_not_found_against_real_schema(
        _RAW_LOG_COLUMN_NOT_FOUND, _dependency_classification(), state,
    )
    assert result.error_type == ErrorType.DEPENDENCY


# ── Keyword classifier split ──────────────────────────────────


def test_column_not_found_is_logic_not_dependency():
    """`column X is not found in table Y` alone must NOT classify as
    DEPENDENCY — it is pending real-schema verification (LOGIC base)."""
    from wren_agent.tools.dolphin_scheduler import classify_execution_error

    result = classify_execution_error(
        "1588",
        "execute sql error: errCode = 2, detailMessage = "
        "column monthly_charge_count is not found in table "
        "dws_category_sales_daily_monthly",
    )
    assert result.error_type == ErrorType.LOGIC
    assert result.action == "escalate"


def test_table_not_found_still_dependency():
    """A missing source table remains a genuine upstream dependency."""
    from wren_agent.tools.dolphin_scheduler import classify_execution_error

    result = classify_execution_error(
        "ti-5b", "Table 'dw.fct_gone' not found"
    )
    assert result.error_type == ErrorType.DEPENDENCY


def test_llm_dependency_unknown_column_reconciled_to_logic():
    """Regression de018b42: LLM labelled Unknown-column as DEPENDENCY and
    opened human_approval; keyword classifier already knows it is LOGIC."""
    from wren_agent.tools.dolphin_scheduler import (
        classify_execution_error,
        reconcile_llm_and_keyword_classification,
    )

    raw = (
        "Unknown column 'code' in 'table list' in PROJECT clause\n"
        "INSERT INTO dim_floor (floor_id, dim_code) SELECT id, code "
        "FROM ods_gz.ods_basics_floor"
    )
    keyword = classify_execution_error("3165", raw)
    llm = ErrorClassification(
        task_instance_id="3165",
        error_type=ErrorType.DEPENDENCY,
        raw_log=raw,
        summary="多个表中列不存在",
        action="escalate",
        llm_classified=True,
    )
    result = reconcile_llm_and_keyword_classification(llm, keyword)
    assert result.error_type == ErrorType.LOGIC
    assert result.llm_classified is True


def test_llm_dependency_kept_when_keyword_also_dependency():
    from wren_agent.tools.dolphin_scheduler import (
        classify_execution_error,
        reconcile_llm_and_keyword_classification,
    )

    raw = "Table 'dw.fct_gone' not found"
    keyword = classify_execution_error("ti-5b", raw)
    llm = ErrorClassification(
        task_instance_id="ti-5b",
        error_type=ErrorType.DEPENDENCY,
        raw_log=raw,
        summary="上游表不存在",
        action="escalate",
        llm_classified=True,
    )
    result = reconcile_llm_and_keyword_classification(llm, keyword)
    assert result.error_type == ErrorType.DEPENDENCY


# ── repair hints for the new checks ───────────────────────────


def test_repair_hints_for_schema_conflict_checks():
    from wren_agent.nodes.validation import _repair_hint

    for check in ("ddl_probe_schema_conflict", "doris_dml_insert_columns"):
        hint = _repair_hint({"check": check})
        assert hint and len(hint) > 20, check
        assert "rename" in hint.lower() or "existing" in hint.lower(), hint
