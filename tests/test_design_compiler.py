"""DesignCompiler tests (ADR-0015 Phase 1).

Verifies the deterministic DDL/load-SQL rendering:
- DDL grammar: UNIQUE KEY = grain as ordered prefix, COMMENT before
  DISTRIBUTED BY, HASH distribution, replication_num PROPERTIES, type
  normalization (STRING/BOOLEAN), db-qualified warehouse targets.
- Load SQL: explicit INSERT column list == DDL columns, GROUP BY grain,
  multi-source aliases, filters.
- Expression whitelist: off-whitelist expressions fail the compile (→ the
  table falls back, the compiler never renders unverifiable SQL).
- design_layers integration: `_try_compiler_dws` parses semantic_designs,
  compiles, maps to DWSDesign, and falls back to legacy when the flag is off
  or the semantic key is absent.
"""

import asyncio

import pytest

from wren_agent.compiler import compile_semantic_design, landing_db_for_layer
from wren_agent.compiler.rules import (
    apply_composed_table_name,
    compose_layer_table_name,
    map_doris_type,
    normalize_table_name,
    table_name_limit,
    uniquify_table_name,
)
from wren_agent.models.semantic import SemanticDesign


def _flow_design() -> SemanticDesign:
    """The skill's canonical example (trans_date + company_id grain)."""
    return SemanticDesign(
        layer="DWS",
        summary_name="每日财务流水汇总",
        summary_table="dws_transaction_flow_daily",
        columns=[
            {"name": "trans_date", "dtype": "DATE", "comment": "交易日期",
             "is_key": True, "source_expr": "DATE(trans_dt)"},
            {"name": "company_id", "dtype": "BIGINT", "comment": "公司ID",
             "is_key": True, "source_expr": "company_id"},
            {"name": "company_name", "dtype": "VARCHAR(255)", "comment": "公司名称",
             "source_expr": "MAX(company_name)"},
            {"name": "daily_amt", "dtype": "DECIMAL(20,2)", "comment": "每日交易金额",
             "is_measure": True, "source_expr": "SUM(amt)"},
            {"name": "daily_flow_count", "dtype": "BIGINT", "comment": "每日交易笔数",
             "is_measure": True, "source_expr": "COUNT(1)"},
        ],
        sources=["dwd_transaction_flow"],
        filters=["trans_dt IS NOT NULL"],
        requirement_coverage=[{
            "requirement": "日均资金流水",
            "status": "supported",
            "evidence": ["trans_date", "daily_amt"],
        }],
    )


# ── Rules ───────────────────────────────────────────────────────────


class TestRules:
    def test_type_mapping(self):
        assert map_doris_type("STRING") == "VARCHAR(65533)"
        assert map_doris_type("BOOLEAN") == "TINYINT"
        assert map_doris_type("decimal(18, 2)") == "DECIMAL(18, 2)"
        assert map_doris_type("VARCHAR(64)") == "VARCHAR(64)"
        assert map_doris_type("") == "VARCHAR(255)"

    def test_table_name_normalization(self):
        assert normalize_table_name("`db_dws`.`dws_x`") == "dws_x"
        assert normalize_table_name("db_dws.dws_x") == "dws_x"
        long_name = "dws_order_revenue_contract_month_snapshot_extended"
        assert len(normalize_table_name(long_name)) <= 64

    def test_compose_splits_card_loc_vs_org_monthly(self):
        """Same DWD topic + month, different entity grain → different DWS names.

        Generic ``dws_{fact}_monthly`` collided card×location vs org quality.
        """
        sources = ["dwd_gz_test.dwd_stock_inventory_info", "dim_gz_test.dim_sku"]
        card = compose_layer_table_name(
            "DWS",
            ["sku_id", "storage_location", "stat_month"],
            sources=sources,
            current="dws_stock_inventory_monthly",
        )
        org = compose_layer_table_name(
            "DWS",
            ["org_id", "stat_month"],
            sources=sources,
            current="dws_stock_inventory_monthly",
        )
        assert card == "dws_stock_inventory_card_loc_monthly"
        assert org == "dws_stock_inventory_org_monthly"
        assert card != org
        assert len(card) <= table_name_limit()
        assert len(org) <= table_name_limit()

    def test_compose_is_idempotent_when_grain_tokens_present(self):
        name = "dws_stock_inventory_card_loc_monthly"
        again = compose_layer_table_name(
            "DWS",
            ["sku_id", "storage_location", "stat_month"],
            current=name,
        )
        assert again == name

    def test_compose_ads_purpose_and_grain(self):
        name = compose_layer_table_name(
            "ADS",
            ["org_id", "stat_month"],
            sources=["dws_gz_test.dws_stock_inventory_org_monthly"],
            current="ads_stock_inventory",
            summary_name="固定库存盘点质量看板",
        )
        assert name == "ads_stock_inventory_org_quality"
        assert len(name) <= table_name_limit()

    def test_compose_fits_doris_limit_with_wide_grain(self):
        name = compose_layer_table_name(
            "DWS",
            [
                "sku_id", "storage_location", "building_id",
                "store_id", "org_id", "stat_month",
            ],
            sources=["dwd_order_rental_receivable_contract_snapshot_extended"],
            current=(
                "dws_order_revenue_contract_month_"
                "snapshot_extended_monthly"
            ),
        )
        assert name.startswith("dws_")
        assert name.endswith("_monthly")
        for token in ("card", "loc", "bldg", "store", "org"):
            assert token in name.split("_"), name
        assert len(name) <= table_name_limit()
        assert len(name) <= 64

    def test_compile_rewrites_generic_monthly_name(self):
        design = SemanticDesign(
            layer="DWS",
            summary_name="卡片明细月汇总",
            summary_table="dws_stock_inventory_monthly",
            columns=[
                {"name": "sku_id", "dtype": "BIGINT", "is_key": True,
                 "source_expr": "a.sku_id"},
                {"name": "storage_location", "dtype": "VARCHAR(255)",
                 "is_key": True, "source_expr": "a.storage_location"},
                {"name": "stat_month", "dtype": "VARCHAR(7)", "is_key": True,
                 "source_expr": "DATE_FORMAT(a.create_time, '%Y-%m')"},
                {"name": "before_number", "dtype": "DECIMAL(19,4)",
                 "is_measure": True, "source_expr": "SUM(a.before_number)"},
            ],
            sources=["dwd_gz_test.dwd_stock_inventory_info"],
        )
        out = compile_semantic_design(design)
        assert out is not None
        assert "dws_stock_inventory_card_loc_monthly" in out.ddl
        assert "dws_stock_inventory_monthly " not in out.ddl
        assert design.summary_table == "dws_stock_inventory_card_loc_monthly"

    def test_uniquify_skips_free_name(self):
        assert uniquify_table_name(
            "dws_stock_inventory_card_loc_monthly",
            {"dws_stock_inventory_monthly"},
        ) == "dws_stock_inventory_card_loc_monthly"

    def test_uniquify_suffixes_occupied_composed_name(self):
        name = uniquify_table_name(
            "dws_stock_inventory_card_loc_monthly",
            {"dws_stock_inventory_card_loc_monthly"},
        )
        assert name == "dws_stock_inventory_card_loc_monthly_v2"
        assert len(name) <= table_name_limit()

    def test_uniquify_increments_when_v2_taken(self):
        name = uniquify_table_name(
            "dws_x_org_monthly",
            {"dws_x_org_monthly", "dws_x_org_monthly_v2"},
        )
        assert name == "dws_x_org_monthly_v3"

    def test_uniquify_fits_doris_limit(self):
        occupied = {"dws_" + "a" * 60}
        name = uniquify_table_name("dws_" + "a" * 60, occupied)
        assert name not in occupied
        assert len(name) <= table_name_limit()
        assert name.startswith("dws_")

    def test_apply_composed_uniquifies_against_occupied(self):
        design = SemanticDesign(
            layer="DWS",
            summary_name="卡片明细月汇总",
            summary_table="dws_stock_inventory_monthly",
            columns=[
                {"name": "sku_id", "dtype": "BIGINT", "is_key": True,
                 "source_expr": "a.sku_id"},
                {"name": "storage_location", "dtype": "VARCHAR(255)",
                 "is_key": True, "source_expr": "a.storage_location"},
                {"name": "stat_month", "dtype": "VARCHAR(7)", "is_key": True,
                 "source_expr": "DATE_FORMAT(a.create_time, '%Y-%m')"},
                {"name": "before_number", "dtype": "DECIMAL(19,4)",
                 "is_measure": True, "source_expr": "SUM(a.before_number)"},
            ],
            sources=["dwd_gz_test.dwd_stock_inventory_info"],
        )
        occupied = {"dws_stock_inventory_monthly", "dws_stock_inventory_card_loc_monthly"}
        assert apply_composed_table_name(design, occupied=occupied) == (
            "dws_stock_inventory_card_loc_monthly_v2"
        )

    def test_strip_source_alias(self):
        from wren_agent.compiler.rules import strip_db_prefix, strip_source_alias
        assert strip_source_alias(
            "dwd_gz_test.dwd_stock_inventory_info a"
        ) == "dwd_gz_test.dwd_stock_inventory_info"
        assert strip_source_alias("dim_sku AS b") == "dim_sku"
        assert strip_source_alias("db_dwd.dwd_sale_order") == "db_dwd.dwd_sale_order"
        assert strip_source_alias("`db`.`table` a") == "`db`.`table`"
        assert strip_db_prefix("dwd_gz_test.dwd_stock_inventory_info a") == (
            "dwd_stock_inventory_info"
        )

    def test_grain_must_lead(self):
        with pytest.raises(Exception, match="grain columns"):
            SemanticDesign(
                layer="DWS",
                summary_table="dws_x",
                columns=[
                    {"name": "daily_amt", "dtype": "DECIMAL(20,2)", "source_expr": "SUM(amt)"},
                    {"name": "trans_date", "dtype": "DATE", "is_key": True,
                     "source_expr": "DATE(trans_dt)"},
                ],
                sources=["dwd_x"],
            )


# ── Compile: DDL ────────────────────────────────────────────────────


class TestCompileDDL:
    def test_business_mode_bare_ddl(self):
        out = compile_semantic_design(_flow_design(), layer_landing_db="")
        assert out is not None
        ddl = out.ddl
        # grammar order: columns → UNIQUE KEY → table COMMENT → DISTRIBUTED → PROPERTIES
        # (rindex — the first "COMMENT" belongs to a column definition)
        assert ddl.index("UNIQUE KEY") < ddl.rindex("COMMENT")
        assert ddl.rindex("COMMENT") < ddl.index("DISTRIBUTED BY HASH")
        assert ddl.index("DISTRIBUTED BY HASH") < ddl.index("PROPERTIES")
        # UNIQUE KEY = grain, in order
        assert "UNIQUE KEY(`trans_date`, `company_id`)" in ddl
        # key columns lead the schema
        assert ddl.index("`trans_date`") < ddl.index("`daily_amt`")
        # no db prefix in business mode
        assert "CREATE TABLE IF NOT EXISTS dws_transaction_flow_company_daily" in ddl

    def test_warehouse_mode_qualified_ddl(self):
        out = compile_semantic_design(_flow_design(), layer_landing_db="db_dws")
        assert out is not None
        assert "CREATE TABLE IF NOT EXISTS `db_dws`.`dws_transaction_flow_company_daily`" in out.ddl
        assert out.target_table == "db_dws.dws_transaction_flow_company_daily"

    def test_type_normalization_in_ddl(self):
        design = _flow_design()
        design.columns[1].dtype = "STRING"  # key column must not stay STRING
        out = compile_semantic_design(design)
        assert out is not None
        assert "VARCHAR(65533)" in out.ddl


# ── Compile: load SQL ───────────────────────────────────────────────


class TestCompileLoadSQL:
    def test_explicit_column_list_matches_ddl(self):
        out = compile_semantic_design(_flow_design())
        assert out is not None
        cols = [c.name for c in _flow_design().columns]
        header = out.load_sql.split("SELECT", 1)[0]
        for col in cols:
            assert f"`{col}`" in header
        # GROUP BY grain in order
        assert "GROUP BY `trans_date`, `company_id`" in out.load_sql
        # filters AND-joined
        assert "WHERE trans_dt IS NOT NULL" in out.load_sql

    def test_multi_source_aliases_and_joins(self):
        design = _flow_design()
        design.sources = ["db_dwd.dwd_sale_order", "db_dwd.dwd_refund_order"]
        design.joins = [{
            "right": "db_dwd.dwd_refund_order",
            "on": "a.order_id = b.order_id",
        }]
        design.columns[0].source_expr = "a.trans_dt"
        design.columns[1].source_expr = "a.company_id"
        out = compile_semantic_design(design, layer_landing_db="db_dws")
        assert out is not None
        assert "FROM db_dwd.dwd_sale_order a" in out.load_sql
        assert "JOIN db_dwd.dwd_refund_order b ON a.order_id = b.order_id" in out.load_sql

    def test_join_on_and_select_aliases_rewritten_to_compiler_letters(self):
        """Task 18594730: compiler FROM aliases are a/b/c in source order, but
        the LLM wrote ON/SELECT against d/t. Doris rejected
        `Unknown column 'sku_id' in 'd'`. Rewrite those letters to
        the compiler aliases — this is the a,b,c contract, not schema bind.
        """
        design = SemanticDesign(
            layer="DWS",
            summary_name="固定库存盘点卡片明细汇总",
            summary_table="dws_stock_inventory_card_monthly",
            columns=[
                {"name": "sku_id", "dtype": "BIGINT", "is_key": True,
                 "source_expr": "a.sku_id"},
                {"name": "storage_location", "dtype": "VARCHAR(255)",
                 "is_key": True, "source_expr": "a.storage_location"},
                {"name": "stat_month", "dtype": "VARCHAR(7)", "is_key": True,
                 "source_expr": "DATE_FORMAT(a.create_time, '%Y-%m')"},
                {"name": "card_code", "dtype": "VARCHAR(255)",
                 "source_expr": "MAX(d.dim_code)"},
                {"name": "use_life_month", "dtype": "VARCHAR(255)",
                 "source_expr": "MAX(t.use_life)"},
                {"name": "before_number", "dtype": "DECIMAL(19,4)",
                 "is_measure": True, "source_expr": "SUM(a.before_number)"},
            ],
            sources=[
                "dwd_gz_test.dwd_stock_inventory_info",
                "dim_gz_test.dim_sku",
                "dim_gz_test.dim_category",
            ],
            joins=[
                {"right": "dim_gz_test.dim_sku",
                 "on": "a.sku_id = d.sku_id AND d.is_current = 1"},
                {"right": "dim_gz_test.dim_category",
                 "on": "d.dim_code = t.dim_code AND t.is_current = 1"},
            ],
        )
        out = compile_semantic_design(design)
        assert out is not None
        sql = out.load_sql
        assert "JOIN dim_gz_test.dim_sku b ON a.sku_id = b.sku_id AND b.is_current = 1" in sql
        assert "JOIN dim_gz_test.dim_category c ON b.dim_code = c.dim_code AND c.is_current = 1" in sql
        assert "MAX(b.dim_code)" in sql
        assert "MAX(c.use_life)" in sql
        assert " d." not in sql
        assert " t." not in sql

    def test_join_aliases_c_and_t_rewritten_when_c_is_compiler_third(self):
        """Task 5c814729: LLM used ``c`` = card and ``t`` = type, but the
        compiler's third source is already ``c``. Collision must not leave
        ``c.sku_id`` bound to ``dim_category``."""
        design = SemanticDesign(
            layer="DWS",
            summary_name="固定库存盘点卡片明细汇总",
            summary_table="dws_stock_inventory_card_monthly",
            columns=[
                {"name": "sku_id", "dtype": "BIGINT", "is_key": True,
                 "source_expr": "a.sku_id"},
                {"name": "storage_location", "dtype": "VARCHAR(255)",
                 "is_key": True, "source_expr": "a.storage_location"},
                {"name": "stat_month", "dtype": "VARCHAR(7)", "is_key": True,
                 "source_expr": "DATE_FORMAT(a.create_time, '%Y-%m')"},
                {"name": "card_code", "dtype": "VARCHAR(255)",
                 "source_expr": "MAX(c.dim_code)"},
                {"name": "card_name", "dtype": "VARCHAR(255)",
                 "source_expr": "MAX(c.dim_name)"},
                {"name": "specifications", "dtype": "VARCHAR(255)",
                 "source_expr": "MAX(c.specifications)"},
                {"name": "use_life_year", "dtype": "VARCHAR(255)",
                 "source_expr": "MAX(t.use_life)"},
                {"name": "before_number", "dtype": "DECIMAL(19,4)",
                 "is_measure": True, "source_expr": "SUM(a.before_number)"},
            ],
            sources=[
                "dwd_gz_test.dwd_stock_inventory_info",
                "dim_gz_test.dim_sku",
                "dim_gz_test.dim_category",
            ],
            joins=[
                {"right": "dim_gz_test.dim_sku",
                 "on": "a.sku_id = c.sku_id AND c.is_current = 1"},
                {"right": "dim_gz_test.dim_category",
                 "on": "c.dim_code = t.dim_code AND t.is_current = 1"},
            ],
        )
        out = compile_semantic_design(
            design, source_columns=_three_table_inventory_source_columns(),
        )
        assert out is not None
        sql = out.load_sql
        assert "JOIN dim_gz_test.dim_sku b ON a.sku_id = b.sku_id AND b.is_current = 1" in sql
        assert "JOIN dim_gz_test.dim_category c ON b.dim_code = c.dim_code AND c.is_current = 1" in sql
        assert "MAX(b.dim_code)" in sql
        assert "MAX(b.specifications)" in sql
        assert "MAX(c.use_life)" in sql
        assert " c.sku_id" not in sql

    def test_baked_in_source_aliases_are_not_doubled(self):
        """task 971a8151: compiler-mode prompts say 'declare joins with
        aliases a, b, c…', so the design LLM puts aliases into `sources`
        (`dwd_x a`, `dim_y b`) and `joins.right`. Rendering those as
        `FROM dwd_x a a JOIN dim_y b b` is rejected by Doris
        (`mismatched input 'a'`). The compiler must strip the baked-in
        alias and emit a single `a`/`b`."""
        design = _flow_design()
        design.sources = [
            "dwd_gz_test.dwd_stock_inventory_info a",
            "dim_gz_test.dim_sku b",
        ]
        design.joins = [{
            "right": "dim_gz_test.dim_sku b",
            "on": "a.sku_id = b.sku_id AND b.is_current = 1",
        }]
        design.columns[0].source_expr = "a.org_id"
        design.columns[1].source_expr = "a.company_id"
        out = compile_semantic_design(design, layer_landing_db="dws_gz_test")
        assert out is not None
        sql = out.load_sql
        assert " a a " not in sql
        assert " b b " not in sql
        assert (
            "FROM dwd_gz_test.dwd_stock_inventory_info a "
            "JOIN dim_gz_test.dim_sku b ON "
            "a.sku_id = b.sku_id AND b.is_current = 1"
        ) in sql

    def test_join_right_without_alias_matches_aliased_source(self):
        """joins.right may omit the alias the LLM baked into sources."""
        design = _flow_design()
        design.sources = [
            "db_dwd.dwd_sale_order a",
            "db_dwd.dwd_refund_order b",
        ]
        design.joins = [{
            "right": "db_dwd.dwd_refund_order",
            "on": "a.order_id = b.order_id",
        }]
        design.columns[0].source_expr = "a.trans_dt"
        design.columns[1].source_expr = "a.company_id"
        out = compile_semantic_design(design, layer_landing_db="db_dws")
        assert out is not None
        assert " a a " not in out.load_sql
        assert (
            "FROM db_dwd.dwd_sale_order a "
            "JOIN db_dwd.dwd_refund_order b ON a.order_id = b.order_id"
        ) in out.load_sql

    def test_single_source_baked_in_alias_is_not_doubled(self):
        """Same baked-in alias on a single source (task 8e76b357 path)."""
        design = _flow_design()
        design.sources = ["db_dwd.dwd_order_payment_cycle_acc a"]
        design.columns[0].source_expr = "DATE_FORMAT(a.cycle_start_date, '%Y-%m-01')"
        design.columns[1].source_expr = "a.org_id"
        design.columns[2].source_expr = "MAX(a.company_name)"
        design.columns[3].source_expr = "SUM(a.amount_receivable)"
        design.filters = ["a.cycle_start_date IS NOT NULL"]
        out = compile_semantic_design(design, layer_landing_db="db_dws")
        assert out is not None
        assert "FROM db_dwd.dwd_order_payment_cycle_acc a a" not in out.load_sql
        assert "FROM db_dwd.dwd_order_payment_cycle_acc a" in out.load_sql

    def test_dws_mapping_strips_baked_in_source_aliases(self):
        from wren_agent.nodes.design_layers import _dws_design_from_semantic

        design = _flow_design()
        design.sources = [
            "dwd_gz_test.dwd_stock_inventory_info a",
            "dim_gz_test.dim_sku b",
        ]
        mapped = _dws_design_from_semantic(design, "dws_gz_test.dws_x")
        assert mapped.source_tables == [
            "dwd_gz_test.dwd_stock_inventory_info",
            "dim_gz_test.dim_sku",
        ]

    def test_multi_source_without_joins_fails_compile(self):
        design = _flow_design()
        design.sources = ["dwd_a", "dwd_b"]
        assert compile_semantic_design(design) is None

    def test_single_source_with_prefixed_expr_gets_alias(self):
        """task 8e76b357: the design LLM writes `a.cycle_start_date` against a
        single source; rendering `FROM tbl` bare produced an undefined alias
        (`mismatched input '('`) that failed every DWS load SQL in the
        revision loop. When a column expr or filter references the source
        alias letter, the compiler must render `FROM tbl a`."""
        design = _flow_design()
        design.sources = ["db_dwd.dwd_order_payment_cycle_acc"]
        design.columns[0].source_expr = "DATE_FORMAT(a.cycle_start_date, '%Y-%m-01')"
        design.columns[1].source_expr = "a.org_id"
        design.columns[2].source_expr = "MAX(a.company_name)"
        design.columns[3].source_expr = "SUM(a.amount_receivable)"
        design.filters = ["a.cycle_start_date IS NOT NULL", "a.org_id IS NOT NULL"]
        out = compile_semantic_design(design, layer_landing_db="db_dws")
        assert out is not None
        assert "FROM db_dwd.dwd_order_payment_cycle_acc a" in out.load_sql

    def test_single_source_without_prefix_stays_bare(self):
        """No alias prefix in exprs/filters -> FROM stays bare (no alias)."""
        design = _flow_design()
        design.sources = ["db_dwd.dwd_order_payment_cycle_acc"]
        design.columns[0].source_expr = "DATE_FORMAT(cycle_start_date, '%Y-%m-01')"
        design.columns[1].source_expr = "org_id"
        design.columns[2].source_expr = "MAX(company_name)"
        design.columns[3].source_expr = "SUM(amount_receivable)"
        design.filters = ["cycle_start_date IS NOT NULL"]
        out = compile_semantic_design(design, layer_landing_db="db_dws")
        assert out is not None
        assert "FROM db_dwd.dwd_order_payment_cycle_acc a" not in out.load_sql
        assert "FROM db_dwd.dwd_order_payment_cycle_acc\n" in out.load_sql

    def test_ads_load_sql_has_no_group_by(self):
        """task 11a576bb: ADS reads DWS summary tables (one row per grain) —
        a compiler-rendered GROUP BY demanded every physical column be
        aggregated and Doris rejected it with "not in aggregate's output"
        at execution. ADS load SQL must have NO GROUP BY."""
        from wren_agent.models.semantic import SemanticColumn

        design = _flow_design()
        design.layer = "ADS"
        design.summary_table = "ads_financial_flow_daily"
        design.sources = ["db_dws.dws_transaction_flow_daily"]
        design.columns = [
            SemanticColumn(name="trans_date", dtype="DATE", comment="交易日期",
                           is_key=True, source_expr="trans_date"),
            SemanticColumn(name="company_id", dtype="BIGINT", comment="公司ID",
                           is_key=True, source_expr="company_id"),
            SemanticColumn(name="daily_amt", dtype="DECIMAL(20,2)", comment="每日交易金额",
                           is_measure=True, source_expr="daily_amt"),
            SemanticColumn(name="collection_rate", dtype="DECIMAL(20,4)", comment="实收率",
                           is_measure=True, source_expr="paid_amt / daily_amt"),
        ]
        out = compile_semantic_design(design, layer_landing_db="db_ads")
        assert out is not None
        assert "GROUP BY" not in out.load_sql
        # derived expression preserved
        assert "paid_amt / daily_amt AS `collection_rate`" in out.load_sql

    def test_ads_strips_redundant_aggregate_shell(self):
        """ADS inherits `MAX(org_name)` from the DWS design pattern — without
        GROUP BY that collapses the whole table to one row. The compiler must
        strip the redundant aggregate shell to the bare column."""
        design = _flow_design()
        design.layer = "ADS"
        design.summary_table = "ads_financial_flow_daily"
        design.sources = ["db_dws.dws_transaction_flow_daily"]
        design.columns[2].source_expr = "MAX(company_name)"
        design.columns[2].name = "company_name"
        out = compile_semantic_design(design, layer_landing_db="db_ads")
        assert out is not None
        assert "MAX(company_name)" not in out.load_sql
        assert "company_name AS `company_name`" in out.load_sql


# ── Expression whitelist ────────────────────────────────────────────


class TestExpressionWhitelist:
    def test_off_whitelist_function_fails_compile(self):
        design = _flow_design()
        design.columns[-1].source_expr = "EXPLODE_SPLIT(ids, ',')"
        assert compile_semantic_design(design) is None

    def test_subquery_fails_compile(self):
        design = _flow_design()
        design.columns[0].source_expr = "(SELECT MAX(dt) FROM dwd_x)"
        assert compile_semantic_design(design) is None

    def test_arithmetic_and_scalar_ok(self):
        design = _flow_design()
        design.columns[-1].source_expr = "SUM(amt) - SUM(refund_amt)"
        out = compile_semantic_design(design)
        assert out is not None
        assert "SUM(amt) - SUM(refund_amt) AS `daily_flow_count`" in out.load_sql

    def test_trim_on_grain_column_compiles(self):
        """Task 9474f040: LLM wrapped storage_location in TRIM();
        the compiler refused the whole DWS. TRIM is Doris-safe."""
        design = _card_monthly_design(
            join_on="a.sku_id = b.sku_id AND b.is_current = 1",
        )
        design.columns[1].source_expr = "TRIM(a.storage_location)"
        out = compile_semantic_design(
            design, source_columns=_card_monthly_source_columns(),
        )
        assert out is not None
        assert "TRIM(a.storage_location) AS `storage_location`" in out.load_sql

    def test_missing_sources_fails_compile(self):
        design = _flow_design()
        design.sources = []
        assert compile_semantic_design(design) is None


def _ads_design() -> SemanticDesign:
    """ADS semantic design with an atomic + a derived metric (Phase 2)."""
    return SemanticDesign(
        layer="ADS",
        summary_name="单位日均资金流水看板",
        summary_table="ads_financial_flow_daily_dashboard",
        target_audience="财务运营看板",
        columns=[
            {"name": "stat_date", "dtype": "DATE", "comment": "统计日期",
             "is_key": True, "source_expr": "trans_date"},
            {"name": "org_id", "dtype": "BIGINT", "comment": "单位ID",
             "is_key": True, "source_expr": "company_id"},
            {"name": "org_name", "dtype": "VARCHAR(255)", "comment": "单位名称",
             "source_expr": "MAX(company_name)"},
            {"name": "daily_amt", "dtype": "DECIMAL(20,2)", "comment": "日均流水金额",
             "is_measure": True, "source_expr": "daily_amt",
             "metric_type": "atomic"},
            {"name": "daily_flow_count", "dtype": "BIGINT", "comment": "日均交易笔数",
             "is_measure": True, "source_expr": "daily_flow_count",
             "metric_type": "atomic"},
            {"name": "avg_ticket", "dtype": "DECIMAL(20,2)", "comment": "日均客单价",
             "is_measure": True, "source_expr": "daily_amt / daily_flow_count",
             "metric_type": "derived", "parent_metrics": ["daily_amt", "daily_flow_count"]},
        ],
        sources=["db_dws.dws_transaction_flow_daily"],
        requirement_coverage=[{
            "requirement": "日均资金流水看板",
            "status": "supported",
            "evidence": ["daily_amt", "avg_ticket"],
        }],
    )


# ── ADS compile (Phase 2) ───────────────────────────────────────────


class TestCompileADS:
    def test_ddl_with_grain_uses_unique_key(self):
        out = compile_semantic_design(_ads_design(), layer_landing_db="db_ads")
        assert out is not None
        assert "CREATE TABLE IF NOT EXISTS `db_ads`.`ads_financial_flow_daily_dashboard`" in out.ddl
        assert "UNIQUE KEY(`stat_date`, `org_id`)" in out.ddl
        assert "DISTRIBUTED BY HASH(`stat_date`)" in out.ddl
        # keys lead, measures come after descriptive
        assert out.ddl.index("`stat_date`") < out.ddl.index("`avg_ticket`")

    def test_load_sql_groups_by_grain(self):
        out = compile_semantic_design(_ads_design())
        assert out is not None
        assert "INSERT INTO ads_financial_flow_daily_dashboard" in out.load_sql
        # task 11a576bb: ADS reads DWS summary tables — one row per grain —
        # so it must NOT GROUP BY (a compiler-rendered GROUP BY made Doris
        # reject the physical columns with "not in aggregate's output").
        assert "GROUP BY" not in out.load_sql
        assert "daily_amt / daily_flow_count AS `avg_ticket`" in out.load_sql
        assert "FROM db_dws.dws_transaction_flow_daily" in out.load_sql

    def test_ads_without_grain_renders_no_key(self):
        design = _ads_design()
        design.columns = [c for c in design.columns if not c.is_key]
        out = compile_semantic_design(design)
        assert out is not None
        assert "UNIQUE KEY" not in out.ddl
        assert "DISTRIBUTED BY RANDOM" in out.ddl
        assert "GROUP BY" not in out.load_sql

    def test_ads_remaps_stat_date_and_drops_org_name(self):
        """Task 9f42f542: skill-example `stat_date`/`org_name` vs DWS `stat_month`."""
        design = SemanticDesign(
            layer="ADS",
            summary_name="单位月度库存盘点看板",
            summary_table="ads_stock_inventory_monthly",
            columns=[
                {"name": "inventory_month", "dtype": "VARCHAR(7)", "comment": "盘点月份",
                 "is_key": True, "source_expr": "stat_date"},
                {"name": "org_id", "dtype": "BIGINT", "comment": "单位ID",
                 "is_key": True, "source_expr": "org_id"},
                {"name": "org_name", "dtype": "VARCHAR(255)", "comment": "单位名称",
                 "source_expr": "MAX(org_name)"},
                {"name": "monthly_before_number_sum", "dtype": "BIGINT",
                 "comment": "盘前数量合计", "is_measure": True,
                 "source_expr": "monthly_before_number_sum"},
            ],
            sources=["dws_gz_test.dws_stock_inventory_monthly"],
        )
        source_cols = {
            "org_id", "stat_month", "sku_code", "sku_name",
            "monthly_before_number_sum", "monthly_after_number_sum",
            "monthly_diff_number_sum", "monthly_inventory_line_count",
        }
        out = compile_semantic_design(design, source_columns=source_cols)
        assert out is not None
        assert "stat_month AS `inventory_month`" in out.load_sql
        assert "stat_date" not in out.load_sql
        assert "org_name" not in out.load_sql
        assert "`org_id`" in out.ddl
        assert "`inventory_month`" in out.ddl
        assert "org_name" not in out.ddl
        assert [c.name for c in design.columns] == [
            "inventory_month", "org_id", "monthly_before_number_sum",
        ]

    def test_ads_remaps_line_count_to_row_count(self):
        """Task f7055bbf: ADS `monthly_inventory_line_count` vs DWS row_count."""
        design = SemanticDesign(
            layer="ADS",
            summary_name="库存盘点月度汇总",
            summary_table="ads_stock_inventory_monthly",
            columns=[
                {"name": "org_id", "dtype": "BIGINT", "is_key": True,
                 "source_expr": "org_id"},
                {"name": "stat_month", "dtype": "VARCHAR(7)", "is_key": True,
                 "source_expr": "stat_month"},
                {"name": "monthly_inventory_line_count", "dtype": "BIGINT",
                 "is_measure": True,
                 "source_expr": "monthly_inventory_line_count"},
            ],
            sources=["dws_gz_test.dws_stock_inventory_monthly"],
        )
        out = compile_semantic_design(design, source_columns={
            "org_id", "stat_month", "monthly_inventory_row_count",
            "monthly_before_number_sum",
        })
        assert out is not None
        assert "monthly_inventory_row_count AS `monthly_inventory_row_count`" in out.load_sql
        assert "line_count" not in out.load_sql
        assert "`monthly_inventory_row_count`" in out.ddl
        assert "line_count" not in out.ddl
        assert [c.name for c in design.columns] == [
            "org_id", "stat_month", "monthly_inventory_row_count",
        ]

    def test_dws_wraps_coalesce_dim_code_in_max(self):
        """Task 891fc962: COALESCE(b.dim_code) at org_id+month grain is not
        aggregated — Doris rejects `dim_code, dim_name not in aggregate's
        output`. Compiler must wrap non-grain, non-aggregate exprs in MAX.
        """
        design = SemanticDesign(
            layer="DWS",
            summary_name="库存盘点月度汇总",
            summary_table="dws_stock_inventory_monthly",
            columns=[
                {"name": "org_id", "dtype": "BIGINT", "is_key": True,
                 "source_expr": "a.org_id"},
                {"name": "stat_month", "dtype": "VARCHAR(7)", "is_key": True,
                 "source_expr": "DATE_FORMAT(a.create_time, '%Y-%m')"},
                {"name": "sku_code", "dtype": "VARCHAR(255)",
                 "source_expr": "COALESCE(b.dim_code, '未知')"},
                {"name": "sku_name", "dtype": "VARCHAR(255)",
                 "source_expr": "COALESCE(b.dim_name, '未知')"},
                {"name": "monthly_before_number_sum", "dtype": "DECIMAL(20,4)",
                 "is_measure": True, "source_expr": "SUM(a.before_number)"},
                {"name": "monthly_inventory_row_count", "dtype": "BIGINT",
                 "is_measure": True, "source_expr": "COUNT(1)"},
            ],
            sources=[
                "dwd_gz_test.dwd_stock_inventory_info",
                "dim_gz_test.dim_sku",
            ],
            joins=[{
                "right": "dim_gz_test.dim_sku",
                "on": "a.sku_id = b.sku_id AND b.is_current = 1",
            }],
        )
        by_alias = {
            "a": {"org_id", "sku_id", "before_number", "create_time"},
            "b": {"sku_id", "dim_code", "dim_name", "is_current"},
            "dwd_gz_test.dwd_stock_inventory_info": {
                "org_id", "sku_id", "before_number", "create_time",
            },
            "dim_gz_test.dim_sku": {
                "sku_id", "dim_code", "dim_name", "is_current",
            },
        }
        out = compile_semantic_design(design, source_columns=by_alias)
        assert out is not None
        assert "MAX(COALESCE(b.dim_code, '未知')) AS `sku_code`" in out.load_sql
        assert "MAX(COALESCE(b.dim_name, '未知')) AS `sku_name`" in out.load_sql
        assert "SUM(a.before_number)" in out.load_sql
        assert "COUNT(1)" in out.load_sql
        assert "DATE_FORMAT(a.create_time, '%Y-%m')" in out.load_sql
        assert "MAX(DATE_FORMAT" not in out.load_sql
        assert "MAX(a.org_id)" not in out.load_sql
        assert "MAX(SUM(" not in out.load_sql

    def test_ads_drops_org_name_fabricated_from_dim_and_remaps_line_count(self):
        """Task 891fc962: ADS invented `org_name` via MAX(COALESCE(b.dim_name))
        (no alias `b` on a single DWS source) and `line_count` vs DWS
        `row_count`. Drop the fabricated attribute; bind the count synonym.
        """
        design = SemanticDesign(
            layer="ADS",
            summary_name="库存盘点月度汇总报表",
            summary_table="ads_stock_inventory_monthly",
            columns=[
                {"name": "stat_month", "dtype": "VARCHAR(7)", "is_key": True,
                 "source_expr": "stat_month"},
                {"name": "org_id", "dtype": "BIGINT", "is_key": True,
                 "source_expr": "org_id"},
                {"name": "org_name", "dtype": "VARCHAR(255)",
                 "source_expr": "MAX(COALESCE(b.dim_name, '未知'))"},
                {"name": "monthly_before_number_sum", "dtype": "BIGINT",
                 "is_measure": True,
                 "source_expr": "monthly_before_number_sum"},
                {"name": "monthly_inventory_line_count", "dtype": "BIGINT",
                 "is_measure": True,
                 "source_expr": "monthly_inventory_line_count"},
            ],
            sources=["dws_gz_test.dws_stock_inventory_monthly"],
        )
        out = compile_semantic_design(design, source_columns={
            "org_id", "stat_month", "sku_code", "sku_name",
            "monthly_before_number_sum", "monthly_after_number_sum",
            "monthly_diff_number_sum", "monthly_inventory_row_count",
        })
        assert out is not None
        assert "org_name" not in out.load_sql
        assert "org_name" not in out.ddl
        assert "dim_name" not in out.load_sql
        assert "monthly_inventory_row_count AS `monthly_inventory_row_count`" in out.load_sql
        assert "line_count" not in out.load_sql
        assert [c.name for c in design.columns] == [
            "stat_month", "org_id", "monthly_before_number_sum",
            "monthly_inventory_row_count",
        ]

    def test_dws_remaps_dim_code_family_per_alias(self):
        """Task 449dc3ca: `b.card_code` on dim_sku which has dim_code."""
        design = SemanticDesign(
            layer="DWS",
            summary_name="库存盘点按管理单位和盘点月份汇总",
            summary_table="dws_stock_inventory_monthly",
            columns=[
                {"name": "org_id", "dtype": "BIGINT", "comment": "管理单位",
                 "is_key": True, "source_expr": "a.org_id"},
                {"name": "stat_month", "dtype": "VARCHAR(7)", "comment": "盘点月份",
                 "is_key": True,
                 "source_expr": "DATE_FORMAT(a.create_time, '%Y-%m')"},
                {"name": "sku_code", "dtype": "VARCHAR(255)",
                 "comment": "资产卡片编号",
                 "source_expr": "MAX(b.card_code)"},
                {"name": "sku_name", "dtype": "VARCHAR(255)",
                 "comment": "资产卡片名称",
                 "source_expr": "MAX(b.card_name)"},
                {"name": "monthly_before_number_sum", "dtype": "DECIMAL(20,4)",
                 "comment": "盘点前数量合计", "is_measure": True,
                 "source_expr": "SUM(a.before_number)"},
            ],
            sources=[
                "dwd_gz_test.dwd_stock_inventory_info",
                "dim_gz_test.dim_sku",
            ],
            joins=[{
                "right": "dim_gz_test.dim_sku",
                "on": "a.sku_id = b.sku_id AND b.is_current = 1",
            }],
        )
        by_alias = {
            "a": {
                "org_id", "sku_id", "before_number", "create_time",
            },
            "b": {
                "sku_id", "dim_code", "dim_name", "is_current",
            },
            "dwd_gz_test.dwd_stock_inventory_info": {
                "org_id", "sku_id", "before_number", "create_time",
            },
            "dim_gz_test.dim_sku": {
                "sku_id", "dim_code", "dim_name", "is_current",
            },
        }
        out = compile_semantic_design(
            design, layer_landing_db="dws_gz_test", source_columns=by_alias,
        )
        assert out is not None
        sql = out.load_sql
        assert "MAX(b.dim_code) AS `sku_code`" in sql
        assert "MAX(b.dim_name) AS `sku_name`" in sql
        assert "b.card_code" not in sql
        assert "b.card_name" not in sql
        assert "DATE_FORMAT(a.create_time, '%Y-%m')" in sql
        assert "SUM(a.before_number)" in sql
        # Alias-aware: dim_code lives on b, not a — do not rewrite a.org_id.
        assert "a.org_id AS `org_id`" in sql


    def test_dws_does_not_steal_dim_code_onto_fact_alias(self):
        """Union bind would map a.card_code → dim_code even though dim_code
        is on the DIM. Alias `a` (DWD) must drop the invented name."""
        design = SemanticDesign(
            layer="DWS",
            summary_name="x",
            summary_table="dws_x",
            columns=[
                {"name": "org_id", "dtype": "BIGINT", "is_key": True,
                 "source_expr": "a.org_id"},
                {"name": "stat_month", "dtype": "VARCHAR(7)", "is_key": True,
                 "source_expr": "a.create_time"},
                {"name": "bogus_code", "dtype": "VARCHAR(255)",
                 "source_expr": "MAX(a.card_code)"},
            ],
            sources=["dwd_gz_test.dwd_stock_inventory_info",
                     "dim_gz_test.dim_sku"],
            joins=[{
                "right": "dim_gz_test.dim_sku",
                "on": "a.sku_id = b.sku_id",
            }],
        )
        out = compile_semantic_design(design, source_columns={
            "a": {"org_id", "create_time", "sku_id"},
            "b": {"sku_id", "dim_code", "dim_name"},
        })
        assert out is not None
        assert "card_code" not in out.load_sql or "b.card_code" not in out.load_sql
        assert "MAX(a.card_code)" not in out.load_sql
        assert "MAX(a.dim_code)" not in out.load_sql
        assert "bogus_code" not in out.ddl
        assert "a.org_id" in out.load_sql


def _card_monthly_design(*, join_on: str, filters: list[str] | None = None) -> SemanticDesign:
    """W-02 DWS shape (task 2efe882a): DWD inventory JOIN dim_sku."""
    return SemanticDesign(
        layer="DWS",
        summary_name="固定库存盘点卡片明细汇总",
        summary_table="dws_stock_inventory_card_monthly",
        columns=[
            {"name": "sku_id", "dtype": "BIGINT", "is_key": True,
             "source_expr": "a.sku_id"},
            {"name": "storage_location", "dtype": "VARCHAR(1020)",
             "is_key": True, "source_expr": "a.storage_location"},
            {"name": "stat_month", "dtype": "VARCHAR(7)", "is_key": True,
             "source_expr": "DATE_FORMAT(a.create_time, '%Y-%m')"},
            {"name": "card_code", "dtype": "VARCHAR(255)",
             "source_expr": "MAX(b.dim_code)"},
            {"name": "card_name", "dtype": "VARCHAR(255)",
             "source_expr": "MAX(b.dim_name)"},
            {"name": "before_number", "dtype": "DECIMAL(19,4)",
             "is_measure": True, "source_expr": "SUM(a.before_number)"},
        ],
        sources=[
            "dwd_gz_test.dwd_stock_inventory_info",
            "dim_gz_test.dim_sku",
        ],
        joins=[{"right": "dim_gz_test.dim_sku", "on": join_on}],
        filters=list(filters or []),
    )


def _three_table_inventory_source_columns() -> dict[str, set[str]]:
    """Explored snapshot for DWD inventory ⋈ card ⋈ type (task 5c814729).

    Card has no ``dim_category_id``; the only joinable shared column with
    type is ``dim_code``.
    """
    dwd = {
        "sku_id", "storage_location", "before_number",
        "after_number", "diff_number", "create_time",
    }
    card = {
        "sku_id", "dim_code", "dim_name", "specifications",
        "use_life_month", "is_current", "effective_date", "expiration_date",
    }
    typ = {
        "dim_category_id", "dim_code", "dim_name", "use_life",
        "is_current", "effective_date", "expiration_date",
    }
    return {
        "a": set(dwd),
        "b": set(card),
        "c": set(typ),
        "dwd_gz_test.dwd_stock_inventory_info": set(dwd),
        "dim_gz_test.dim_sku": set(card),
        "dim_gz_test.dim_category": set(typ),
    }


def _card_monthly_source_columns() -> dict[str, set[str]]:
    dwd = {
        "sku_id", "storage_location", "before_number",
        "after_number", "diff_number", "create_time",
    }
    dim = {
        "sku_id", "dim_code", "dim_name", "is_current",
        "specifications", "use_life_month",
    }
    return {
        "a": set(dwd),
        "b": set(dim),
        "dwd_gz_test.dwd_stock_inventory_info": set(dwd),
        "dim_gz_test.dim_sku": set(dim),
    }


class TestClosedWorldCompiler:
    """Compiler closed world: JOIN/filter identifiers must exist on the
    explored snapshot. Invented JOIN keys are refused, not remapped
    (task 2efe882a: ``b.dim_id`` vs ``sku_id``)."""

    def test_invented_join_key_refuses_compile(self):
        out = compile_semantic_design(
            _card_monthly_design(
                join_on="a.sku_id = b.dim_id AND b.is_current = 1",
            ),
            source_columns=_card_monthly_source_columns(),
        )
        assert out is None

    def test_does_not_remap_dim_id_to_sku_id(self):
        """Closed world is refuse-and-revise, not unique-hit JOIN rewrite."""
        from wren_agent.compiler.render import try_compile_semantic_design

        compiled, problems = try_compile_semantic_design(
            _card_monthly_design(
                join_on="a.sku_id = b.dim_id AND b.is_current = 1",
            ),
            source_columns=_card_monthly_source_columns(),
        )
        assert compiled is None
        blob = " ".join(problems)
        assert "b.dim_id" in blob
        assert "Available columns" in blob
        assert "sku_id" in blob
        assert "dim_id →" not in blob
        assert "remap" not in blob.lower()

    def test_real_join_key_compiles(self):
        out = compile_semantic_design(
            _card_monthly_design(
                join_on="a.sku_id = b.sku_id AND b.is_current = 1",
            ),
            source_columns=_card_monthly_source_columns(),
        )
        assert out is not None
        assert "b.sku_id" in out.load_sql
        assert "b.dim_id" not in out.load_sql

    def test_varchar_grain_empty_filter_becomes_coalesce(self):
        """Task e3914a7e: ``storage_location != ''`` dropped every
        NULL location and materialized an empty DWS. Grain VARCHAR empty
        guards become COALESCE(..., '未知'), other filters stay."""
        design = _card_monthly_design(
            join_on="a.sku_id = b.sku_id AND b.is_current = 1",
            filters=[
                "a.sku_id IS NOT NULL AND a.create_time IS NOT NULL "
                "AND a.storage_location != '' AND b.is_current = 1",
            ],
        )
        out = compile_semantic_design(
            design, source_columns=_card_monthly_source_columns(),
        )
        assert out is not None
        sql = out.load_sql
        assert "storage_location != ''" not in sql
        assert "create_time IS NOT NULL" in sql
        assert "sku_id IS NOT NULL" in sql
        assert "b.is_current = 1" in sql
        assert "COALESCE(NULLIF(a.storage_location, ''), '未知')" in sql

    def test_pipe_concat_rewrites_to_concat(self):
        """Task 60a570e6: Doris treats ``||`` as OR, so ``'-Q'`` becomes
        ``can't cast to bool in strict mode``. String-literal ``||``
        chains compile to ``CONCAT(...)``."""
        design = SemanticDesign(
            layer="ADS",
            summary_name="固定库存盘点质量看板",
            summary_table="ads_stock_inventory_quality",
            columns=[
                {"name": "stat_quarter", "dtype": "VARCHAR(7)", "is_key": True,
                 "source_expr": (
                     "SUBSTRING(a.stat_month, 1, 4) || '-Q' || "
                     "CEIL(CAST(SUBSTRING(a.stat_month, 6, 2) AS INT) / 3.0)"
                 )},
                {"name": "stat_month", "dtype": "VARCHAR(7)", "is_key": True,
                 "source_expr": "a.stat_month"},
                {"name": "org_id", "dtype": "BIGINT", "is_key": True,
                 "source_expr": "a.org_id"},
                {"name": "inventory_variance_rate", "dtype": "DECIMAL(20,4)",
                 "is_measure": True,
                 "source_expr": (
                     "IF(a.monthly_before_number = 0, NULL, "
                     "a.monthly_diff_number / a.monthly_before_number)"
                 )},
            ],
            sources=["dws_gz_test.dws_stock_inventory_monthly"],
            grain=["stat_quarter", "stat_month", "org_id"],
        )
        src = {
            "stat_month", "org_id", "monthly_before_number",
            "monthly_diff_number",
        }
        out = compile_semantic_design(design, source_columns=src)
        assert out is not None
        sql = out.load_sql
        assert "||" not in sql
        assert (
            "CONCAT(SUBSTRING(a.stat_month, 1, 4), '-Q', "
            "CEIL(CAST(SUBSTRING(a.stat_month, 6, 2) AS INT) / 3.0))"
        ) in sql

    def test_boolean_or_pipes_kept(self):
        from wren_agent.compiler.render import rewrite_pipe_concat

        pred = "a.is_current = 1 || b.is_current = 1"
        assert rewrite_pipe_concat(pred) == pred

    def test_non_grain_varchar_empty_filter_kept(self):
        design = _card_monthly_design(
            join_on="a.sku_id = b.sku_id AND b.is_current = 1",
            filters=["b.dim_code != ''"],
        )
        out = compile_semantic_design(
            design, source_columns=_card_monthly_source_columns(),
        )
        assert out is not None
        assert "b.dim_code != ''" in out.load_sql

    def test_both_sides_missing_join_key_rewrites_to_unique_shared(self):
        """Task 5c814729 ADS: ``b.asset_type_id = c.asset_type_id`` is absent
        on both tables. Rewrite to the unique shared joinable column
        (``dim_code``). One-sided inventions (``b.dim_id``) stay refused."""
        design = SemanticDesign(
            layer="ADS",
            summary_name="固定库存盘点卡片明细汇总",
            summary_table="ads_stock_inventory_card_monthly",
            columns=[
                {"name": "sku_id", "dtype": "BIGINT", "is_key": True,
                 "source_expr": "a.sku_id"},
                {"name": "stat_month", "dtype": "VARCHAR(7)", "is_key": True,
                 "source_expr": "DATE_FORMAT(a.create_time, '%Y-%m')"},
                {"name": "before_number", "dtype": "BIGINT",
                 "is_measure": True, "source_expr": "a.before_number"},
            ],
            sources=[
                "dwd_gz_test.dwd_stock_inventory_info",
                "dim_gz_test.dim_sku",
                "dim_gz_test.dim_category",
            ],
            joins=[
                {"right": "dim_gz_test.dim_sku",
                 "on": "a.sku_id = b.sku_id AND b.is_current = 1"},
                {"right": "dim_gz_test.dim_category",
                 "on": "b.asset_type_id = c.asset_type_id AND c.is_current = 1"},
            ],
        )
        out = compile_semantic_design(
            design, source_columns=_three_table_inventory_source_columns(),
        )
        assert out is not None
        assert "b.dim_code = c.dim_code" in out.load_sql
        assert "asset_type_id" not in out.load_sql

    def test_invented_filter_column_refuses_compile(self):
        out = compile_semantic_design(
            _card_monthly_design(
                join_on="a.sku_id = b.sku_id AND b.is_current = 1",
                filters=["b.dim_sk IS NOT NULL"],
            ),
            source_columns=_card_monthly_source_columns(),
        )
        assert out is None

    def test_without_source_columns_join_letters_still_rewrite(self):
        """Closed world is a no-op when explore snapshot was not passed —
        alias-letter rewrite (task 18594730) must keep working."""
        design = SemanticDesign(
            layer="DWS",
            summary_name="x",
            summary_table="dws_x",
            columns=[
                {"name": "sku_id", "dtype": "BIGINT", "is_key": True,
                 "source_expr": "a.sku_id"},
                {"name": "card_code", "dtype": "VARCHAR(255)",
                 "source_expr": "MAX(d.dim_code)"},
            ],
            sources=[
                "dwd_gz_test.dwd_stock_inventory_info",
                "dim_gz_test.dim_sku",
            ],
            joins=[{
                "right": "dim_gz_test.dim_sku",
                "on": "a.sku_id = d.sku_id",
            }],
        )
        out = compile_semantic_design(design)
        assert out is not None
        assert "JOIN dim_gz_test.dim_sku b ON a.sku_id = b.sku_id" in out.load_sql


# ── _try_compiler_ads integration ───────────────────────────────────


class TestTryCompilerAds:
    @pytest.mark.asyncio
    async def test_compiles_and_maps_derived_metrics(self, monkeypatch):
        from wren_agent.nodes import design_layers as dl
        from wren_agent.config import settings
        monkeypatch.setattr(settings.llm, "compiler_enabled", True)

        state = _make_state()
        used, designs, artifacts = await dl._try_compiler_ads(
            state, {"semantic_designs": [_ads_design().model_dump()]},
        )
        assert used is True
        assert len(designs) == 1
        ads = designs[0]
        assert ads.ads_table == "ads_financial_flow_daily_dashboard"
        assert ads.source_dws_tables == ["db_dws.dws_transaction_flow_daily"]
        assert ads.dimensions == ["stat_date", "org_id"]
        by_name = {m["name"]: m for m in ads.metrics}
        assert by_name["avg_ticket"]["metric_type"] == "derived"
        assert by_name["avg_ticket"]["parent_metrics"] == ["daily_amt", "daily_flow_count"]
        assert by_name["daily_amt"]["metric_type"] == "atomic"
        types = {a.sql_type for a in artifacts}
        assert types == {"DDL", "DML_INSERT"}
        assert "UNIQUE KEY(`stat_date`, `org_id`)" in artifacts[0].sql

    @pytest.mark.asyncio
    async def test_wrong_layer_entry_rejected(self, monkeypatch):
        from wren_agent.nodes import design_layers as dl
        from wren_agent.config import settings
        monkeypatch.setattr(settings.llm, "compiler_enabled", True)

        state = _make_state()
        wrong = _ads_design().model_dump()
        wrong["layer"] = "DWS"  # DWS designs must not be consumed by ADS
        with pytest.raises(Exception, match="expected ADS"):
            await dl._try_compiler_ads(state, {"semantic_designs": [wrong]})

    @pytest.mark.asyncio
    async def test_no_semantic_key_falls_back(self, monkeypatch):
        from wren_agent.nodes import design_layers as dl
        from wren_agent.config import settings
        monkeypatch.setattr(settings.llm, "compiler_enabled", True)

        state = _make_state()
        used, designs, artifacts = await dl._try_compiler_ads(
            state, {"designs": [{"ads_table": "ads_x"}], "skip": False},
        )
        assert used is False


    @pytest.mark.asyncio
    async def test_binds_against_plan_dws_columns(self, monkeypatch):
        """ADS compile reads this plan's DWS columns (task 9f42f542)."""
        from wren_agent.models.design import DWSDesign, DWDesignPlan
        from wren_agent.nodes import design_layers as dl
        from wren_agent.config import settings
        monkeypatch.setattr(settings.llm, "compiler_enabled", True)

        plan = DWDesignPlan(task_id="t", modeling_goal="g")
        plan.dws_designs = [DWSDesign(
            summary_name="月度库存盘点",
            summary_table="dws_gz_test.dws_stock_inventory_monthly",
            source_dwd_table="dwd_gz_test.dwd_stock_inventory_info",
            fields=[
                {"name": "org_id"}, {"name": "stat_month"},
                {"name": "sku_code"}, {"name": "sku_name"},
            ],
            measures=[{"name": "monthly_before_number_sum"}],
            grain=["org_id", "stat_month"],
        )]
        state = _make_state()
        state["design_plan"] = plan
        semantic = {
            "layer": "ADS",
            "summary_name": "看板",
            "summary_table": "ads_stock_inventory_monthly",
            "columns": [
                {"name": "inventory_month", "dtype": "VARCHAR(7)",
                 "is_key": True, "source_expr": "stat_date"},
                {"name": "org_id", "dtype": "BIGINT",
                 "is_key": True, "source_expr": "org_id"},
                {"name": "org_name", "dtype": "VARCHAR(255)",
                 "source_expr": "org_name"},
                {"name": "monthly_before_number_sum", "dtype": "BIGINT",
                 "is_measure": True, "source_expr": "monthly_before_number_sum"},
            ],
            "sources": ["dws_gz_test.dws_stock_inventory_monthly"],
        }
        used, designs, artifacts = await dl._try_compiler_ads(
            state, {"semantic_designs": [semantic]},
        )
        assert used is True
        assert len(designs) == 1
        load_sql = next(a.sql for a in artifacts if a.sql_type == "DML_INSERT")
        assert "stat_month AS `inventory_month`" in load_sql
        assert "stat_date" not in load_sql
        assert "org_name" not in load_sql
        assert "org_name" not in {f["name"] for f in designs[0].fields}


# ── design_layers integration (`_try_compiler_dws`) ─────────────────


def _make_state(**overrides):
    from wren_agent.state import create_initial_state
    state = create_initial_state("t-comp", "goal", [], {})
    state.update(overrides)
    return state


@pytest.fixture(autouse=True)
def _no_live_landing_tables(monkeypatch):
    """Compiler tests must not uniquify against a real Doris catalog."""
    async def _empty(database=""):
        return []
    monkeypatch.setattr(
        "wren_agent.nodes.design_layers.fetch_landing_table_names",
        _empty,
        raising=False,
    )


class TestTryCompilerDws:
    @pytest.mark.asyncio
    async def test_compiles_semantic_result(self, monkeypatch):
        from wren_agent.nodes import design_layers as dl
        from wren_agent.config import settings

        monkeypatch.setattr(settings.llm, "compiler_enabled", True)

        state = _make_state()
        result = {
            "semantic_designs": [_flow_design().model_dump()],
            "warnings": [],
        }
        used, designs, artifacts = await dl._try_compiler_dws(state, result)

        assert used is True
        assert len(designs) == 1
        dws = designs[0]
        assert dws.summary_table == "dws_transaction_flow_company_daily"
        assert dws.grain == ["trans_date", "company_id"]
        assert {m["name"] for m in dws.measures} == {"daily_amt", "daily_flow_count"}
        types = {a.sql_type for a in artifacts}
        assert types == {"DDL", "DML_INSERT"}
        assert "CREATE TABLE IF NOT EXISTS" in artifacts[0].sql
        assert "UNIQUE KEY(`trans_date`, `company_id`)" in artifacts[0].sql
        assert "INSERT INTO dws_transaction_flow_company_daily" in artifacts[1].sql

    @pytest.mark.asyncio
    async def test_flag_off_returns_unused(self, monkeypatch):
        from wren_agent.nodes import design_layers as dl
        from wren_agent.config import settings
        monkeypatch.setattr(settings.llm, "compiler_enabled", False)

        state = _make_state()
        used, designs, artifacts = await dl._try_compiler_dws(
            state, {"semantic_designs": [_flow_design().model_dump()]},
        )
        assert used is False and designs == [] and artifacts == []

    @pytest.mark.asyncio
    async def test_no_semantic_key_returns_unused(self, monkeypatch):
        from wren_agent.nodes import design_layers as dl
        from wren_agent.config import settings
        monkeypatch.setattr(settings.llm, "compiler_enabled", True)

        state = _make_state()
        used, designs, artifacts = await dl._try_compiler_dws(
            state, {"dws_designs": [{"summary_table": "dws_a"}]},
        )
        assert used is False

    @pytest.mark.asyncio
    async def test_uncompilable_table_skipped_with_warning(self, monkeypatch):
        from wren_agent.nodes import design_layers as dl
        from wren_agent.config import settings
        monkeypatch.setattr(settings.llm, "compiler_enabled", True)

        state = _make_state()
        bad = _flow_design().model_dump()
        bad["sources"] = []  # uncompilable
        used, designs, artifacts = await dl._try_compiler_dws(
            state, {"semantic_designs": [bad]},
        )
        assert used is True
        assert len(designs) == 1
        assert artifacts == []
        assert any("not compiled" in w for w in state["warnings"])

    @pytest.mark.asyncio
    async def test_warehouse_landing_prefix(self, monkeypatch):
        from wren_agent.nodes import design_layers as dl
        from wren_agent.config import settings
        monkeypatch.setattr(settings.llm, "compiler_enabled", True)

        state = _make_state(source_mode="warehouse")
        state["source_databases"] = [{
            "name": "wh", "db_type": "doris",
            "connection_params": {
                "layers": {"dws": ["db_dws"], "ads": ["db_ads"]},
                "landing": {"dws": "db_dws", "ads": "db_ads"},
            },
        }]
        used, designs, artifacts = await dl._try_compiler_dws(
            state, {"semantic_designs": [_flow_design().model_dump()]},
        )
        assert used is True
        assert designs[0].summary_table == "db_dws.dws_transaction_flow_company_daily"
        assert "CREATE TABLE IF NOT EXISTS `db_dws`." in artifacts[0].sql

    @pytest.mark.asyncio
    async def test_catalog_collision_suffixes_v2(self, monkeypatch):
        """Leftover in the DWS landing catalog must not be reused as a new target."""
        from wren_agent.models.schema import SchemaExploration
        from wren_agent.nodes import design_layers as dl
        from wren_agent.config import settings
        monkeypatch.setattr(settings.llm, "compiler_enabled", True)

        state = _make_state(source_mode="warehouse")
        state["explorations"] = {
            "wh#dws": SchemaExploration(
                source_db_id="wh",
                catalog_table_names=[
                    "dws_stock_inventory_monthly",
                    "dws_transaction_flow_company_daily",
                ],
            ),
        }
        used, designs, artifacts = await dl._try_compiler_dws(
            state, {"semantic_designs": [_flow_design().model_dump()]},
        )
        assert used is True
        assert designs[0].summary_table == "dws_transaction_flow_company_daily_v2"
        assert "dws_transaction_flow_company_daily_v2" in artifacts[0].sql
        assert "INSERT INTO dws_transaction_flow_company_daily_v2" in artifacts[1].sql

    @pytest.mark.asyncio
    async def test_live_landing_collision_suffixes_v2(self, monkeypatch):
        from wren_agent.nodes import design_layers as dl
        from wren_agent.config import settings
        monkeypatch.setattr(settings.llm, "compiler_enabled", True)

        async def _live(database=""):
            return ["dws_transaction_flow_company_daily"]

        monkeypatch.setattr(dl, "fetch_landing_table_names", _live)
        state = _make_state(source_mode="warehouse")
        state["source_databases"] = [{
            "name": "wh", "db_type": "doris",
            "connection_params": {
                "layers": {"dws": ["dws_gz_test"]},
                "landing": {"dws": "dws_gz_test"},
            },
        }]
        used, designs, _ = await dl._try_compiler_dws(
            state, {"semantic_designs": [_flow_design().model_dump()]},
        )
        assert used is True
        assert designs[0].summary_table == "dws_gz_test.dws_transaction_flow_company_daily_v2"

    @pytest.mark.asyncio
    async def test_batch_uniquifies_duplicate_composed_names(self, monkeypatch):
        from wren_agent.nodes import design_layers as dl
        from wren_agent.config import settings
        monkeypatch.setattr(settings.llm, "compiler_enabled", True)

        used, designs, _ = await dl._try_compiler_dws(
            state := _make_state(),
            {"semantic_designs": [
                _flow_design().model_dump(),
                _flow_design().model_dump(),
            ]},
        )
        assert used is True
        names = {d.summary_table for d in designs}
        assert names == {
            "dws_transaction_flow_company_daily",
            "dws_transaction_flow_company_daily_v2",
        }

    @pytest.mark.asyncio
    async def test_binds_explored_dim_code_family(self, monkeypatch):
        """DWS compile remaps b.card_code against explored dim_sku."""
        from wren_agent.models.schema import FieldSchema, SchemaExploration, TableSchema
        from wren_agent.nodes import design_layers as dl
        from wren_agent.config import settings
        monkeypatch.setattr(settings.llm, "compiler_enabled", True)

        state = _make_state(source_mode="warehouse")
        state["source_databases"] = [{
            "name": "wh", "db_type": "doris",
            "connection_params": {
                "layers": {"dws": ["dws_gz_test"], "dwd": ["dwd_gz_test"],
                           "dim": ["dim_gz_test"]},
                "landing": {"dws": "dws_gz_test"},
            },
        }]
        state["explorations"] = {
            "wh#dwd": SchemaExploration(source_db_id="wh#dwd", tables=[
                TableSchema(
                    name="dwd_stock_inventory_info", schema_name="dwd_gz_test",
                    db_type="doris", row_count_estimate=100,
                    fields=[
                        FieldSchema(name="org_id", data_type="BIGINT"),
                        FieldSchema(name="sku_id", data_type="BIGINT"),
                        FieldSchema(name="before_number", data_type="DECIMAL"),
                        FieldSchema(name="create_time", data_type="DATE"),
                    ],
                ),
            ]),
            "wh#dim": SchemaExploration(source_db_id="wh#dim", tables=[
                TableSchema(
                    name="dim_sku", schema_name="dim_gz_test",
                    db_type="doris", row_count_estimate=100,
                    fields=[
                        FieldSchema(name="sku_id", data_type="BIGINT"),
                        FieldSchema(name="dim_code", data_type="VARCHAR(255)"),
                        FieldSchema(name="dim_name", data_type="VARCHAR(255)"),
                        FieldSchema(name="is_current", data_type="TINYINT"),
                    ],
                ),
            ]),
        }
        semantic = {
            "layer": "DWS",
            "summary_name": "库存盘点月汇总",
            "summary_table": "dws_stock_inventory_monthly",
            "columns": [
                {"name": "org_id", "dtype": "BIGINT", "is_key": True,
                 "source_expr": "a.org_id"},
                {"name": "stat_month", "dtype": "VARCHAR(7)", "is_key": True,
                 "source_expr": "DATE_FORMAT(a.create_time, '%Y-%m')"},
                {"name": "sku_code", "dtype": "VARCHAR(255)",
                 "source_expr": "MAX(b.card_code)"},
                {"name": "monthly_before_number_sum", "dtype": "DECIMAL(20,4)",
                 "is_measure": True, "source_expr": "SUM(a.before_number)"},
            ],
            "sources": [
                "dwd_gz_test.dwd_stock_inventory_info",
                "dim_gz_test.dim_sku",
            ],
            "joins": [{
                "right": "dim_gz_test.dim_sku",
                "on": "a.sku_id = b.sku_id AND b.is_current = 1",
            }],
        }
        used, designs, artifacts = await dl._try_compiler_dws(
            state, {"semantic_designs": [semantic]},
        )
        assert used is True
        assert len(designs) == 1
        load_sql = next(a.sql for a in artifacts if a.sql_type == "DML_INSERT")
        assert "MAX(b.dim_code) AS `sku_code`" in load_sql
        assert "b.card_code" not in load_sql

    @pytest.mark.asyncio
    async def test_closed_world_join_keeps_semantic_for_revise(self, monkeypatch):
        """Task 2efe882a: refuse ``b.dim_id``, keep semantic + explored
        columns so design_revise can pick ``sku_id``."""
        from wren_agent.models.schema import FieldSchema, SchemaExploration, TableSchema
        from wren_agent.nodes import design_layers as dl
        from wren_agent.config import settings
        monkeypatch.setattr(settings.llm, "compiler_enabled", True)

        state = _make_state(source_mode="warehouse")
        state["source_databases"] = [{
            "name": "wh", "db_type": "doris",
            "connection_params": {
                "layers": {"dws": ["dws_gz_test"], "dwd": ["dwd_gz_test"],
                           "dim": ["dim_gz_test"]},
                "landing": {"dws": "dws_gz_test"},
            },
        }]
        state["explorations"] = {
            "wh#dwd": SchemaExploration(source_db_id="wh#dwd", tables=[
                TableSchema(
                    name="dwd_stock_inventory_info", schema_name="dwd_gz_test",
                    db_type="doris", row_count_estimate=100,
                    fields=[
                        FieldSchema(name="sku_id", data_type="BIGINT"),
                        FieldSchema(name="storage_location", data_type="VARCHAR"),
                        FieldSchema(name="before_number", data_type="DECIMAL"),
                        FieldSchema(name="create_time", data_type="DATE"),
                    ],
                ),
            ]),
            "wh#dim": SchemaExploration(source_db_id="wh#dim", tables=[
                TableSchema(
                    name="dim_sku", schema_name="dim_gz_test",
                    db_type="doris", row_count_estimate=100,
                    fields=[
                        FieldSchema(name="sku_id", data_type="BIGINT"),
                        FieldSchema(name="dim_code", data_type="VARCHAR(255)"),
                        FieldSchema(name="dim_name", data_type="VARCHAR(255)"),
                        FieldSchema(name="is_current", data_type="TINYINT"),
                    ],
                ),
            ]),
        }
        semantic = _card_monthly_design(
            join_on="a.sku_id = b.dim_id AND b.is_current = 1",
        ).model_dump()
        used, designs, artifacts = await dl._try_compiler_dws(
            state, {"semantic_designs": [semantic]},
        )
        assert used is True
        assert artifacts == []
        assert len(designs) == 1
        assert state.get("compiled_semantics")
        failures = state.get("compiler_failures") or []
        assert failures
        blob = " ".join(str(f.get("detail", "")) for f in failures)
        assert "b.dim_id" in blob
        assert "sku_id" in blob
        assert "Available columns" in blob
        warn_blob = " ".join(state.get("warnings") or [])
        assert "not compiled" in warn_blob
        assert "sku_id" in warn_blob

    def test_landing_db_for_layer_business_mode(self):
        state = _make_state()
        assert landing_db_for_layer(state, "DWS") == ""


# ── Phase 4: patcher isolation + semantic repair loop ───────────────


class TestCompiledTargets:
    def test_detects_compiled_artifacts(self):
        from wren_agent.models.design import DWDesignPlan, SQLArtifact
        from wren_agent.nodes.validation import _compiled_targets

        plan = DWDesignPlan(task_id="t", modeling_goal="g")
        plan.sql_artifacts = [
            SQLArtifact(layer="DWS", target_table="dws_a", sql_type="DDL",
                        sql="x", description="Compiled DDL for dws_a (DesignCompiler)"),
            SQLArtifact(layer="DWS", target_table="dws_b", sql_type="DDL",
                        sql="x", description="LLM DDL"),
        ]
        assert _compiled_targets(plan) == {"dws_a"}

    def test_backfill_skips_compiled_targets(self):
        from wren_agent.models.design import DWSDesign, DWDesignPlan, SQLArtifact
        from wren_agent.nodes.validation import _backfill_declared_columns

        plan = DWDesignPlan(task_id="t", modeling_goal="g")
        plan.dws_designs = [DWSDesign(
            summary_name="a", summary_table="dws_a", source_dwd_table="dwd_x",
            fields=[{"name": "miss", "type": "BIGINT", "comment": "c"}],
            measures=[], grain=["k"], group_by_dimensions=["k"],
        )]
        plan.sql_artifacts = [
            SQLArtifact(layer="DWS", target_table="dws_a", sql_type="DDL",
                        sql="CREATE TABLE dws_a (k BIGINT)",
                        description="Compiled DDL for dws_a (DesignCompiler)"),
            SQLArtifact(layer="DWS", target_table="dws_a", sql_type="DML_INSERT",
                        sql="INSERT INTO dws_a (k, miss) SELECT k, MAX(x) AS miss FROM dwd_x GROUP BY k",
                        description="Compiled load for dws_a (DesignCompiler)"),
        ]
        # declared `miss` is missing from the DDL — but the table is compiled,
        # so the patcher must NOT rewrite it (the compiler owns the column list).
        # (call sites pass skip_targets=_compiled_targets(plan))
        from wren_agent.nodes.validation import _compiled_targets
        n = _backfill_declared_columns({}, plan, skip_targets=_compiled_targets(plan))
        assert n == 0
        assert "miss" not in plan.sql_artifacts[0].sql


class TestSemanticRepair:
    @pytest.mark.asyncio
    async def test_repair_recompiles_and_replaces(self, monkeypatch):
        from wren_agent.models.design import DWSDesign, DWDesignPlan, SQLArtifact
        from wren_agent.models.task import ValidationLevel, ValidationResult
        from wren_agent.nodes import design_layers, validation
        from wren_agent.config import settings

        monkeypatch.setattr(settings.llm, "compiler_enabled", True)

        plan = DWDesignPlan(task_id="t", modeling_goal="g")
        design = _flow_design()
        target = "dws_transaction_flow_daily"
        plan.dws_designs = [design_layers._dws_design_from_semantic(design, target)]
        plan.sql_artifacts = [
            SQLArtifact(layer="DWS", target_table=target, sql_type="DDL",
                        sql="old ddl", description="Compiled DDL (DesignCompiler)"),
            SQLArtifact(layer="DWS", target_table=target, sql_type="DML_INSERT",
                        sql="old load", description="Compiled load (DesignCompiler)"),
        ]
        state = _make_state()
        state["design_plan"] = plan
        state["compiled_semantics"] = {target: design}

        failure = ValidationResult(
            level=ValidationLevel.STRUCTURE, layer="DWS", passed=False,
            checks=[], failures=[{"check": "dws_fields_materialized_in_load_sql",
                                  "detail": "dws_transaction_flow_daily missing column"}],
            semantic_assertions=[],
        )

        # Fake repair LLM: returns the SAME semantic design (fixed) — the
        # compiler must re-render from it.
        async def fake_generate_typed_json(self, system_prompt, user_message, schema, **kw):
            return schema.model_validate({
                "semantic_designs": [design.model_dump()],
            })

        monkeypatch.setattr(
            "wren_agent.tools.llm_client.get_llm_client",
            lambda: type("FakeLLM", (), {"generate_typed_json": fake_generate_typed_json})(),
        )

        fixed = await validation._llm_repair_semantic(state, failure, plan)

        assert fixed is True
        assert len(plan.sql_artifacts) == 2  # old compiled artifacts replaced
        assert all("Compiled" in (a.description or "") for a in plan.sql_artifacts)
        assert "UNIQUE KEY(`trans_date`, `company_id`)" in plan.sql_artifacts[0].sql
        composed = "dws_transaction_flow_company_daily"
        assert composed in state["compiled_semantics"]
        assert state["compiled_semantics"][composed].summary_table == composed
        assert target not in state["compiled_semantics"]

    @pytest.mark.asyncio
    async def test_repair_skips_layer_without_semantics(self, monkeypatch):
        from wren_agent.models.task import ValidationLevel, ValidationResult
        from wren_agent.nodes import validation

        state = _make_state()
        state["design_plan"] = None
        failure = ValidationResult(
            level=ValidationLevel.STRUCTURE, layer="DWS", passed=False,
            checks=[], failures=[{"check": "x", "detail": "y"}], semantic_assertions=[],
        )
        fixed = await validation._llm_repair_semantic(state, failure, None)
        assert fixed is False  # no compiled semantics → legacy repair path

    @pytest.mark.asyncio
    async def test_repair_prompt_lists_explored_columns(self, monkeypatch):
        """B: revision sees the explore snapshot, not a guessed JOIN key."""
        from wren_agent.models.design import DWDesignPlan
        from wren_agent.models.schema import FieldSchema, SchemaExploration, TableSchema
        from wren_agent.models.task import ValidationLevel, ValidationResult
        from wren_agent.nodes import validation
        from wren_agent.config import settings

        monkeypatch.setattr(settings.llm, "compiler_enabled", True)

        design = _card_monthly_design(
            join_on="a.sku_id = b.dim_id AND b.is_current = 1",
        )
        target = "dws_stock_inventory_card_monthly"
        plan = DWDesignPlan(task_id="t", modeling_goal="g")
        state = _make_state(source_mode="warehouse")
        state["design_plan"] = plan
        state["compiled_semantics"] = {target: design}
        state["explorations"] = {
            "wh#dim": SchemaExploration(source_db_id="wh#dim", tables=[
                TableSchema(
                    name="dim_sku", schema_name="dim_gz_test",
                    db_type="doris", row_count_estimate=10,
                    fields=[
                        FieldSchema(name="sku_id", data_type="BIGINT"),
                        FieldSchema(name="dim_code", data_type="VARCHAR"),
                        FieldSchema(name="dim_name", data_type="VARCHAR"),
                        FieldSchema(name="is_current", data_type="TINYINT"),
                    ],
                ),
            ]),
            "wh#dwd": SchemaExploration(source_db_id="wh#dwd", tables=[
                TableSchema(
                    name="dwd_stock_inventory_info", schema_name="dwd_gz_test",
                    db_type="doris", row_count_estimate=10,
                    fields=[
                        FieldSchema(name="sku_id", data_type="BIGINT"),
                        FieldSchema(name="create_time", data_type="DATE"),
                        FieldSchema(name="before_number", data_type="DECIMAL"),
                        FieldSchema(name="storage_location", data_type="VARCHAR"),
                    ],
                ),
            ]),
        }
        captured: dict[str, str] = {}

        async def fake_generate_typed_json(self, system_prompt, user_message, schema, **kw):
            captured["user"] = user_message
            repaired = design.model_dump()
            repaired["joins"] = [{
                "right": "dim_gz_test.dim_sku",
                "on": "a.sku_id = b.sku_id AND b.is_current = 1",
            }]
            return schema.model_validate({"semantic_designs": [repaired]})

        monkeypatch.setattr(
            "wren_agent.tools.llm_client.get_llm_client",
            lambda: type("FakeLLM", (), {"generate_typed_json": fake_generate_typed_json})(),
        )
        failure = ValidationResult(
            level=ValidationLevel.STRUCTURE, layer="DWS", passed=False,
            checks=[],
            failures=[{
                "check": "compiler_closed_world",
                "detail": (
                    "JOIN ON `b.dim_id` is not in explored source "
                    "`dim_gz_test.dim_sku` (alias b). "
                    "Available columns: sku_id, dim_code, dim_name, is_current"
                ),
            }],
            semantic_assertions=[],
        )
        fixed = await validation._llm_repair_semantic(state, failure, plan)
        assert "sku_id" in captured.get("user", "")
        assert "closed world" in captured.get("user", "").lower()
        assert "dim_gz_test.dim_sku" in captured.get("user", "")
        assert fixed is True
        load = next(a.sql for a in plan.sql_artifacts if a.sql_type == "DML_INSERT")
        assert "b.sku_id" in load
        assert "b.dim_id" not in load


class TestClosedWorldHarness:
    @pytest.mark.asyncio
    async def test_empty_sql_with_compiler_failures_goes_to_revise(self, monkeypatch):
        """Refuse-compile must not fail-fast as 'nothing designed'."""
        from wren_agent.models.design import DWSDesign, DWDesignPlan
        from wren_agent.models.task import TaskStatus
        from wren_agent.nodes.validation import harness_validate
        from wren_agent.state import create_initial_state

        plan = DWDesignPlan(task_id="t", modeling_goal="g")
        plan.dws_designs = [DWSDesign(
            summary_name="卡片月汇总",
            summary_table="dws_gz_test.dws_stock_inventory_card_monthly",
            source_dwd_table="dwd_gz_test.dwd_stock_inventory_info",
            fields=[{"name": "sku_id"}],
            measures=[], grain=["sku_id"],
        )]
        plan.sql_artifacts = []
        state = create_initial_state("t-cw", "goal", [], {})
        state["design_plan"] = plan
        state["compiler_failures"] = [{
            "layer": "DWS",
            "check": "compiler_closed_world",
            "target_table": "dws_gz_test.dws_stock_inventory_card_monthly",
            "detail": (
                "JOIN ON `b.dim_id` is not in explored source "
                "`dim_gz_test.dim_sku` (alias b). "
                "Available columns: sku_id, dim_code, dim_name, is_current"
            ),
        }]
        result = await harness_validate(state)
        assert result["status"] != TaskStatus.FAILED
        assert result["all_validations_passed"] is False
        assert result["validation_blocking"] is True
        checks = [
            f.get("check")
            for r in result.get("validation_results") or []
            for f in (r.failures or [])
            if isinstance(f, dict)
        ]
        assert "compiler_closed_world" in checks

    @pytest.mark.asyncio
    async def test_design_revise_uses_semantics_without_compiled_sql(self, monkeypatch):
        from wren_agent.models.design import DWSDesign, DWDesignPlan
        from wren_agent.models.task import ValidationLevel, ValidationResult
        from wren_agent.nodes import validation
        from wren_agent.config import settings

        monkeypatch.setattr(settings.llm, "compiler_enabled", True)
        design = _card_monthly_design(
            join_on="a.sku_id = b.sku_id AND b.is_current = 1",
        )
        target = "dws_stock_inventory_card_monthly"
        plan = DWDesignPlan(task_id="t", modeling_goal="g")
        plan.dws_designs = [DWSDesign(
            summary_name="x", summary_table=target,
            source_dwd_table="dwd_x", fields=[], measures=[], grain=[],
        )]
        plan.sql_artifacts = []
        state = _make_state()
        state["design_plan"] = plan
        state["compiled_semantics"] = {target: design}
        state["validation_results"] = [ValidationResult(
            level=ValidationLevel.STRUCTURE, layer="DWS", passed=False,
            checks=[],
            failures=[{"check": "compiler_closed_world", "detail": "b.dim_id"}],
            semantic_assertions=[],
        )]

        async def fake_generate_typed_json(self, system_prompt, user_message, schema, **kw):
            return schema.model_validate({
                "semantic_designs": [design.model_dump()],
            })

        monkeypatch.setattr(
            "wren_agent.tools.llm_client.get_llm_client",
            lambda: type("FakeLLM", (), {"generate_typed_json": fake_generate_typed_json})(),
        )
        out = await validation.design_revise(state)
        assert any(a.sql_type == "DML_INSERT" for a in plan.sql_artifacts)
        assert "b.sku_id" in next(
            a.sql for a in plan.sql_artifacts if a.sql_type == "DML_INSERT"
        )


# ── Whitelist: window functions (real-goal regression) ──────────────


class TestWindowFunctionWhitelist:
    def test_row_number_over_accepted(self):
        """Real-goal regression: daily closing balance needs
        MAX(IF(ROW_NUMBER() OVER (PARTITION BY ...) = 1, col, NULL)) — the
        single DWS table of the first real compiler run was rejected because
        ROW_NUMBER was off-whitelist (acceptance finding)."""
        from wren_agent.models.semantic import SemanticColumn
        design = _flow_design()
        design.columns.append(SemanticColumn(
            name="daily_balance_end", dtype="DECIMAL(20,2)",
            comment="每日期末余额", is_measure=True,
            source_expr=(
                "MAX(IF(ROW_NUMBER() OVER (PARTITION BY DATE(trans_dt), "
                "company_id ORDER BY trans_dt DESC) = 1, balance, NULL))"
            ),
        ))
        out = compile_semantic_design(design)
        assert out is not None
        assert "ROW_NUMBER() OVER (PARTITION BY" in out.load_sql

    def test_case_when_accepted(self):
        from wren_agent.models.semantic import SemanticColumn
        design = _flow_design()
        design.columns.append(SemanticColumn(
            name="income_flag", dtype="TINYINT",
            comment="收支方向", is_measure=True,
            source_expr="CASE WHEN cd_sign = '0' THEN 1 ELSE 0 END",
        ))
        out = compile_semantic_design(design)
        assert out is not None

    def test_unknown_window_function_still_rejected(self):
        design = _flow_design()
        design.columns[-1].source_expr = "NTILE(4) OVER (ORDER BY x)"
        assert compile_semantic_design(design) is None
