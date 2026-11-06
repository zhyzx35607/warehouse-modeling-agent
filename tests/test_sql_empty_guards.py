"""Type-aware empty-string guards (task 0af225c9).

Native BIGINT/INT/DECIMAL/DATE columns compared to '' make Doris INSERT-strict
throw `can't cast to decimal in strict mode`. VARCHAR-stored numbers still
need `!= ''` / NULLIF (task 55f47744).
"""

from wren_agent.models.design import SQLArtifact
from wren_agent.models.schema import FieldSchema, SchemaExploration, TableSchema
from wren_agent.models.task import ErrorType
from wren_agent.tools.sql_empty_guards import (
    column_types_from_explorations,
    rewrite_typed_empty_string_guards,
    sanitize_sql_artifacts,
)


def _inv_exploration() -> dict:
    inv = TableSchema(
        name="ods_sku__stock_inventory_info",
        schema_name="ods_ecommerce_0_SH",
        db_type="doris",
        fields=[
            FieldSchema(name="id", data_type="bigint(20)"),
            FieldSchema(name="sku_id", data_type="bigint(20)"),
            FieldSchema(name="stock_inventory_id", data_type="bigint(20)"),
            FieldSchema(name="status", data_type="int(11)"),
            FieldSchema(name="inventory_is", data_type="int(11)"),
            FieldSchema(name="before_storage_location", data_type="varchar(200)"),
            FieldSchema(name="tenant_id", data_type="varchar(200)"),
        ],
    )
    tr = TableSchema(
        name="ods_sku__stock_transfer",
        schema_name="ods_ecommerce_0_SH",
        db_type="doris",
        fields=[
            FieldSchema(name="id", data_type="bigint(20)"),
            FieldSchema(name="sku_id", data_type="bigint(20)"),
            FieldSchema(name="in_org_id", data_type="bigint(20)"),
            FieldSchema(name="out_org_id", data_type="bigint(20)"),
            FieldSchema(name="type", data_type="int(11)"),
            FieldSchema(name="transfer_reason", data_type="varchar(500)"),
        ],
    )
    exp = SchemaExploration(
        source_db_id="prod-doris",
        tables=[inv, tr],
    )
    return {"prod-doris#ods": exp}


INV_SQL = """
INSERT INTO dwd_stock_inventory_info (id, sku_id, stock_inventory_id, status, tenant_id)
SELECT
  NULLIF(CAST(id AS STRING), '') AS id,
  NULLIF(CAST(sku_id AS STRING), '') AS sku_id,
  NULLIF(CAST(stock_inventory_id AS STRING), '') AS stock_inventory_id,
  CAST(NULLIF(status, '') AS TINYINT) AS status,
  NULLIF(tenant_id, '') AS tenant_id
FROM ods_ecommerce_0_SH.ods_sku__stock_inventory_info
WHERE id != '' AND sku_id != '' AND stock_inventory_id != '';
"""


def test_strips_numeric_where_keeps_varchar_nullif():
    types = column_types_from_explorations(_inv_exploration())
    out = rewrite_typed_empty_string_guards(INV_SQL, types)
    assert "id != ''" not in out
    assert "sku_id != ''" not in out
    assert "stock_inventory_id != ''" not in out
    assert "WHERE" not in out.upper()
    assert "NULLIF(CAST(id AS STRING), '')" not in out.upper().replace(" ", "")
    assert "CAST(NULLIF(status, '')" not in out.upper().replace(" ", "")
    assert "NULLIF(tenant_id, '')" in out
    assert "SELECT" in out.upper()
    assert "id AS id" in out or ",\n  id," in out or "  id," in out or "  id\n" in out or "  id AS" in out


def test_keeps_varchar_empty_string_filter():
    sql = (
        "INSERT INTO dwd_t (id, reason) SELECT id, transfer_reason "
        "FROM ods_ecommerce_0_SH.ods_sku__stock_transfer "
        "WHERE transfer_reason != '' AND id != '';"
    )
    types = column_types_from_explorations(_inv_exploration())
    out = rewrite_typed_empty_string_guards(sql, types)
    assert "transfer_reason != ''" in out
    assert "id != ''" not in out
    assert "WHERE transfer_reason != ''" in out


def test_unknown_types_are_left_alone():
    sql = "INSERT INTO t (id) SELECT id FROM mystery WHERE id != '';"
    types = column_types_from_explorations({})
    assert rewrite_typed_empty_string_guards(sql, types) == sql


def test_sanitize_rewrites_pipe_concat():
    """Task 60a570e6: LLM fallback INSERT still used ``||`` concat."""
    arts = [
        SQLArtifact(
            layer="ADS", target_table="ads_x", sql_type="DML_INSERT",
            sql=(
                "INSERT INTO ads_x (stat_quarter) SELECT "
                "SUBSTRING(a.stat_month, 1, 4) || '-Q' || CEIL(1) "
                "AS `stat_quarter` FROM dws_x a"
            ),
        ),
        SQLArtifact(
            layer="ADS", target_table="ads_x", sql_type="DDL",
            sql="CREATE TABLE ads_x (stat_quarter VARCHAR(7))",
        ),
    ]
    n = sanitize_sql_artifacts(arts, {}, layer="ADS")
    assert n == 1
    assert "||" not in arts[0].sql
    assert "CONCAT(" in arts[0].sql
    assert "CREATE TABLE" in arts[1].sql


def test_sanitize_sql_artifacts_only_touches_dml():
    types_state = _inv_exploration()
    arts = [
        SQLArtifact(layer="DWD", target_table="dwd_x", sql_type="DDL",
                    sql="CREATE TABLE dwd_x (id BIGINT)"),
        SQLArtifact(layer="DWD", target_table="dwd_x", sql_type="DML_INSERT",
                    sql=INV_SQL),
    ]
    n = sanitize_sql_artifacts(arts, types_state, layer="DWD")
    assert n == 1
    assert "id != ''" not in arts[1].sql
    assert "CREATE TABLE" in arts[0].sql


def test_classify_strict_decimal_cast_tells_remove_guards():
    from wren_agent.tools.dolphin_scheduler import classify_execution_error

    log = (
        "execute sql error: errCode = 2, detailMessage = "
        " can't cast to decimal in strict mode."
    )
    c = classify_execution_error(task_instance_id="1", raw_log=log)
    assert c.error_type == ErrorType.LOGIC
    assert "NULLIF" not in c.summary or "remove" in c.summary.lower() or "去掉" in c.summary or "不要" in c.summary
    assert any(tok in c.summary for tok in ("!= ''", "空串", "decimal", "strict"))


def test_classify_parse_number_fail_still_adds_varchar_guards():
    from wren_agent.tools.dolphin_scheduler import classify_execution_error

    log = (
        "execute sql error: errCode = 2, detailMessage = "
        "(127.0.0.1)[INVALID_ARGUMENT]parse number fail, string: ''"
    )
    c = classify_execution_error(task_instance_id="1", raw_log=log)
    assert c.error_type == ErrorType.LOGIC
    assert "NULLIF" in c.summary


def test_repair_hint_for_strict_decimal_cast():
    from wren_agent.nodes.validation import _failure_from_execution_error, _repair_hint

    class Err:
        raw_log = (
            "name=sql_dwd_2_dwd_stock_inventory_info\n"
            "errCode = 2, detailMessage =  can't cast to decimal in strict mode."
        )
        summary = "cast fail"

    vr = _failure_from_execution_error(Err())
    assert vr is not None
    assert vr.layer == "DWD"
    check = vr.failures[0]["check"]
    assert check == "numeric_empty_string_compare"
    hint = _repair_hint(vr.failures[0])
    assert "VARCHAR" in hint or "STRING" in hint
    assert "!=" in hint or "NULLIF" in hint
