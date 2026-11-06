---
name: design-dwd-dws
description: Generate DWD (Detail Fact) and DWS (Summary) layer DDL. LLM acts as primary designer producing fact and aggregation table designs with grain declarations, measure detection, and dimension associations.
---

# Design DWD + DWS Layers — Generative Guide

You are designing the **DWD (Detail Fact) layer** and **DWS (Summary) layer** for a data warehouse.
Produce complete fact table and aggregation designs with explicit grain declarations and measure definitions.

## Layer Principles (from CONTEXT.md)

### DWD (明细事实层)
> 以业务过程为驱动，清洗、去重并集成各业务系统的明细数据。
> 统一数据格式，结合维度做轻度宽表化。

1. **Business process driven**: Each fact table represents one business process.
2. **Explicit grain**: Every fact table MUST declare its grain (e.g., "one row per transaction per day").
3. **Measure/attribute separation**: Numeric columns are measures; non-numeric are dimension attributes.
4. **Dimension references**: Link to DIM tables via foreign-key-like column references.

### DWS (公共汇总层)
> 以分析主题为驱动，按通用维度对明细数据进行汇总，构建派生指标的汇总宽表。
> 一致性粒度约束：同一汇总表中的度量必须具有相同的业务粒度。

1. **Analysis topic driven**: Group DWD facts by common dimensions.
2. **Consistent grain within each DWS table**: All measures in one DWS table must share the same group-by dimensions.
3. **Aggregation from DWD**: Every DWS measure derives from a DWD fact measure via SUM/COUNT/AVG/etc.

## DWD Fact Table Design

### Fact Candidate Identification

A source table is a fact candidate when EITHER:
- It has **genuine numeric measure columns** (see Measure Detection below) → these are **measures**
- It has a non-zero row count estimate (even small tables can be facts in some domains)

Skip only when: no genuine numeric columns AND row count is 0 (empty table).

### Measure Detection (CRITICAL — BIGINT is NOT a measure by default)

In Chinese business databases, **BIGINT columns are almost always snowflake IDs**
(primary keys, foreign keys, org_id, user_id, dept_id, etc.) — NOT business quantities.
Treat them accordingly.

#### Type-Based Classification

**Preferred measure types** (always candidate measures unless they are primary keys):

| Type | Treatment | Example columns |
|------|-----------|----------------|
| `DECIMAL` / `NUMERIC` | **Always measure candidate** | `loan_amount`, `interest_rate`, `balance` |
| `DOUBLE` / `FLOAT` | **Always measure candidate** | `exchange_rate`, `percentage` |

**Conditional types** (only measures when naming suggests quantity):

| Type | Default | Becomes measure ONLY when |
|------|---------|--------------------------|
| `BIGINT` | **NOT a measure** (assume snowflake ID) | Column name contains quantity keywords |
| `INT` / `INTEGER` | **NOT a measure** by default | Column name contains quantity keywords AND NOT a status/flag |
| `TINYINT` / `SMALLINT` | **NOT a measure** (usually status flags) | Only if name strongly suggests count (rare) |

**Excluded types** (never measures):

| Type | Reason |
|------|--------|
| `VARCHAR` / `STRING` / `TEXT` | Non-numeric |
| `DATE` / `DATETIME` / `TIMESTAMP` | Temporal, not quantitative |
| `BOOLEAN` / `BIT` | Binary flag |
| `JSON` / `BLOB` | Non-aggregatable |

#### BIGINT / INT Column Name Analysis

When evaluating whether a BIGINT or INT column could be a measure, apply these rules:

**Exclude as ID / FK / Key (NOT measures):**
- Name ends with `_id`, `_key`, `_no`, `_code` → foreign key / identifier
- Name starts with `id_` → identifier pattern
- Name is `id` alone → primary key

**Exclude as status / flag / version (NOT measures):**
- Name contains: `status`, `flag`, `is_`, `has_`, `version`, `ts_`, `deleted`, `approved`
- Name contains: `_version`, `_status`, `_flag`, `_state`, `_type`
- Name matches: `parent_id`, `create_user`, `create_by`, `update_user`, `org_id`, `dept_id`, `tenant_id`, `group_id`

**Accept as count/quantity measure ONLY when name contains quantity keywords:**
- English: `count`, `cnt`, `num`, `qty`, `quantity`, `times`, `total`, `amount`, `sum`
- Chinese: `数量`, `次数`, `笔数`, `个数`, `件数`, `人数`, `天数`, `金额`, `总额`, `余额`, `合计`
- Pattern: `*_count`, `*_num`, `*_qty`, `total_*`, `*_total`

If a BIGINT/INT column is NOT in the accept list AND NOT clearly a quantity,
**exclude it from measures**. False negatives (missing a real count) are better than
false positives (treating IDs as metrics).

#### Decision Flow

```
For each non-PK column:
  1. DECIMAL/NUMERIC/DOUBLE/FLOAT? → MEASURE candidate ✓
  2. BIGINT? → Check name:
     a. _id/_key/_no/code/status/flag/is_/has_/version/ts_/deleted/approved? → ATTRIBUTE ✗
     b. count/cnt/num/qty/times/total/数量/次数/笔数? → MEASURE candidate ✓
     c. Everything else → ATTRIBUTE ✗ (assume snowflake ID)
  3. INT/TINYINT/SMALLINT? → Same rules as BIGINT (step 2)
  4. VARCHAR/DATE/others? → ATTRIBUTE ✗
```

#### Examples

| Column | Type | Verdict | Reason |
|--------|------|---------|--------|
| `loan_amount` | DECIMAL(20,2) | **MEASURE** | DECIMAL type |
| `repayment_count` | BIGINT | **MEASURE** | Name contains "count" |
| `total_limit` | BIGINT | **MEASURE** | Name contains "total" |
| `org_id` | BIGINT | attribute | Ends with `_id` |
| `parent_id` | BIGINT | attribute | Ends with `_id` |
| `status` | TINYINT | attribute | Status flag |
| `is_deleted` | TINYINT | attribute | Flag with `is_` |
| `ts_version` | BIGINT | attribute | Version number |
| `create_user` | BIGINT | attribute | User ID |
| `debt_report_id` | BIGINT | attribute | Foreign key |
| `dept_id` | BIGINT | attribute | Foreign key |
| `def_long1` | BIGINT | attribute | BIGINT, no quantity keyword |
| `def_long2` | BIGINT | attribute | BIGINT, no quantity keyword |
| `visit_times` | INT | **MEASURE** | Name contains "times" |
| `笔数` | BIGINT | **MEASURE** | Chinese quantity keyword |
| `审批状态` | TINYINT | attribute | Status in Chinese |

### Measure Expression

For each identified measure:
- **Additive** (DECIMAL/INT types): `SUM({column_name})`
- **Non-additive** (FLOAT/DOUBLE types): direct column reference
- **Type annotation**: `"additive"` or `"non_additive"`

### Dimension Association

Match DWD fact tables to DIM tables by:
1. Check if the dim base name (e.g., `dim_base` from `dim_financial_account_bank` → `financial_account_bank`) appears in the source table name
2. Check if any dim column name appears in the source table's field names
3. Only associate if the match is semantically meaningful

### Multi-Value References (MANDATORY — never comma-join IDs, never row-explode)

A contract/order often references MULTIPLE store, product, or units (one-to-many).
NEVER store those references as a comma-joined string in a single TEXT column:
at execution, downstream joins that `CAST(col AS BIGINT)` crash with
`parse number fail, string: 'id1,id2'` (task 55f47744 — a contract with 16
comma-joined product IDs broke every DWS load).

**THIS Doris cluster does NOT support `LATERAL VIEW`, `UNNEST`, or
`EXPLODE_SPLIT`** (probe-verified, task 13d02e94: `mismatched input 'LATERAL'`
failed every DWS load). Therefore:

1. **Never write row-explosion SQL.** If a column holds comma-separated IDs,
   aggregate at that column's OWN grain (contract/cycle/unit) — do not attempt
   to split it into rows.
2. **Prefer a child/bridge table** for true one-to-many relationships
   (e.g. `dwd_{process}_ref_map` with one row per parent×child key).
3. **Never `GROUP_CONCAT`/concat IDs** into a single column during load.
4. When the goal needs a per-product/per-shop breakdown, join to an existing
   data-bearing table that already has one row per product/shop — never
   explode a comma-joined string.

### Empty-String Guards (MANDATORY, type-aware)

**Look at the source column type in the schema dump before writing a guard.**

- **VARCHAR / STRING / CHAR / TEXT**: these can hold `''`. Filter with
  `WHERE {col} != ''` or `NULLIF({col}, '')` when the column is fed to
  `DATE_FORMAT` / `CAST` / `QUARTER` (empty strings crash those). `IS NOT NULL`
  alone does NOT exclude empty strings — `CAST('' AS BIGINT)` /
  `DATE_FORMAT('', '%Y%m')` still crash (task 55f47744).
  **Do not** put `!= ''` on a VARCHAR **grain** column (地点/名称等维度键).
  NULL there is a missing dimension member — `COALESCE(NULLIF(col, ''), '未知')`
  in SELECT/GROUP BY, not a row filter (task e3914a7e: location `!= ''`
  materialized an empty DWS). The compiler rewrites grain `!= ''` the same way.
- **BIGINT / INT / DECIMAL / DATE / DATETIME / TINYINT**: these **cannot**
  hold `''` (missing values are NULL). **Never** write `{col} != ''`,
  `NULLIF({col}, '')`, or `NULLIF(CAST({col} AS STRING), '')`. Doris INSERT
  with `enable_insert_strict` then throws `can't cast to decimal in strict
  mode` (task 0af225c9). Pass the column through as-is, or `IS NOT NULL` if
  you must drop nulls.

### Grain Declaration

Grain should be the most atomic level:
- Primary key columns if available
- Fallback to `["_row_"]` if no PK exists
- Format: `["column1", "column2"]`

### DWD DDL Template

```sql
-- 粒度: 每行代表 ({grain_columns})
CREATE TABLE IF NOT EXISTS {dwd_table} (
  {attribute_columns_with_types_and_chinese_comments}
)
DUPLICATE KEY({first_grain_column})
COMMENT '明细事实层: {dwd_table}';
```

**Important**: DWD DDL only includes attribute columns (non-measure fields).
Measures are stored in the design metadata for DWS aggregation, not in the DWD DDL.
Every attribute column MUST carry a Chinese `COMMENT` (use the source comment verbatim if Chinese, otherwise derive from the column name).

### DWD Load SQL (MANDATORY)

Every DWD fact must include executable `load_sql` in addition to `ddl`. CREATE TABLE alone leaves an empty fact table.

```sql
INSERT INTO {dwd_table} ({attribute_columns_in_ddl_order})
SELECT {source_columns}
FROM {ods_landing_db}.{ods_table}
```

- **Explicit INSERT column list** matching the DDL attribute columns (and grain).
- **Qualify the ODS source** with the ODS landing database from context. DWD SQL runs on the DWD datasource — an unqualified `FROM ods_x` looks in the wrong database.
- **Bind SELECT columns to the explored ODS Columns list.** Use exact physical names; never invent `code`/`name` aliases (task de018b42).
- Do not INSERT measure expressions into DWD; measures stay in design metadata for DWS.

## DWS Summary Table Design

### Topic-Driven Summary Generation

Design DWS tables from the **analysis topic and requested query grains**, not by
blindly creating one table per DWD fact or taking the first two dimensions.

1. Parse the modeling goal into explicit requirements: requested grains,
   atomic measures, derived metrics, time windows, and audiences.
2. For every requested derived metric, work backwards to its required atomic
   measures. Example: `occupancy_rate = leased_area / rentable_area` requires
   both `leased_area` and `rentable_area` in DWS, even if the goal names only
   the rate.
3. Group compatible atomic measures into a topic-wide table only when they
   share exactly the same grain. Split daily/monthly/yearly or
   shop/unit/customer grains into separate tables when they differ.
4. Include all useful query dimensions at that grain plus descriptive
   attributes that are functionally dependent on it. Do not impose an
   arbitrary two-dimension limit. `group_by_dimensions` must COVER every
   grain column (grain ⊆ group_by); extra columns are allowed only as
   descriptive attributes functionally dependent on the grain (e.g.
   `contract_code` alongside `contract_id`) — never add independent
   dimensions, which would silently duplicate rows under the UNIQUE KEY.
5. If a requested grain or atomic measure cannot be sourced, mark it
   `partial` or `unsupported` in `requirement_coverage`; never silently drop or
   weaken it. In warehouse mode this must trigger the insufficient-warehouse
   path rather than claiming complete support.

Table naming follows the actual **entity + period grain**, not just the
topic and a time suffix. Pattern:

`dws_{topic}_{grain_entities}_{period}`

- **topic**: first DWD/fct source with `dwd_` / trailing `info`/`fact`/`detail` stripped (`dwd_stock_inventory_info` → `stock_inventory`)
- **grain_entities** (short tokens, grain order, skip the period column): `sku_id`→`card`, `storage_location`→`loc`, `org_id`→`org`, `building_id`→`bldg`, `store_id`/`room_id`→`store`/`product`, `unit_id`→`unit`, `company_id`→`company`
- **period** last: `stat_month`/`inventory_month`→`monthly`, date/`trans_date`→`daily`, quarter→`quarterly`, year→`yearly`

Examples (these two MUST NOT share a name):

- grain = sku_id + storage_location + month → `dws_stock_inventory_card_loc_monthly`
- grain = org_id + month → `dws_stock_inventory_org_monthly`
- grain = shop + day → `dws_order_bldg_daily`

Doris identifiers are **≤ 64 characters**. If the composed name would overflow, drop topic fillers first (`info`, `detail`, `data`, `snapshot`, `extended`) — never drop the `dws_` prefix, entity tokens, or the period suffix. The compiler rewrites a generic `dws_{topic}_{period}` that is missing grain entities.

### Measure Aggregation

For each DWD measure (already filtered — only genuine numeric measures reach this stage):
- **Name**: prefix with `daily_` → `daily_{measure_name}`
- **Expression**: wrap in aggregation function (SUM for additive, AVG for non-additive)
- **Type**: preserve from source measure
- **Doris type**: `DECIMAL(20,2)` for monetary/ratio measures, `BIGINT` for pure row counts
  - Most aggregated measures → `DECIMAL(20,2)`
  - `BIGINT` ONLY for measures that are explicitly row-count metrics (e.g., `daily_repayment_count`, `daily_loan_times`)

**Important**: If a DWD fact table has ZERO genuine measures after BIGINT filtering,
create the DWD table (attributes only) but do NOT generate a DWS summary for it.
A DWS table without measures is meaningless.

### DWS DDL Template

```sql
-- 粒度: ({group_by_dimensions})
CREATE TABLE IF NOT EXISTS {dws_table} (
  {grain_column_1} VARCHAR(255) COMMENT '中文维度注释',
  {grain_column_2} VARCHAR(255) COMMENT '中文维度注释',
  {descriptive_columns VARCHAR(255) COMMENT '中文注释'},
  {measure_columns DECIMAL(20,2) COMMENT '中文指标注释'}
)
UNIQUE KEY({grain_column_1}, {grain_column_2})
COMMENT '汇总表: {dws_table}'
DISTRIBUTED BY HASH({grain_column_1}) BUCKETS AUTO;
```

Every column and the table itself MUST carry a **Chinese** COMMENT — derive measure column comments from the measure's business meaning (e.g. `daily_repayment_count` → `每日还款笔数`).

### Doris Constraints (CRITICAL — DDL is executed verbatim on Doris)

1. **Use the UNIQUE KEY model, never AGGREGATE KEY.** DWS data is written by
   `INSERT INTO ... SELECT ... GROUP BY` — already aggregated. UNIQUE KEY gives
   idempotent re-runs (same-key rows are replaced) and, unlike AGGREGATE KEY,
   does NOT require an aggregate type on every non-key column.
2. **Key columns MUST be an ordered prefix of the column list.** The columns
   named in `UNIQUE KEY(a, b, c)` must be the FIRST columns defined in the
   table, in exactly that order. Doris rejects any other arrangement
   ("Key columns should be a ordered prefix of the schema").
3. **UNIQUE KEY(...) = the grain columns (`group_by_dimensions`), all of them,
   in order.** Descriptive attributes (e.g. `company_name` alongside
   `company_id`) are NOT keys — place them AFTER the key columns and BEFORE the
   measure columns. They take the last-written value per key, which is correct
   for attributes functionally dependent on the grain.
   **Never add entity columns to the key beyond the grain** — a unit-level
   table (`grain = [org_id, snapshot_month]`) must NOT be keyed by
   `basics_room_id` even when a product column is present for reference (task
   81df9f70: `unique_key_matches_grain` failed unit/contract tables whose
   UNIQUE KEY mixed in a finer entity). The harness rejects any UNIQUE KEY
   that is not EXACTLY the declared grain.
4. **Key columns must NOT be STRING** — use `VARCHAR(255)`.
5. **Measure columns use DECIMAL(20,2)** for amounts (BIGINT for pure counts)
   and always come last.
6. **UNIQUE KEY tables MUST declare `DISTRIBUTED BY HASH(<first key column>)
   BUCKETS AUTO`.** Doris defaults to RANDOM distribution, which UNIQUE KEY
   tables reject ("should not contain random distribution desc").
7. **The table-level `COMMENT` MUST come BEFORE `DISTRIBUTED BY`.** Doris
   grammar order is: columns → `UNIQUE KEY(...)` → `COMMENT '...'` →
   `DISTRIBUTED BY ... BUCKETS ...`. Putting COMMENT after BUCKETS is a
   syntax error ("mismatched input 'COMMENT'") — see the template above.

### Materialization SQL (MANDATORY)

Every DWS design must include executable `load_sql` in addition to `ddl`.
`load_sql` must be an idempotent `INSERT INTO ... SELECT ... GROUP BY ...`
statement that populates every declared grain and measure column, reads only
from `source_dwd_table` / `source_tables`, and aliases expressions to the exact
physical target-column names. Warehouse-mode sources must be fully qualified.
Every `_daily` table must materialize a physical date column such as
`stat_date`. A DWS response containing only CREATE TABLE statements is invalid.

**Doris DML dialect (CRITICAL — load SQL executes verbatim on Doris)**: the
warehouse mode does NOT EXPLAIN DML before deploy, so dialect mistakes surface
only at execution. Use only Doris-valid functions and date syntax:
- `TO_DATE(expr)` is **single-argument** in Doris — never `TO_DATE(x, 'yyyy-MM')`.
  Use `DATE_FORMAT(x, '%Y-%m')` for month extraction, `SUBSTRING(x, 1, 7)` or
  `DATE_TRUNC('month', x)`.
- Date format patterns use **`%Y`-style** tokens (`'%Y-%m-%d'`, `'%Y-%m'`),
  NOT `yyyy-MM` (Spark style).
- `CURRENT_DATE()` / `NOW()` are functions with parens; `QUARTER(date)` exists.
- String concat: `CONCAT(a, b)` — **never `||`**. Doris parses `||` as
  boolean OR, so `SUBSTRING(month,1,4) || '-Q'` fails with
  `'-Q' can't cast to bool in strict mode` (task 60a570e6). The compiler
  rewrites literal `||` chains to `CONCAT`. Use `COALESCE`,
  `IF(cond, a, b)` (not `IFNULL`-only patterns) — both are fine, but never
  `NVL` with a two-arg TO_DATE-style combo that Doris rejects.
- When in doubt, prefer the same functions you see in the Source Schema typed
  samples above.

**Join keys: never CAST a TEXT key directly (MANDATORY)**. A TEXT join key may
hold comma-joined multi-values or empty strings; `CAST(text_key AS BIGINT)`
then fails at execution with `parse number fail` (task 55f47744 —
`store_id`/`basics_room_id` held 16 comma-joined snowflake IDs plus
empty strings). Rules:
1. Never write `CAST(text_col AS BIGINT)` on a TEXT join key unless the column
   is single-valued AND guarded: `CAST(NULLIF(col, '') AS BIGINT)`.
2. **Never use `LATERAL VIEW` / `UNNEST` / `EXPLODE_SPLIT`** — this Doris
   cluster rejects them (`mismatched input 'LATERAL'`, task 13d02e94).
   Multi-valued keys must be joined at their own grain; string equality on the
   raw value (`dim.id = text_col` after guarding `text_col != ''`) never throws
   `parse number fail`.
3. Prefer parse-safe string comparison when the dim key is numeric:
   `CAST(dim.id AS STRING) = text_col` — string equality never throws
   `parse number fail`.
4. Every **TEXT** join-key also needs the empty-string guard
   (`WHERE text_key != ''` — `IS NOT NULL` does NOT exclude `''`).
   Native BIGINT/INT join keys must NOT be compared to `''`.

**Explicit target column list (MANDATORY)**: write the INSERT with the full
target column list, exactly the DDL columns in order —
`INSERT INTO {table} (col1, col2, ...) SELECT ...`. The harness validator
extracts the target columns from this list to prove every declared field and
measure is materialized; a column-less `INSERT INTO {table} SELECT ...` cannot
be verified and is rejected. Never rely on positional column order.

### Consistency Contract (MANDATORY)

The harness cross-checks the design JSON, the DDL, and the load SQL against
each other. Keep all three in exact agreement:

1. **declared fields ⊆ DDL columns ⊆ INSERT target columns**: every name in
   `fields` must appear in the CREATE TABLE body, and every DDL column must be
   listed in the INSERT column list. Never declare a field you do not
   materialize, and never INSERT a column the DDL does not define.
2. **Every requested grain must materialize**: each entity/time grain named in
   the modeling goal (店铺 / 商品 / 单位 / 月 / 季度 …) must exist as columns in
   at least one DWS table of this plan. If an explored source fact table has
   the key column (e.g. `basics_room_id`), you MUST build that grain —
   declaring it "unsupported" is only allowed when the key column does not
   exist in ANY explored source.
3. **Never shadow an existing warehouse table**: in warehouse mode, when an
   explored DWS table already covers the requested grain, anchor on it (read /
   roll up FROM it) — do NOT create a new table with the same name, and never
   write `INSERT INTO <t> SELECT ... FROM <t>` (self-reference duplicates
   rows at execution).

**Correct example** (grain = trans_date + company_id):

```sql
CREATE TABLE IF NOT EXISTS dws_transaction_flow_daily (
  trans_date VARCHAR(255) COMMENT '交易日期',
  company_id VARCHAR(255) COMMENT '公司ID',
  company_name VARCHAR(255) COMMENT '公司名称',
  daily_amt DECIMAL(20,2) COMMENT '每日交易金额',
  daily_flow_count BIGINT COMMENT '每日交易笔数'
)
UNIQUE KEY(trans_date, company_id)
COMMENT '财务流水每日汇总表'
DISTRIBUTED BY HASH(trans_date) BUCKETS AUTO;
```

```sql
-- load_sql: explicit target column list, same columns as the DDL in order
INSERT INTO dws_transaction_flow_daily (trans_date, company_id, company_name, daily_amt, daily_flow_count)
SELECT DATE(trans_dt) AS trans_date,
       company_id,
       MAX(company_name) AS company_name,
       SUM(amt) AS daily_amt,
       COUNT(1) AS daily_flow_count
FROM dwd_transaction_flow
WHERE trans_dt IS NOT NULL
GROUP BY DATE(trans_dt), company_id;
```

**Wrong** (both rejected by Doris): `AGGREGATE KEY(trans_date, company_id)`
with plain non-key columns (needs aggregate types), or
`UNIQUE KEY(trans_date, company_id, cd_sign)` when `cd_sign` is not the third
defined column (key not an ordered prefix).

## Output Format

Return a single JSON object with both layers:

```json
{
  "layer": "DWD_DWS",
  "dwd_designs": [
    {
      "fact_name": "string — e.g., fact_financial_transaction_flow",
      "fact_table": "string — dwd_{source_table}",
      "business_process": "string — source table name",
      "source_ods_tables": ["string — ods_{source_table}"],
      "fields": [
        {"name": "string", "source_column": "string", "type": "string", "comment": "string — 中文注释"}
      ],
      "measures": [
        {"name": "string", "expression": "string — e.g., SUM(amount)", "type": "additive | non_additive"}
      ],
      "dimension_refs": ["string — dim table names"],
      "grain": ["string — grain columns"],
      "ddl": "string — complete CREATE TABLE statement",
      "load_sql": "string — complete INSERT INTO ... (explicit target column list) SELECT ... FROM {ods_db}.{ods_table}"
    }
  ],
  "dws_designs": [
    {
      "summary_name": "string — e.g., Daily summary of fact_xxx",
      "summary_table": "string — dws_{topic}_{grain_entities}_{period}",
      "source_dwd_table": "string",
      "fields": [{"name": "string", "type": "VARCHAR(255)", "comment": "string — 中文注释，必填"}],
      "measures": [
        {"name": "string", "expression": "string", "type": "string", "comment": "string — 中文注释，必填"}
      ],
      "group_by_dimensions": ["string"],
      "grain": ["string"],
      "refresh_logic": "incremental",
      "requirement_coverage": [
        {
          "requirement": "string — one requested grain, metric, or formula dependency",
          "status": "supported | partial | unsupported",
          "evidence": ["string — physical grain/field/measure names"],
          "gap_reason": "string — required when status is not supported"
        }
      ],
      "ddl": "string — complete CREATE TABLE with UNIQUE KEY (grain columns as ordered prefix)",
      "load_sql": "string — complete INSERT INTO ... (explicit target column list, DDL order) SELECT ... GROUP BY statement"
    }
  ],
  "warnings": ["string"]
}
```

## Design Checklist

- [ ] BIGINT/INT columns excluded from measures unless name contains quantity keywords (count/cnt/num/qty/total/数量/次数/笔数)
- [ ] `_id`, `_key`, `_no`, `_code` columns excluded (identifiers, not measures)
- [ ] `status`, `flag`, `is_`, `has_`, `version`, `deleted`, `approved` columns excluded
- [ ] DECIMAL/NUMERIC/DOUBLE/FLOAT columns always included as measure candidates
- [ ] `def_long*` and other generic BIGINT columns treated as attributes (not measures)
- [ ] Every fact candidate (has genuine numeric measures or non-zero rows) processed
- [ ] Grain explicitly declared for every fact table
- [ ] Every DWD table has executable `load_sql` (INSERT ... SELECT from ODS), not only DDL
- [ ] DWD `load_sql` qualifies the ODS source with the ODS landing database
- [ ] DWD `load_sql` SELECT columns are exact names from the ODS table's Columns list
- [ ] Measures correctly separated from dimension attributes
- [ ] DIM associations semantically validated, not just string-matched
- [ ] DWS uses UNIQUE KEY model (not AGGREGATE KEY)
- [ ] DWS UNIQUE KEY columns = grain columns, defined FIRST and in key order (ordered prefix)
- [ ] DWS DDL declares DISTRIBUTED BY HASH(first key column) BUCKETS AUTO
- [ ] DWS key columns use VARCHAR(255) not STRING
- [ ] DWS measure columns use DECIMAL(20,2) (BIGINT only for pure row counts) and come last
- [ ] DWS grain constraints enforced (same grain across all measures in one table)
- [ ] Modeling goal decomposed into requested grains, metrics, and formula dependencies
- [ ] Every derived metric's numerator/denominator/base measures are materialized in DWS
- [ ] No arbitrary dimension-count limit; all useful dimensions at the declared grain evaluated
- [ ] Different time/entity grains split into separate DWS tables with distinct names (`dws_{topic}_{entities}_{period}`, ≤64 chars)
- [ ] Every requested item appears once in `requirement_coverage`; unsupported items are explicit
- [ ] Every DWS table has executable `load_sql`, not only DDL
- [ ] `load_sql` declares the explicit INSERT target column list (DDL column order)
- [ ] Consistency: declared `fields` ⊆ DDL columns ⊆ INSERT target columns
- [ ] Every requested grain (店铺/商品/单位/月/季度) materializes in ≥1 DWS table; never mark a grain unsupported when an explored source has its key column
- [ ] Derived quantities are computed from existing columns (未收 = 应收 − 实收), never invented column names or substituted semantics
- [ ] No DWS table shadows an explored warehouse table with the same name; no `INSERT INTO t SELECT ... FROM t` self-reference
- [ ] Every `_daily` table has a physical date field in grain, DDL, and load SQL
- [ ] DWD facts with zero genuine measures still get attribute DDL, but NO empty DWS generated
- [ ] No tables skipped that have valid measures
- [ ] Every DWD/DWS column and table carries a **Chinese** COMMENT (表级+字段级, `-- 粒度` 头注释为中文)
- [ ] `fields`/`measures` 的 JSON 条目都带 `comment` 中文注释——下游层（ADS/revise）靠它理解字段语义，缺失会导致臆造列名
- [ ] One-to-many references (合同→房/楼) exploded into rows or a child table — NO comma-joined IDs in a single TEXT column
- [ ] Load SQL empty-string guards are type-aware: `WHERE col != ''` / `NULLIF(col, '')` only on VARCHAR/STRING/CHAR/TEXT; native BIGINT/INT/DECIMAL/DATE columns are never compared to `''`
- [ ] No `CAST(text_col AS BIGINT)` on TEXT join keys (multi-value or empty strings crash with `parse number fail`)

## Compiler Mode (LLM_LLM_COMPILER=true)

When the task runs with the DesignCompiler enabled (ADR-0015), the DWS layer
uses a **semantic output contract**: you provide the STRUCTURE, the compiler
renders the Doris DDL and load SQL deterministically. Do NOT write `ddl` /
`load_sql` text in this mode.

```json
{
  "semantic_designs": [
    {
      "summary_name": "string",
      "summary_table": "string — dws_{topic}_{grain_entities}_{period}",
      "columns": [
        {"name": "trans_date", "dtype": "DATE", "comment": "交易日期", "is_key": true, "source_expr": "DATE(trans_dt)"},
        {"name": "company_id", "dtype": "BIGINT", "comment": "公司ID", "is_key": true, "source_expr": "company_id"},
        {"name": "company_name", "dtype": "VARCHAR(255)", "comment": "公司名称", "source_expr": "MAX(company_name)"},
        {"name": "daily_amt", "dtype": "DECIMAL(20,2)", "comment": "每日交易金额", "is_measure": true, "source_expr": "SUM(amt)"},
        {"name": "daily_flow_count", "dtype": "BIGINT", "comment": "每日交易笔数", "is_measure": true, "source_expr": "COUNT(1)"}
      ],
      "sources": ["dwd_transaction_flow"],
      "joins": [{"right": "db_dwd.dwd_xxx", "on": "a.id = b.id"}],
      "filters": ["trans_dt IS NOT NULL"],
      "requirement_coverage": [{"requirement": "string", "status": "supported", "evidence": ["string"]}]
    }
  ],
  "warnings": ["string"]
}
```

Compiler-mode rules (the compiler validates and renders; violations fall
back to the repair loop):

- **`source_expr` is the SELECT expression** that produces the column: an
  aggregation (`SUM(col)` / `COUNT(1)` / `MAX(col)` / `MIN(col)` / `AVG(col)`),
  a Doris-safe scalar function (`DATE` / `DATE_FORMAT` / `DATE_TRUNC` /
  `SUBSTRING` / `COALESCE` / `IF` / `NULLIF` / `CONCAT` / `QUARTER` / `YEAR` /
  `MONTH` / `CAST` / `ABS` / `ROUND` / `TRIM` / `LTRIM` / `RTRIM`), a plain
  column, or arithmetic combining those. Subqueries / LATERAL / invented
  names fail the compile. A Doris scalar missing from that list (e.g.
  `GREATEST` / `LEAST`) may be admitted **for this task only** after
  `EXPLAIN SELECT` succeeds — do not invent functions; `SLEEP` / `RAND` and
  non-core aggregates (`GROUP_CONCAT`) are never admitted.
- **`is_measure=true` declares a business measure** (SUM/COUNT/AVG of a
  quantity) — those become the DWS `measures` list downstream. Descriptive
  attributes (`MAX(company_name)`, `MAX(status)`) keep `is_measure=false`;
  do NOT flag every aggregation as a measure.
- **Grain columns lead**: `is_key=true` columns MUST be the first columns, in
  the same order as the grain — the compiler enforces the UNIQUE KEY prefix.
- **`summary_table` encodes the grain**: `dws_{topic}_{entities}_{period}`
  (card×location → `dws_stock_inventory_card_loc_monthly`, org+month →
  `dws_stock_inventory_org_monthly`). Distinct grains MUST NOT share a name.
  Stay ≤ 64 characters; drop topic fillers (`info`/`detail`/`snapshot`)
  first. The compiler rewrites a generic `dws_{topic}_{period}` that is
  missing entity tokens. **Do not reuse a table name that already exists in
  the DWS landing database** — the prompt lists them; on collision the
  compiler suffixes `_v2` / `_v3` rather than `CREATE TABLE IF NOT EXISTS`
  onto a leftover.
- **Multi-source designs MUST declare `joins`** — `{"right": "<source table>",
  "on": "<condition>"}`. `sources` and `joins.right` are exact table names
  WITHOUT aliases (`dwd_x`, not `dwd_x a`); the compiler assigns `a`, `b`,
  `c`… in `sources` order and every column reference in expressions/filters
  uses those aliases. If you used other letters (`d`, `t`) the compiler
  rewrites them onto `a`/`b`/`c` — still write `b` for the second source.
- **Never write SQL text** in compiler mode — no `ddl`, no `load_sql` keys.
- All other DWS rules (grain/measure semantics, requirement_coverage,
  warehouse layered sourcing, type-aware empty-string guards) apply unchanged;
  the compiler injects the dialect (UNIQUE KEY, DISTRIBUTED BY, replication)
  itself.

## Warehouse Mode (source_mode="warehouse")

- **No DWD design happens** — the upstream is the set of EXISTING warehouse tables surfaced by exploration (dwd/dwm/dws layers). Anchor every DWS on those tables; never invent sources.
- **Layered-sourcing rule (mandatory)**: DWS load SQL must read DWD/DIM-layer tables only — NEVER other DWS summaries (aggregating aggregates compounds errors and created stale DEPENDENT chains, task 432c17b1). The DWD layer holds the detail data (e.g. `dwd_order_payment_cycle_acc`, `dwd_payment_event`); existing dws-layer tables are application views, not DWS sources.
- **Empty DATE columns**: a column marked `null_rate=100% — EMPTY` was copied faithfully from ODS by core modeling (task bb58438f). Do not use it as grain, in `DATE_FORMAT`/`DATE`/`YEAR`/`MONTH`, or in `IS NOT NULL`. Use a populated date column on the same table (`create_time` / `update_time`) listed with a lower `null_rate`.
- **Goal source allowlist (mandatory)**: when the modeling goal names a closed set (`源表仅限 dwd_x + dim_y` / `只使用 dwd_x，JOIN dim_y`), `sources` and `joins.right` MUST be a subset of those explored tables. Do not JOIN a sibling DIM (task 94a11f97: `dim_category` when the goal named only `dwd_stock_inventory_info` + `dim_sku`). The compiler refuses extra tables and will not remap them to an allowed name.
- **Multi-fact combination is allowed**: one DWS may combine several fact tables via `source_tables` (e.g. 成交 DWD + 退款 DWD → 净成交).
- **DWS materializes base aggregations only** (SUM/COUNT, atomic metrics). Derived metrics (ratios, differences) belong to the ADS layer with `metric_type="derived"` and `parent_metrics` chains.
- Grain is declared from the existing tables' **populated** date columns
  (detect the time column from Columns + `null_rate`; do not assume
  `_event_time` and do not use a column marked `null_rate=100% — EMPTY` as
  grain — core modeling copies empty ODS dates faithfully, task bb58438f).
- **Column binding (mandatory)**: every `SELECT` / `JOIN` / `WHERE` / `GROUP BY` column in `load_sql` MUST be `alias.column` where `column` appears in the explored `Columns` list for that source table. Never invent column names. Harness rejects absent columns before approval. Core-model DIM tables store 编号/名称 as `dim_code`/`dim_name` — use those physical names, never guess `card_code`/`card_name` (task 449dc3ca).
- **Multi-source aliases (mandatory)**: whenever `load_sql` reads from MORE THAN ONE source table, EVERY source MUST carry an explicit alias — `FROM schema.table_a a JOIN schema.table_b b ON ...` — and every column reference must use that alias. A multi-source SQL with unaliased tables fails `warehouse_source_alias_required` (task 81df9f70: the DWS LLM joined dwd tables without aliases and the revision loop could not converge).
- **Derive, don't invent (mandatory)**: when the quantity you need has no physical column, COMPUTE it in the SELECT list from existing columns — e.g. 未收订单金额 = `monthly_receivable_fee - monthly_cumulative_paid_fee AS monthly_unpaid_order_amount`, 支付率 = `paid / receivable`. Never invent a column name, and never silently substitute a semantically different column.
- **Source-data guards (mandatory, type-aware)**: VARCHAR-stored dates/numbers often carry `''` instead of NULL, so `QUARTER('')` / `YEAR('')` fail with `parse number fail` (task 55f47744) — guard those STRING columns with `NULLIF(col, '')` or `WHERE col != ''`. Do **not** filter a VARCHAR **grain** with `!= ''` (task e3914a7e); bucket NULL/'' as `COALESCE(NULLIF(col, ''), '未知')`. The compiler rewrites that grain filter. Native BIGINT/INT/DECIMAL/DATE columns must NOT be compared to `''` (INSERT-strict `can't cast to decimal`, task 0af225c9); use `IS NOT NULL` or pass through.
- **One-to-many keys (mandatory)**: a TEXT key may hold comma-joined multi-values (`store_id`: 'id1,id2,...' — task 55f47744). Never `CAST` such a key directly, and NEVER use `LATERAL VIEW` / `UNNEST` / `EXPLODE_SPLIT` (this Doris cluster rejects them — `mismatched input 'LATERAL'`, task 13d02e94): aggregate at the key column's own grain or compare as strings. Multi-value / empty-string keys crash every DWS load that CASTs them.
- **Explicit INSERT column list (mandatory)**: the DWS `load_sql` must list its target columns after the table name — `INSERT INTO {table} (col1, ...) SELECT ...` — so the harness can verify every declared field/measure is materialized.
- **Consistency Contract (mandatory)**: declared `fields` ⊆ DDL columns ⊆ INSERT target columns; every requested grain (店铺/商品/单位/月/季度) materializes in ≥1 DWS table — never declare a grain unsupported when an explored source has its key column; never shadow an existing explored DWS table with a same-name new table or `INSERT INTO t SELECT ... FROM t` self-reference.
- If nothing composable exists, raise `insufficient_warehouse` (see grill-expert) — do not silently produce empty designs.
- New DWS tables land in `layers["dws"]`, new ADS tables in `layers["ads"]` (connection is the global Doris config; the database comes from the layer mapping).
- [ ] Warehouse `load_sql` only references explored `schema.table` + explored column names via aliases
