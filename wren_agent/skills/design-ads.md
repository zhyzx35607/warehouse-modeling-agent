---
name: design-ads
description: Generate ADS (Application Data Service) layer DDL. LLM acts as primary designer producing application-facing metric tables with full lineage traceability to atomic metrics.
---

# Design ADS Layer — Generative Guide

You are designing the **ADS (Application Data Service) layer** for a data warehouse.
Produce application-facing metric tables where every derived metric traces back to atomic metrics.

## Layer Principles (from CONTEXT.md)

> ADS (应用数据层): 面向具体业务产品、BI 报表或数据挖掘的个性化指标。
> 支持直接查询与高并发读取。

1. **Application-specific**: Each ADS table serves a specific business product, BI dashboard, or analytical use case.
2. **Derived from DWS**: All ADS metrics aggregate from DWS summary tables.
3. **Lineage required**: Every metric must trace back to atomic measures in DWD fact tables.

## Metric Design Rules

### Atomic Metrics
An atomic metric is the minimum computable unit:
- Defined as: `AGGFUNC(column) FROM {dws_table} WHERE {conditions}`
- Named with standard convention: `{业务动作}_{度量}_{口径后缀}`
  - Example: `订单金额_剔除取消`, `用户数_去重`

### Derived Metrics
A derived metric is computed from atomic metrics:
- Formula: combination of atomic metrics (e.g., `客单价 = 订单金额 / 用户数`)
- Every derived metric MUST reference atomic metrics by MetricRegistry ID
- Derived metric naming: `{业务概念}_{计算方式}`

### Conflict Detection
When defining a new metric:
1. Check MetricRegistry for metrics with similar names (fuzzy match)
2. If found, compare definitions (SQL expression, source table, conditions)
3. If definitions differ → generate a comparison report and ask human to arbitrate
4. If definitions match → reuse existing metric (do not duplicate)
5. NEVER auto-resolve metric conflicts

## Generating ADS from DWS

### Application Grain and Wide-Table Shape

ADS is query-oriented. A table must contain the physical dimensions needed by
its target dashboard or API together with the metric columns it serves.

- Materialize date/period and entity dimensions such as `stat_date`,
  `stat_year`, `building_id`, `unit_id`, `org_id`, plus useful display
  attributes such as names when they are functionally dependent on the grain.
  Bind the period column to a **populated** upstream date (never a column
  marked `null_rate=100% — EMPTY` — core modeling copies empty ODS dates
  as-is, task bb58438f).
- Keep one coherent grain per table. Daily occupancy and yearly revenue income
  belong in separate ADS tables unless the target grain is explicitly defined
  to support both without ambiguity.
- For a derived metric, set `metric_type="derived"` and list every base metric
  in `parent_metrics`. Those parents must be **physical measure names** of the
  declared DWS sources (the exact `measures[].name` / `fields[].name` values,
  e.g. `monthly_cumulative_paid_amount` — never Chinese business labels like
  `累计实收金额`); the harness matches them against the upstream DWS measure
  names, and a Chinese label fails the parent chain (task f3511018). MetricRegistry
  IDs are also accepted (`registry:<id>`).
- When auditability or drill-down is part of the use case, materialize the
  atomic numerator/denominator beside the derived metric. For example, an
  occupancy table should normally expose `leased_area`, `rentable_area`, and
  `occupancy_rate`.
- Never silently remove a requested grain or metric. Record it in
  `requirement_coverage` as `partial` or `unsupported` with a gap reason.

### Upstream Measure Quality Check (CRITICAL — validate before using)

Before extracting ADS metric candidates, validate that upstream DWS measures are
**genuine business metrics**, not aggregations of ID/key/status columns.

**Red flags — reject DWS measures that are aggregations of:**
- ID columns: `org_id`, `dept_id`, `user_id`, `parent_id`, `group_id`, `tenant_id`, `debt_report_id`, `debt_subject_id`, etc.
- Status/flags: `status`, `is_deleted`, `is_approved`, `is_main_approved`, `construction_status`, etc.
- Version/technical columns: `ts_version`, `version_number`, `create_user`, `update_user`, etc.
- Generic BIGINT fields: `def_long1`, `def_long2`, `def_long3` (custom extension fields with no semantics)

**Accept only when DWS measure is a genuine business quantity:**
- Monetary amounts: `loan_amount`, `repayment_amount`, `total_limit`, `balance`
- Counts: `repayment_count`, `loan_times`, `bond_count`
- Rates/ratios: `interest_rate`, `approval_rate`

**The sniff test:** If a DWS measure name looks like it came from an ID column
(e.g., `daily_org_id`, `daily_parent_id`, `daily_status`), it is NOT a real
metric and MUST NOT be used for ADS derivation.

### Measure Selection

For each DWS summary table in the design plan:
1. **First validate**: exclude measures that are ID/status/version aggregations
2. Evaluate every remaining genuine measure against the requested application;
   include all required atomic and derived metric dependencies at the table's grain
3. Apply business naming conventions based on the modeling goal
4. Register each metric (check for conflicts)

### Handling No Metrics Available

If the upstream DWS layer produced no measures, OR all DWS measures are
ID/status aggregations (see quality check above):
- **DO NOT generate a placeholder ADS table** (empty or zero-column DDL would fail sandbox)
- **DO set `skip: true`** in the output
- **DO explain why** in the warnings field
- **DO list the rejected measures and why each was rejected** (helps diagnose upstream design issues)
- Common causes: no numeric columns in source tables, all BIGINT columns are IDs, DWD/DWS misidentified key columns as measures

### DDL Template

When metrics ARE available:

```sql
CREATE TABLE IF NOT EXISTS {ads_table} (
  {grain_columns DATE/BIGINT/VARCHAR COMMENT '中文维度名'},
  {descriptive_columns VARCHAR COMMENT '中文属性名'},
  {atomic_metric_columns DECIMAL/BIGINT COMMENT '中文原子指标名'},
  {derived_metric_columns DECIMAL COMMENT '中文派生指标名'}
)
[DUPLICATE KEY({grain_columns})]
COMMENT '应用层指标表: {application_name}';
```

Use a key model only when the materialization strategy needs it; if present,
the key must equal the physical grain. Every dimension, attribute, metric, and
the table itself MUST carry a **Chinese** COMMENT.

**Doris DDL clause order (CRITICAL)**: columns → KEY clause (`DUPLICATE KEY(...)`
/ `UNIQUE KEY(...)`, only when a key is used) → `COMMENT` → `DISTRIBUTED BY` →
`PROPERTIES`. **The `COMMENT` must NEVER precede the KEY clause** — that is a
parse error (`mismatched input 'DUPLICATE'`, task 26ec85e0), and this cluster
does NOT support `LATERAL VIEW` / `UNNEST` / `EXPLODE_SPLIT` in any SQL
(`mismatched input 'LATERAL'`, task 13d02e94) — never write row-explosion SQL.

### Materialization SQL (MANDATORY)

Every ADS design must declare its physical query `dimensions` and provide
`load_sql` in addition to `ddl`. The load SQL must read only from the declared
`source_dws_tables`, populate every dimension and metric field, and alias each
metric to its physical `field_name`. In warehouse mode, cross-database DWS
sources must be fully qualified. A DDL-only ADS response is invalid.

**Explicit target column list (MANDATORY)**: write the INSERT with the full
target column list — `INSERT INTO {table} (col1, col2, ...) SELECT ...` — so
the harness can verify every declared dimension/metric field is materialized.
Never rely on positional column order.

**Doris DML dialect (CRITICAL)**: load SQL executes verbatim on Doris without
a pre-deploy EXPLAIN — use only Doris-valid functions: `TO_DATE(expr)` is
single-argument (use `DATE_FORMAT(x, '%Y-%m')` / `SUBSTRING(x, 1, 7)` for
month extraction), date patterns are `%Y-%m` style (never Spark `yyyy-MM`),
and `CONCAT` / `COALESCE` / `IF` are safe. **Never use `||` for string
concat** — Doris treats `||` as boolean OR, so `SUBSTRING(...) || '-Q'`
fails with `'-Q' can't cast to bool in strict mode` (task 60a570e6).
Prefer functions seen in the Source Schema samples.

**Consistency Contract (MANDATORY)**: the harness cross-checks the design
JSON, the DDL, and the load SQL. Every `dimensions` / `fields` / `metrics`
name must appear in the CREATE TABLE body, and every DDL column must be
listed in the INSERT column list. Never declare a field you do not
materialize, and never INSERT a column the DDL does not define.

### Doris Constraints (CRITICAL)
1. ADS may use a DUPLICATE/UNIQUE KEY when needed for idempotent materialization;
   if a key is used, it must equal the declared physical grain and be an
   ordered prefix of the schema.
2. Metric columns use an appropriate numeric type: DECIMAL for financial/rate
   values and BIGINT for explicit counts.
3. Physical dimensions are allowed and required. Use DATE/INT/BIGINT/VARCHAR
   according to the upstream type; do not cast identifiers to DECIMAL.
4. **Chinese comments are mandatory** on every dimension, attribute, metric,
   and the table itself; escape single quotes as `''`.

## Output Format

Return a single JSON object:

```json
{
  "layer": "ADS",
  "skip": false,
  "designs": [
    {
      "application_name": "string — e.g., Modeling Result",
      "ads_table": "string — e.g., ads_modeling_result",
      "description": "string",
      "source_dws_tables": ["string"],
      "dimensions": ["string — physical query grain columns, e.g. org_id, stat_date"],
      "fields": [{"name": "string", "type": "DECIMAL(20,2)", "comment": "string — 中文注释，必填"}],
      "metrics": [
        {
          "name": "string — business metric name",
          "field_name": "string — physical column in fields/DDL",
          "expression": "string",
          "metric_type": "atomic | derived",
          "parent_metrics": ["string — required for derived metrics"],
          "metric_registry_id": null,
          "comment": "string — 中文注释，必填"
        }
      ],
      "target_audience": "string — e.g., Data analysts, BI dashboards",
      "requirement_coverage": [
        {
          "requirement": "string — one requested grain, metric, or formula dependency",
          "status": "supported | partial | unsupported",
          "evidence": ["string — physical dimension/field/metric names"],
          "gap_reason": "string — required when status is not supported"
        }
      ],
      "ddl": "string — complete CREATE TABLE statement",
      "load_sql": "string — complete INSERT INTO ... (explicit target column list) SELECT statement from source_dws_tables"
    }
  ],
  "warnings": ["string"]
}
```

## Design Checklist

- [ ] Upstream DWS measures validated — ID/key/status aggregations rejected
- [ ] Only genuine business quantities (amounts, counts, rates) used for ADS derivation
- [ ] Every accepted DWS measure evaluated for ADS inclusion
- [ ] Metric names follow business naming conventions
- [ ] MetricRegistry checked for conflicts before defining new metrics
- [ ] All metrics have clear lineage to DWS → DWD source
- [ ] If ADS DDL uses a KEY model, key columns exactly match the physical grain and lead the schema
- [ ] Every metric column and the table carry a **Chinese** COMMENT (表级+字段级)
- [ ] Physical query dimensions are declared and materialized
- [ ] Each table has one coherent time/entity grain; incompatible grains are split; names stay distinct (`ads_{topic}_{entities}_{purpose}`, ≤64 chars)
- [ ] Every derived metric declares `metric_type=derived` and non-empty `parent_metrics`
- [ ] `parent_metrics` uses the upstream DWS **physical measure names** (English column names), never Chinese business labels
- [ ] Derived metric parents exist in declared DWS sources or MetricRegistry
- [ ] Numerator/denominator fields are materialized when the application needs audit/drill-down
- [ ] Every requested item appears once in `requirement_coverage`; unsupported items are explicit
- [ ] Every ADS table has executable `load_sql`, not only DDL
- [ ] `load_sql` declares the explicit INSERT target column list
- [ ] Consistency: declared dimensions/fields/metrics ⊆ DDL columns ⊆ INSERT target columns
- [ ] `fields`/`metrics` 的 JSON 条目都带 `comment` 中文注释（下游依赖其理解语义）
- [ ] If no metrics available: skip with clear explanation of WHY each DWS measure was rejected, never generate empty DDL
- [ ] Target audience specified for each ADS table

## Compiler Mode (LLM_LLM_COMPILER=true)

When the DesignCompiler is enabled (ADR-0015), ADS uses the **semantic output
contract**: you provide the STRUCTURE, the compiler renders the Doris DDL and
load SQL. Do NOT write `ddl` / `load_sql` text in this mode.

```json
{
  "semantic_designs": [
    {
      "layer": "ADS",
      "summary_name": "string — application name",
      "summary_table": "string — ads_{topic}_{entities}_{purpose} (质量→quality)",
      "target_audience": "string",
      "columns": [
        {"name": "stat_date", "dtype": "DATE", "comment": "统计日期", "is_key": true, "source_expr": "stat_date"},
        {"name": "org_id", "dtype": "BIGINT", "comment": "单位ID", "is_key": true, "source_expr": "org_id"},
        {"name": "org_name", "dtype": "VARCHAR(255)", "comment": "单位名称", "source_expr": "MAX(org_name)"},
        {"name": "daily_amt", "dtype": "DECIMAL(20,2)", "comment": "日均流水金额", "is_measure": true, "source_expr": "daily_amt"},
        {"name": "daily_flow_count", "dtype": "BIGINT", "comment": "日均交易笔数", "is_measure": true, "source_expr": "daily_flow_count"},
        {"name": "avg_ticket", "dtype": "DECIMAL(20,2)", "comment": "日均客单价", "is_measure": true,
         "source_expr": "daily_amt / daily_flow_count", "metric_type": "derived",
         "parent_metrics": ["daily_amt", "daily_flow_count"]}
      ],
      "sources": ["db_dws.dws_transaction_flow_daily"],
      "filters": [],
      "requirement_coverage": [{"requirement": "string", "status": "supported", "evidence": ["string"]}]
    }
  ],
  "warnings": ["string"]
}
```

Compiler-mode rules:

- `source_expr` is the SELECT expression (plain column, aggregation, or
  arithmetic — the compiler validates a Doris-safe whitelist; window
  functions / subqueries fail the compile). A Doris scalar absent from the
  permanent list may be EXPLAIN-admitted for this task only; do not invent
  function names.
- **`source_expr` MUST use THIS PLAN's DWS physical column names** from the
  "This Plan's DWS Layer" section (and that table's Columns list). The JSON
  example's `stat_date` / `org_name` / `daily_amt` are ILLUSTRATIVE — if the
  DWS table has `stat_month` and no `org_name`, write
  `source_expr: "stat_month"` and omit `org_name`. Copying the example
  identifiers fails `warehouse_source_columns_exist` (task 9f42f542).
- **Grain columns lead**: `is_key=true` columns first, in grain order; the
  compiler renders `UNIQUE KEY(grain)` + `GROUP BY grain` (idempotent
  materialization). A table without key columns renders without a key clause.
- **`summary_table`**: `ads_{topic}_{entities}_{purpose}` for generic names
  (`ads_stock_inventory` + 质量看板 → `ads_stock_inventory_org_quality`).
  Already-specific names (`ads_…_dashboard`) are kept. ≤ 64 characters.
  Do not reuse a name that already exists in the ADS landing database;
  the compiler suffixes `_vN` on collision.
- **Metrics**: business metrics declare `is_measure=true`; derived metrics
  additionally set `metric_type="derived"` and `parent_metrics` = the base
  metric PHYSICAL column names (matching the DWS `measures[].name` — never
  Chinese labels).
- `sources` = the upstream DWS tables (db-qualified in warehouse mode),
  exact names WITHOUT aliases — the compiler assigns `a`, `b`, `c`….
- All other ADS rules (requirement_coverage, derived-vs-atomic, skip when no
  metrics) apply unchanged.

## Principle

"Every derived metric must trace back to atomic metrics. Every atomic metric must have a single, globally unique definition."

## Warehouse Mode (source_mode="warehouse")

- ADS stays **chained to the task's new DWS designs** — it does not bypass the summary layer. If DWS produced nothing (partial support), ADS is skipped and the gaps are recorded in the report.
- Derived metrics are materialized here: `metric_type="derived"`, `definition_sql` records the expression, `parent_metrics` links the base DWS metrics. Reuse `register_metric` (existing path) and the metric_registry conflict check.
- **Column binding (mandatory)**: `load_sql` may only read explored/planned DWS columns via `alias.column`, and the column MUST be listed in that source table's Columns. Never invent field names or guess by role — e.g. do NOT write `b.building_id` when the source's Columns list `store_id` (task 81df9f70: ADS referencing `b.building_id` / `r.room_id` failed `warehouse_source_columns_exist` for 6 tables). Use the exact physical column name from the source's Columns, or JOIN the source that actually has the display attribute you need.
- **Multi-source aliases (mandatory)**: whenever `load_sql` reads from more than one source table, EVERY source MUST carry an explicit alias (`FROM schema.table_a a JOIN schema.table_b b ON ...`) and every column reference must use it — unaliased multi-source SQL fails `warehouse_source_alias_required`.
- New ADS tables land in `layers["ads"]`.
- [ ] Warehouse ADS `load_sql` only references explored/planned DWS columns via aliases
