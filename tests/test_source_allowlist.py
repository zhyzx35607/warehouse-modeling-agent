"""Goal source-table allowlist (task 94a11f97).

When the modeling goal restricts sources (源表仅限 / 只使用 / sources only),
the compiler refuses extra JOIN tables instead of remapping them.
"""

from wren_agent.compiler.allowlist import (
    parse_goal_source_allowlist,
    source_allowlist_problems,
)
from wren_agent.compiler.render import compile_semantic_design, try_compile_semantic_design
from wren_agent.models.semantic import SemanticDesign


EXPLORED = {
    "dwd_stock_inventory_info",
    "dim_sku",
    "dim_category",
    "dim_management_unit",
    "dws_stock_inventory_monthly",
}

T02_GOAL = (
    "建设固定库存盘点卡片明细汇总：按资产卡片、存放地点（盘点后）、盘点月份，"
    "统计盘点前/后数量与差异。展示卡片编码、名称、规格、使用年限。"
    "源表仅限 dwd_stock_inventory_info + dim_sku。"
    "DIM 若含 is_current 则只取当前行。粒度：sku_id + storage_location + 统计月。"
    "DWS 原子指标，ADS 不做。"
)

T01_GOAL = (
    "只使用 dwd_stock_inventory_info，JOIN dim_sku 取卡片编码/名称。"
    "DWS 只物化 SUM/COUNT 原子指标。"
)


def test_parse_source_limited_clause():
    allow = parse_goal_source_allowlist(T02_GOAL, EXPLORED)
    assert allow == frozenset({"dwd_stock_inventory_info", "dim_sku"})
    assert "dim_category" not in allow


def test_parse_only_use_with_join():
    allow = parse_goal_source_allowlist(T01_GOAL, EXPLORED)
    assert allow == frozenset({"dwd_stock_inventory_info", "dim_sku"})


def test_parse_no_restrict_marker_is_none():
    goal = (
        "按管理单位、盘点月份统计库存盘点，JOIN dim_sku 和 dim_category。"
    )
    assert parse_goal_source_allowlist(goal, EXPLORED) is None


def test_parse_ignores_column_tokens():
    goal = "源表仅限 is_current + storage_location。"
    assert parse_goal_source_allowlist(goal, EXPLORED) is None


def _dws_with_sources(sources: list[str], joins: list[dict]) -> SemanticDesign:
    return SemanticDesign(
        layer="DWS",
        summary_name="卡片月汇总",
        summary_table="dws_stock_inventory_card_monthly",
        columns=[
            {"name": "sku_id", "dtype": "BIGINT", "is_key": True,
             "source_expr": "a.sku_id"},
            {"name": "stat_month", "dtype": "VARCHAR(7)", "is_key": True,
             "source_expr": "DATE_FORMAT(a.create_time, '%Y-%m')"},
            {"name": "before_number", "dtype": "DECIMAL(19,4)", "is_measure": True,
             "source_expr": "SUM(a.before_number)"},
        ],
        sources=sources,
        joins=joins,
    )


def test_extra_dim_refuses_compile():
    design = _dws_with_sources(
        [
            "dwd_gz_test.dwd_stock_inventory_info",
            "dim_gz_test.dim_sku",
            "dim_gz_test.dim_category",
        ],
        [
            {"right": "dim_gz_test.dim_sku",
             "on": "a.sku_id = b.sku_id"},
            {"right": "dim_gz_test.dim_category",
             "on": "b.dim_category_id = c.dim_category_id"},
        ],
    )
    allow = {"dwd_stock_inventory_info", "dim_sku"}
    compiled, problems = try_compile_semantic_design(
        design, allowed_source_tables=allow,
    )
    assert compiled is None
    blob = " ".join(problems)
    assert "dim_category" in blob
    assert "Allowed tables" in blob
    assert "dwd_stock_inventory_info" in blob


def test_does_not_remap_extra_table_to_allowed():
    design = _dws_with_sources(
        [
            "dwd_gz_test.dwd_stock_inventory_info",
            "dim_gz_test.dim_category",
        ],
        [{"right": "dim_gz_test.dim_category",
          "on": "a.sku_id = b.sku_id"}],
    )
    out = compile_semantic_design(
        design,
        allowed_source_tables={"dwd_stock_inventory_info", "dim_sku"},
    )
    assert out is None


def test_allowed_sources_still_compile():
    design = _dws_with_sources(
        [
            "dwd_gz_test.dwd_stock_inventory_info",
            "dim_gz_test.dim_sku",
        ],
        [{"right": "dim_gz_test.dim_sku",
          "on": "a.sku_id = b.sku_id AND b.is_current = 1"}],
    )
    out = compile_semantic_design(
        design,
        allowed_source_tables={"dwd_stock_inventory_info", "dim_sku"},
    )
    assert out is not None
    assert "dim_category" not in out.load_sql


def test_ads_may_read_plan_dws_outside_goal_allowlist():
    """ADS reading this plan's DWS is the layer contract; extra DIM is not."""
    design = SemanticDesign(
        layer="ADS",
        summary_name="看板",
        summary_table="ads_stock_inventory_card_monthly",
        columns=[
            {"name": "sku_id", "dtype": "BIGINT", "is_key": True,
             "source_expr": "a.sku_id"},
        ],
        sources=["dws_gz_test.dws_stock_inventory_card_monthly"],
    )
    problems = source_allowlist_problems(
        design,
        {"dwd_stock_inventory_info", "dim_sku",
         "dws_stock_inventory_card_monthly"},
    )
    assert problems == []
    extra = SemanticDesign(
        layer="ADS",
        summary_name="看板",
        summary_table="ads_x",
        columns=[
            {"name": "sku_id", "dtype": "BIGINT", "is_key": True,
             "source_expr": "a.sku_id"},
        ],
        sources=[
            "dws_gz_test.dws_stock_inventory_card_monthly",
            "dim_gz_test.dim_category",
        ],
        joins=[{"right": "dim_gz_test.dim_category",
                "on": "a.x = b.y"}],
    )
    extra_problems = source_allowlist_problems(
        extra,
        {"dwd_stock_inventory_info", "dim_sku",
         "dws_stock_inventory_card_monthly"},
    )
    assert extra_problems
    assert any("dim_category" in p for p in extra_problems)
