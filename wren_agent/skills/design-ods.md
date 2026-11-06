---
name: design-ods
description: Generate ODS (Operational Data Store) layer DDL and sync configurations. LLM acts as the primary designer — not just reviewer — producing complete ODS table designs from source schema explorations.
---

# Design ODS Layer — Generative Guide

You are designing the **ODS (Operational Data Store) layer** for a data warehouse modeling task.
Your job is to produce complete ODS table designs including DDL, sync configurations,
and incremental strategy recommendations.

## Layer Principles (from CONTEXT.md)

> ODS (原始数据层): 保持数据原貌，仅做物理存储，不做或仅做少量清洗。
> 禁止跨层调用，所有下游必须基于 ODS 向上构建。

1. **Preserve as-is**: All source columns must be included without filtering.
2. **Add metadata columns**: Every ODS table MUST include these three columns with DEFAULT so DataX Stream Load can omit them (source tables do not have `_ods_*` columns):
   - `_ods_load_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '数据加载时间'`
   - `_ods_source_db VARCHAR(64) NOT NULL DEFAULT '' COMMENT '来源数据库标识'`
   - `_ods_sync_type VARCHAR(32) NOT NULL DEFAULT '' COMMENT '同步方式(全量/增量)'`
3. **One ODS table per source table**: `ods_{source_table_name}`
4. **No cross-layer dependencies**: ODS reads only from source, never from other DW layers.

## Doris Type Mapping

Map source DB columns to Doris types using this reference table.
When a source type is not in the table, fall back to `VARCHAR(65533)` with a warning.

### MySQL → Doris
| Source Type | Doris Type | Notes |
|---|---|---|
| TINYINT, SMALLINT, MEDIUMINT | TINYINT, SMALLINT, INT | |
| INT, INTEGER | INT | |
| BIGINT | BIGINT | |
| FLOAT, DOUBLE, DOUBLE PRECISION | FLOAT, DOUBLE, DOUBLE | |
| DECIMAL, NUMERIC | DECIMAL | Preserve precision: DECIMAL(p,s) |
| VARCHAR, CHAR | VARCHAR, CHAR | Preserve length |
| TEXT, TINYTEXT, MEDIUMTEXT, LONGTEXT | STRING | For non-key columns |
| BLOB, ENUM, SET | STRING | |
| DATE | DATE | |
| DATETIME, TIMESTAMP | DATETIME | |
| TIME | STRING | |
| JSON | JSON | |
| GEOMETRY, POINT, GEOMETRYCOLLECTION | UNSUPPORTED | Flag for human review |

### PostgreSQL → Doris
| Source Type | Doris Type |
|---|---|
| INTEGER, INT, BIGINT, SMALLINT | INT, INT, BIGINT, SMALLINT |
| REAL, DOUBLE PRECISION | FLOAT, DOUBLE |
| NUMERIC, DECIMAL | DECIMAL |
| VARCHAR, CHARACTER VARYING, CHAR, CHARACTER | VARCHAR, VARCHAR, CHAR, CHAR |
| TEXT, BYTEA | STRING |
| DATE | DATE |
| TIMESTAMP, TIMESTAMPTZ | DATETIME |
| BOOLEAN, BOOL | BOOLEAN |
| JSON, JSONB | JSON |
| UUID | VARCHAR(36) |
| GEOMETRY | UNSUPPORTED |

### Oracle → Doris
| Source Type | Doris Type |
|---|---|
| NUMBER, INTEGER | DECIMAL, BIGINT |
| VARCHAR2, VARCHAR, CHAR, NVARCHAR2 | VARCHAR, VARCHAR, CHAR, VARCHAR |
| CLOB, NCLOB, LONG, BLOB, RAW, XMLTYPE | STRING |
| DATE, TIMESTAMP, TIMESTAMP WITH TIME ZONE | DATETIME |
| FLOAT, BINARY_FLOAT, BINARY_DOUBLE | DOUBLE, FLOAT, DOUBLE |
| SDO_GEOMETRY | UNSUPPORTED |

### DM (Dameng) → Doris
| Source Type | Doris Type |
|---|---|
| INT, INTEGER, BIGINT, SMALLINT, TINYINT | INT, INT, BIGINT, SMALLINT, TINYINT |
| NUMERIC, NUMBER, DECIMAL, DEC | DECIMAL |
| FLOAT, REAL, DOUBLE, DOUBLE PRECISION | DOUBLE, FLOAT, DOUBLE, DOUBLE |
| VARCHAR, VARCHAR2, CHAR, CHARACTER | VARCHAR, VARCHAR, CHAR, CHAR |
| TEXT, CLOB, LONGVARCHAR, BLOB, IMAGE, BINARY, VARBINARY | STRING |
| DATE, TIME, TIMESTAMP, DATETIME | DATETIME, STRING, DATETIME, DATETIME |
| BIT, BOOLEAN | BOOLEAN |
| INTERVAL | UNSUPPORTED |

### Kingbase → Doris
| Source Type | Doris Type |
|---|---|
| INTEGER, INT, BIGINT, SMALLINT | INT, INT, BIGINT, SMALLINT |
| REAL, DOUBLE PRECISION | FLOAT, DOUBLE |
| NUMERIC, DECIMAL | DECIMAL |
| VARCHAR, CHARACTER VARYING, CHAR, CHARACTER | VARCHAR, VARCHAR, CHAR, CHAR |
| TEXT, BYTEA | STRING |
| DATE | DATE |
| TIMESTAMP, TIMESTAMPTZ | DATETIME |
| BOOLEAN | BOOLEAN |
| JSON, JSONB | JSON |
| UUID | VARCHAR(36) |
| GEOMETRY | UNSUPPORTED |

## Doris DDL Constraints (CRITICAL)

!!! VIOLATING THESE WILL CAUSE SANDBOX VALIDATION FAILURE !!!

1. **STRING type MUST NOT appear in any KEY column** (DUPLICATE KEY, PRIMARY KEY, AGGREGATE KEY, UNIQUE KEY).
   - If the first column (which becomes the DUPLICATE KEY) would map to STRING, use `VARCHAR(65533)` instead.
   - Rule: the DUPLICATE KEY column's type must be one of: TINYINT, SMALLINT, INT, BIGINT, LARGEINT, FLOAT, DOUBLE, DECIMAL, DATE, DATETIME, CHAR, VARCHAR.

2. **DUPLICATE KEY columns must be the first N columns** in the column list.
   - Always use the source table's first column (typically `id`) as the DUPLICATE KEY.
   - The key column ordering in DUPLICATE KEY(...) must match the column definition order.

3. **Do NOT use BOOLEAN in DDL** — use `TINYINT` instead. `DEFAULT TRUE` → `DEFAULT 1`.

4. **For TEXT/BLOB columns used in KEY position**: downgrade to `VARCHAR(65533)`.

5. **Unsupported types** (GEOMETRY, etc.): mark the field with `doris_type: "UNSUPPORTED"` and add a warning.

## Chinese Comments (MANDATORY)

Every ODS table and every column MUST carry a **Chinese** COMMENT — a hard requirement, not a suggestion:

1. **Table-level**: `COMMENT 'ODS 原始数据表: {source_table}'` (or another Chinese description).
2. **Column-level**: every column gets `COMMENT '中文'`, including the three metadata columns above.
   - If the source column already has a Chinese comment, use it verbatim.
   - If the source comment is empty or English, derive a Chinese comment from the column name (e.g. `amount` → `金额`, `status` → `状态`, `create_time` → `创建时间`). Never leave a column without a COMMENT clause.
3. **Escape single quotes** inside comment values as `''` (Doris SQL standard).
4. Placement: column comments go inline after the column type; the table comment goes after `DUPLICATE KEY(...)` and before `PROPERTIES(...)`.

## Sync Method Selection

For each source table, choose the sync method:

- **Flink CDC**: when modeling goal contains 实时/准实时/realtime/streaming/秒级/分钟级 keywords
  AND the table has > 100,000 rows (fact table candidate)
- **DataX**: default for all other cases (batch T+1 sync)

## Incremental Strategy

For each source table, recommend one of:
- **full**: No timestamp or auto-increment column detected
- **incremental**: Has `updated_at` / `modified_time` / `update_time` / `modify_time` / `gmt_modified` column
- **incremental_with_full_reconcile**: Same as incremental + recommend weekly full sync
- **Frequency**: "daily" (default) or "hourly" (for realtime sources)

## Output Format

Return a single JSON object with this schema:

```json
{
  "layer": "ODS",
  "designs": [
    {
      "source_table": "string — original table name",
      "source_db_id": "string — source database identifier",
      "ods_table": "string — ods_{source_table}",
      "fields": [
        {
          "name": "string — column name",
          "source_type": "string — original DB type",
          "doris_type": "string — mapped Doris type (VARCHAR(65533) for STRING in key pos)",
          "nullable": true,
          "comment": "string — 中文注释; 源注释为空时由列名生成中文",
          "is_sensitive": false
        }
      ],
      "ddl": "string — complete CREATE TABLE IF NOT EXISTS statement",
      "sync_method": "datax | flink_cdc",
      "incremental_strategy": "full | incremental | incremental_with_full_reconcile",
      "incremental_column": "string | null — the increment column name",
      "schedule_frequency": "daily | hourly"
    }
  ],
  "warnings": ["string — any issues the human should know about"]
}
```

## Design Checklist

Before outputting, verify:
- [ ] Every source column is included in the ODS fields
- [ ] Three metadata columns (_ods_load_time, _ods_source_db, _ods_sync_type) are appended with NOT NULL DEFAULT (CURRENT_TIMESTAMP / '')
- [ ] DUPLICATE KEY column is NOT STRING type (downgraded to VARCHAR(65533) if necessary)
- [ ] DDL syntax is valid Doris SQL
- [ ] Every column and the table carry a **Chinese** COMMENT (表级+字段级)
- [ ] Sync method matches the modeling goal requirements
- [ ] Incremental strategy has a clear reasoning
- [ ] Unsupported types are flagged with warnings
