---
name: design-dim
description: Generate DIM (Dimension) layer DDL. LLM acts as primary designer producing complete dimension table designs with SCD Type 2 handling and DimensionRegistry integration.
---

# Design DIM Layer — Generative Guide

You are designing the **DIM (Dimension) layer** for a data warehouse modeling task.
Produce complete dimension table designs including DDL, surrogate keys, and SCD Type 2 columns.

## Layer Principles (from CONTEXT.md)

> DIM (公共维度层): 全企业统一的维度字典（时间、地域、商品等），确保维度属性口径一致。
> 跨 ModelingTask 共享，受 DimensionRegistry 治理。

1. **Shared across tasks**: Dimensions are enterprise-wide. Check for existing dimensions before creating new ones.
2. **Additive-only changes**: Existing dimensions can only be extended (add columns), never modified or deleted.
3. **Surrogate keys**: Every dimension table uses a surrogate key (`{dim_name}_id BIGINT`) for internal joins.
4. **SCD Type 2**: Track historical changes with `effective_date`, `expiration_date`, `is_current`.

## Dimension Candidate Identification

A source table is a dimension candidate when:
- Row count ≤ 500,000 (small enough to be a lookup/reference table)
- Has exactly 1 primary key column (simple lookup, not composite bridge table)
- Contains mostly descriptive/attribute columns (names, codes, categories)

**Do NOT create dimensions for**:
- Tables > 500K rows (likely fact tables → DWD layer)
- Tables with composite primary keys (bridge/association tables)
- Tables with only numeric measure columns (fact tables)

## DimensionRegistry Rules

When processing each dimension candidate:
1. **Check for existing dimensions** first: look at the `existing_dimensions` provided in context
2. **If an existing dimension matches**: reuse it (`source: "from_existing_dim"`), do NOT generate new DDL
3. **If creating new**: register it with `source: "from_source_table"`, version 1

## DDL Generation Rules

### Column Specification
Every NEW dimension DDL must include these columns in order:

1. **Surrogate key**: `{dim_name}_id BIGINT NOT NULL COMMENT '代理键'` — always first
2. **Business attributes**: all source table columns as `VARCHAR(255)` with **Chinese** comments
   - Rename common columns to avoid clashes: `id` → `dim_id`, `name` → `dim_name`, `code` → `dim_code`
   - Comment rule: use the source column's Chinese comment verbatim if present; otherwise derive a Chinese comment from the column name (e.g. `name` → `名称`, `code` → `编码`)
3. **SCD Type 2 columns** (always appended):
   - `effective_date DATE COMMENT 'SCD 类型2 生效日期'`
   - `expiration_date DATE COMMENT 'SCD 类型2 失效日期'`
   - `is_current TINYINT DEFAULT 1 COMMENT 'SCD 类型2 当前标志(1=当前,0=已失效)'`

### Doris Constraints (CRITICAL)
1. **DUPLICATE KEY must use the surrogate key column** (first column, BIGINT — always safe)
2. **NEVER use BOOLEAN** — use `TINYINT` with `DEFAULT 1` or `DEFAULT 0`
3. **NEVER use STRING in KEY columns** (not an issue since surrogate key is BIGINT)
4. **NEVER use DEFAULT TRUE** — use `DEFAULT 1` instead

### DDL Template

```sql
CREATE TABLE IF NOT EXISTS {dim_table} (
  {dim_name}_id BIGINT NOT NULL COMMENT '代理键',
  {attribute_columns_with_chinese_comments},
  effective_date DATE COMMENT 'SCD 类型2 生效日期',
  expiration_date DATE COMMENT 'SCD 类型2 失效日期',
  is_current TINYINT DEFAULT 1 COMMENT 'SCD 类型2 当前标志(1=当前,0=已失效)'
)
DUPLICATE KEY({dim_name}_id)
COMMENT '维度表: {source_table}';
```

**Mandatory**: every column and the table itself carry a **Chinese** COMMENT — no column may be left without one. Escape single quotes in comment values as `''`.

## Load SQL (MANDATORY for new dimensions)

A CREATE TABLE is not a pipeline. Every NEW dimension (`source: "from_source_table"`) must also include executable `load_sql` that copies current ODS rows into the DIM table.

```sql
INSERT INTO {dim_table} ({dim_name}_id, {attribute_columns}, effective_date, expiration_date, is_current)
SELECT {natural_key}, {source_attribute_columns}, CURRENT_DATE(), CAST('9999-12-31' AS DATE), 1
FROM {ods_landing_db}.{ods_table}
```

Rules:
- **Explicit INSERT column list** in DDL column order (surrogate key, attributes, SCD2 columns).
- **Qualify the ODS source** with the ODS landing database from context (`ods_gz_test.ods_operate_customer`). DIM SQL runs on the DIM datasource — an unqualified `FROM ods_x` looks in the wrong database.
- **Bind SELECT columns to the explored ODS Columns list.** Use the exact physical names (`floor_code`, `floor_name`, `room_name`) — never guess `code`/`name`/`tenant_name` when those names are absent from Columns (task de018b42).
- Surrogate key SELECT uses the source natural key (usually `id`).
- SCD2 bootstrap: `effective_date = CURRENT_DATE()`, `expiration_date = DATE '9999-12-31'`, `is_current = 1`.
- Reused dimensions (`source: "from_existing_dim"`) must NOT emit `ddl` or `load_sql`.

## Output Format

Return a single JSON object:

```json
{
  "layer": "DIM",
  "designs": [
    {
      "dim_name": "string — e.g., dim_financial_account_bank",
      "dim_table": "string — same as dim_name",
      "source": "from_source_table | from_existing_dim",
      "source_table": "string | null",
      "existing_dim_ref": "string | null — dim_name if reusing existing",
      "fields": [
        {"name": "string", "source_type": "string", "comment": "string — 中文注释"}
      ],
      "natural_key": ["string — original PK column name(s)"],
      "surrogate_key": "string — {dim_name}_id",
      "ddl": "string — complete CREATE TABLE IF NOT EXISTS (null if reusing existing)",
      "load_sql": "string | null — INSERT INTO ... SELECT from ODS (null if reusing existing)",
      "version": 1,
      "registered": false
    }
  ],
  "warnings": ["string"]
}
```

## Design Checklist

- [ ] Every candidate table evaluated against DimensionRegistry before creating new
- [ ] Surrogate key is BIGINT, first column, used as DUPLICATE KEY
- [ ] All SCD Type 2 columns present and correctly typed
- [ ] `is_current` uses TINYINT DEFAULT 1 (not BOOLEAN DEFAULT TRUE)
- [ ] Common column names renamed to avoid clashes (id→dim_id, name→dim_name)
- [ ] Every column and the table carry a **Chinese** COMMENT (表级+字段级)
- [ ] Existing dimensions referenced, not duplicated
- [ ] Every NEW dimension has executable `load_sql` (INSERT ... SELECT from ODS), not only DDL
- [ ] `load_sql` SELECT columns are exact names from the ODS table's Columns list
