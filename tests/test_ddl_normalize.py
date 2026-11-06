"""Tests for Doris DDL normalization in _normalize_ddl_properties.

Regression: task b6dc0698 failed on Doris with "mismatched input 'COMMENT'"
because the generated DDL placed the table-level COMMENT after DISTRIBUTED BY
— Doris requires COMMENT before DISTRIBUTED BY.
"""

import pytest

from wren_agent.nodes.design_layers import _normalize_ddl_properties


@pytest.fixture
def replication(monkeypatch):
    """Pin settings.doris.replication_num for deterministic PROPERTIES."""
    from wren_agent.config import settings
    monkeypatch.setattr(settings.doris, "replication_num", 1)


def test_comment_after_distributed_by_is_reordered(replication):
    ddl = (
        "CREATE TABLE IF NOT EXISTS dws_test (\n"
        "  d VARCHAR(255) COMMENT '日期',\n"
        "  v DECIMAL(20,2) COMMENT '值'\n"
        ")\n"
        "UNIQUE KEY(d)\n"
        "DISTRIBUTED BY HASH(d) BUCKETS AUTO\n"
        "COMMENT '汇总表: dws_test'\n"
        "PROPERTIES('replication_num' = '1');"
    )
    out = _normalize_ddl_properties(ddl)
    # COMMENT must come before DISTRIBUTED BY now
    assert out.index("COMMENT '汇总表: dws_test'") < out.index("DISTRIBUTED BY")
    # and still be a single valid statement shape
    assert out.endswith(");") or out.endswith("';")


def test_correct_order_unchanged(replication):
    ddl = (
        "CREATE TABLE IF NOT EXISTS dws_ok (\n"
        "  d VARCHAR(255) COMMENT '日期'\n"
        ")\n"
        "UNIQUE KEY(d)\n"
        "COMMENT 'OK'\n"
        "DISTRIBUTED BY HASH(d) BUCKETS AUTO\n"
        "PROPERTIES('replication_num' = '1');"
    )
    out = _normalize_ddl_properties(ddl)
    assert out == ddl


def test_no_distributed_by_comment_untouched(replication):
    ddl = (
        "CREATE TABLE IF NOT EXISTS ads_flat (\n"
        "  m DECIMAL(20,2) COMMENT '指标'\n"
        ")\n"
        "COMMENT '应用层表: ads_flat'\n"
        "PROPERTIES('replication_num' = '1');"
    )
    out = _normalize_ddl_properties(ddl)
    assert "COMMENT '应用层表: ads_flat'" in out
    assert "DISTRIBUTED BY" not in out


def test_comment_added_before_distributed_when_unique_key_missing_distribution(replication):
    # LLM DDL without DISTRIBUTED BY: the helper inserts it — the reorder
    # guard must not duplicate or corrupt the COMMENT.
    ddl = (
        "CREATE TABLE IF NOT EXISTS dws_no_dist (\n"
        "  d VARCHAR(255) COMMENT '日期'\n"
        ")\n"
        "UNIQUE KEY(d)\n"
        "COMMENT 'NO DIST'\n"
        "PROPERTIES('replication_num' = '1');"
    )
    out = _normalize_ddl_properties(ddl)
    assert out.count("COMMENT 'NO DIST'") == 1
    assert out.index("COMMENT 'NO DIST'") < out.index("DISTRIBUTED BY")
