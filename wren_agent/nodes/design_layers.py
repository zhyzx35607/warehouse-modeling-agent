"""Layer design nodes — ODS, DIM, DWD, DWS, ADS.

DDL generation is LLM-first with explicit, opt-in legacy fallback.
Each design_* node:
  1. Builds context (system prompt from skill + user message from AgentState)
  2. Calls LLM to generate structured JSON
  3. Parses JSON → pydantic models
  4. Raises on failure unless legacy programmatic fallback is enabled
"""

from __future__ import annotations

import json as _json
import logging
import re as _re
from datetime import datetime, timezone
from typing import Any

from wren_agent.config import settings
from wren_agent.models.design import (
    ADSDesign,
    DIMDesign,
    DWDDesign,
    DWDesignPlan,
    DWSDesign,
    FlinkCDCConfig,
    DataXConfig,
    ODSDesign,
    SQLArtifact,
)
from wren_agent.models.llm_outputs import LayerPlanOutput, ReviewOutput
from wren_agent.models.semantic import SemanticDesign
from wren_agent.models.task import IncrementalStrategy, TaskPhase
from wren_agent.tools.json_utils import (
    extract_json_from_llm_response as _extract_json_from_llm_response,
)


# ── Measure type classification ──────────────────────────────────
# BIGINT columns in Chinese business databases are almost always
# snowflake IDs (PK, FK) — NOT business quantities. This helper
# implements the rules from the design-dwd-dws.md skill.

# Columns whose names suggest they are identifiers / keys (never measures)
_ID_PATTERNS: list[str] = [
    "_id", "_key", "_no", "_code", "id_",
    "parent_id", "org_id", "dept_id", "tenant_id", "group_id",
    "create_user", "create_by", "update_user", "update_by",
    "debt_report_id", "debt_subject_id", "user_id",
]

# Columns whose names suggest status flags / versions (never measures)
_STATUS_PATTERNS: list[str] = [
    "status", "state", "flag", "is_", "has_",
    "version", "ts_", "deleted", "approved",
    "construction_status", "is_main_approved",
    "project_asset_ownership",
]

# Keywords that suggest a BIGINT/INT column IS a genuine count/quantity
_QUANTITY_KEYWORDS: list[str] = [
    "count", "cnt", "num", "qty", "quantity", "times", "total", "amount", "sum",
    "数量", "次数", "笔数", "个数", "件数", "人数", "天数", "金额", "总额", "余额", "合计",
]

# Types that are always genuine numeric measures
_MEASURE_TYPES: frozenset[str] = frozenset({"DECIMAL", "NUMERIC", "DOUBLE", "FLOAT"})

# Types that are conditional (need name-based filtering)
_CONDITIONAL_TYPES: frozenset[str] = frozenset({"INT", "INTEGER", "BIGINT", "TINYINT", "SMALLINT", "MEDIUMINT"})


def _is_genuine_measure(col_name: str, col_type: str, is_primary_key: bool = False) -> bool:
    """Check if a column should be treated as a genuine business measure.

    Returns False for BIGINT/INT columns that look like IDs, status flags,
    or version numbers — these are NOT business quantities even though
    they happen to be numeric types.
    """
    if is_primary_key:
        return False

    dtype = (col_type or "").upper()
    col_lower = col_name.lower()

    # Always-measure types: DECIMAL, NUMERIC, DOUBLE, FLOAT
    for mt in _MEASURE_TYPES:
        if mt in dtype:
            return True

    # Conditional types: INT, BIGINT, TINYINT, SMALLINT
    for ct in _CONDITIONAL_TYPES:
        if ct in dtype:
            break
    else:
        # Not a numeric type at all
        return False

    # BIGINT/INT/etc — check name patterns
    # 1. Exclude if it looks like an ID / key / code
    for pattern in _ID_PATTERNS:
        if pattern in col_lower:
            return False

    # 2. Exclude if it looks like a status / flag / version
    for pattern in _STATUS_PATTERNS:
        if pattern in col_lower:
            return False

    # 3. Accept only if name contains quantity keywords
    for keyword in _QUANTITY_KEYWORDS:
        if keyword in col_lower:
            return True

    # BIGINT/INT with no quantity keyword → assume snowflake ID
    return False


def _ddl_properties() -> str:
    """Return the Doris PROPERTIES clause with configured replication_num."""
    from wren_agent.config import settings
    return f"PROPERTIES('replication_num' = '{settings.doris.replication_num}')"


def _normalize_ddl_properties(ddl: str) -> str:
    """Normalize PROPERTIES clause in a DDL statement to match configured replication_num.

    This prevents sandbox validation failures when LLM-generated DDLs specify
    a replication_num that exceeds the available BE nodes in the Doris cluster.

    Also ensures every CREATE TABLE has a PROPERTIES clause (adds one if missing),
    and gives UNIQUE KEY tables an explicit HASH distribution — Doris defaults to
    RANDOM distribution, which UNIQUE KEY tables reject ("should not contain
    random distribution desc").
    """
    import re as _re

    from wren_agent.config import settings

    target_replication = settings.doris.replication_num

    # Replace existing PROPERTIES clause — matches PROPERTIES(...) case-insensitively
    ddl = _re.sub(
        r"PROPERTIES\s*\([^)]*\)",
        f"PROPERTIES('replication_num' = '{target_replication}')",
        ddl,
        flags=_re.IGNORECASE,
    )

    # If no PROPERTIES clause exists, add one before the trailing semicolon
    if "PROPERTIES" not in ddl.upper():
        ddl = ddl.rstrip().rstrip(";").rstrip()
        ddl += f"\nPROPERTIES('replication_num' = '{target_replication}');"

    # UNIQUE KEY tables require explicit HASH distribution
    unique_key = _re.search(r"UNIQUE\s+KEY\s*\(([^)]*)\)", ddl, _re.IGNORECASE)
    if unique_key and not _re.search(r"DISTRIBUTED\s+BY", ddl, _re.IGNORECASE):
        first_key = unique_key.group(1).split(",")[0].strip().strip("`")
        ddl = _re.sub(
            r"(UNIQUE\s+KEY\s*\([^)]*\))",
            rf"\1\nDISTRIBUTED BY HASH(`{first_key}`) BUCKETS AUTO",
            ddl,
            count=1,
            flags=_re.IGNORECASE,
        )

    # Doris grammar: the table-level COMMENT clause must precede DISTRIBUTED BY.
    # LLM-generated DDLs following the old skill template put COMMENT after
    # BUCKETS — Doris rejects it ("mismatched input 'COMMENT'"). Reorder so the
    # shipped DDL parses even if the model repeats the old pattern.
    dist = _re.search(
        r"DISTRIBUTED\s+BY\b.*?(?=\bCOMMENT\s+'[^']*'|\bPROPERTIES\s*\(|;|$)",
        ddl,
        _re.IGNORECASE | _re.DOTALL,
    )
    if dist:
        after = ddl[dist.end():]
        cmt = _re.match(r"\s*(COMMENT\s+'[^']*')", after, _re.IGNORECASE | _re.DOTALL)
        if cmt:
            comment = cmt.group(1)
            rest = after[cmt.end():].strip()
            ddl = (
                ddl[:dist.start()] + comment + "\n"
                + ddl[dist.start():dist.end()]
                + (("\n" + rest) if rest else "")
            )

    return ddl
from wren_agent.state import AgentState, is_ods_migration_task

logger = logging.getLogger(__name__)

# ── Chinese comment helpers ─────────────────────────────────────────
# Hard rule (CONTEXT.md): every generated DDL carries Chinese comments —
# table-level COMMENT '中文' AND column-level COMMENT '中文'.
# Source Chinese comments pass through verbatim; empty/non-Chinese ones
# are replaced by a common-term translation; the column name is the last
# resort (a COMMENT clause is still always emitted).

# Common column name → Chinese comment (programmatic translation fallback)
_ZH_COMMENT_DICT: dict[str, str] = {
    "id": "主键ID", "name": "名称", "code": "编码", "type": "类型", "status": "状态",
    "remark": "备注", "comment": "备注", "description": "描述", "desc": "描述",
    "amount": "金额", "amt": "金额", "count": "数量", "cnt": "数量", "num": "数量",
    "qty": "数量", "quantity": "数量", "total": "合计", "balance": "余额",
    "times": "次数", "price": "价格", "rate": "比率", "ratio": "比率", "sum": "合计",
    "create_time": "创建时间", "created_at": "创建时间", "create_date": "创建日期",
    "update_time": "更新时间", "updated_at": "更新时间", "update_date": "更新日期",
    "modify_time": "修改时间", "modified_time": "修改时间", "gmt_modified": "修改时间",
    "delete_flag": "删除标志", "is_deleted": "删除标志", "deleted": "删除标志",
    "is_current": "当前标志", "version": "版本号", "effective_date": "生效日期",
    "expiration_date": "失效日期", "date": "日期", "time": "时间",
    "parent_id": "上级ID", "org_id": "组织ID", "dept_id": "部门ID",
    "tenant_id": "客户ID", "user_id": "用户ID", "group_id": "分组ID",
    "create_user": "创建人", "create_by": "创建人", "update_user": "更新人",
    "update_by": "更新人",
    "city": "城市", "region": "地区", "area": "区域", "province": "省份",
    "country": "国家", "address": "地址", "phone": "电话", "telephone": "电话",
    "tel": "电话", "email": "邮箱", "age": "年龄", "gender": "性别", "sex": "性别",
    "level": "级别", "grade": "等级", "priority": "优先级", "source": "来源",
    "target": "目标", "user": "用户", "account": "账户", "bank": "银行",
    "loan": "贷款", "company": "公司", "dept": "部门", "org": "组织",
    "order": "订单", "order_id": "订单ID", "project": "项目", "product": "产品",
    "category": "分类", "channel": "渠道", "industry": "行业", "currency": "币种",
    "unit": "单位", "title": "标题", "score": "分数", "item": "条目",
    "start_time": "开始时间", "end_time": "结束时间", "finish_time": "完成时间",
    "submit_time": "提交时间", "approve_time": "审批时间", "deleted_at": "删除时间",
    "is_valid": "是否有效", "is_active": "是否启用", "enabled": "是否启用",
    "approve_status": "审批状态", "audit_status": "审核状态", "business_type": "业务类型",
    "trans_date": "交易日期", "trans_time": "交易时间", "trade_no": "交易号",
    "bill_no": "单据号", "serial_no": "流水号", "batch_no": "批次号",
    "due_date": "到期日", "expiry_date": "到期日期", "birth_date": "出生日期",
    "register_date": "注册日期", "last_login": "最后登录时间", "phone_no": "电话号码",
    "id_card": "身份证号", "tax_no": "税号", "customer_id": "客户ID",
    "supplier_id": "供应商ID", "employee_id": "员工ID", "product_id": "商品ID",
    "order_no": "订单号", "contract_no": "合同号", "invoice_no": "发票号",
    "account_no": "账号", "company_name": "公司名称", "user_name": "用户名",
    "customer_name": "客户名称", "product_name": "商品名称", "project_name": "项目名称",
    "department_name": "部门名称", "category_name": "分类名称",
}

# Suffix → Chinese comment for names like `order_status`, `total_amount`
_ZH_COMMENT_SUFFIXES: list[tuple[str, str]] = [
    ("_status", "状态"), ("_name", "名称"), ("_code", "编码"), ("_type", "类型"),
    ("_count", "数量"), ("_num", "数量"), ("_no", "编号"), ("_key", "键"),
    ("_amount", "金额"), ("_amt", "金额"), ("_price", "价格"), ("_rate", "比率"),
    ("_date", "日期"), ("_time", "时间"), ("_id", "ID"),
]


def _zh_comment(col_name: str, source_comment: str = "") -> str:
    """Derive a Chinese comment for a column.

    Order: Chinese source comment → common-term dictionary → suffix match
    → column name itself (a COMMENT clause is still always emitted).
    """
    src = (source_comment or "").strip()
    if src and any("一" <= ch <= "鿿" for ch in src):
        return src
    base = (col_name or "").lower().strip("_")
    if base in _ZH_COMMENT_DICT:
        return _ZH_COMMENT_DICT[base]
    # Dimension references (`dim_xxx`) → 维度:xxx
    if base.startswith("dim_"):
        return f"维度:{base[4:]}"
    for suffix, zh in _ZH_COMMENT_SUFFIXES:
        if base.endswith(suffix):
            return zh
    return col_name or "无"


def _esc_comment(value: str) -> str:
    """Escape single quotes in a COMMENT value for Doris SQL."""
    return (value or "").replace("'", "''")


def _ods_metadata_fields() -> list[dict]:
    """ODS metadata columns. DEFAULT lets DataX omit them on Stream Load."""
    return [
        {
            "name": "_ods_load_time",
            "doris_type": "DATETIME",
            "nullable": False,
            "default": "CURRENT_TIMESTAMP",
            "comment": "数据加载时间",
        },
        {
            "name": "_ods_source_db",
            "doris_type": "VARCHAR(64)",
            "nullable": False,
            "default": "''",
            "comment": "来源数据库标识",
        },
        {
            "name": "_ods_sync_type",
            "doris_type": "VARCHAR(32)",
            "nullable": False,
            "default": "''",
            "comment": "同步方式(全量/增量)",
        },
    ]


def _format_ods_column_ddl(field: dict) -> str:
    piece = f"{field['name']} {field['doris_type']}"
    if not field.get("nullable", True):
        piece += " NOT NULL"
    if field.get("default"):
        piece += f" DEFAULT {field['default']}"
    if field.get("comment"):
        piece += f" COMMENT '{_esc_comment(field.get('comment', ''))}'"
    return piece


def _ensure_ods_metadata_defaults(sql: str) -> str:
    """Inject DEFAULT on _ods_* columns so Stream Load can omit them."""
    specs = (
        ("_ods_load_time", r"DATETIME(?:\s*\([^)]+\))?", "CURRENT_TIMESTAMP"),
        ("_ods_source_db", r"VARCHAR\s*\(\d+\)", "''"),
        ("_ods_sync_type", r"VARCHAR\s*\(\d+\)", "''"),
    )
    for name, type_pat, default in specs:
        pattern = _re.compile(
            rf"({_re.escape(name)}\s+{type_pat}(?:\s+NOT\s+NULL)?)(?!\s+DEFAULT)",
            _re.IGNORECASE,
        )
        sql = pattern.sub(rf"\1 DEFAULT {default}", sql)
    return sql


# ── LLM generation helpers ──────────────────────────────────────────


# Prompt-size caps for design/repair LLM context (task-scoped cache).
# Sized for the deepseek flash 1M-token input window: most relevant tables
# with full column detail fit comfortably, while the relevance ranking (below)
# still keeps facts first. The old 20-table/60k caps starved the LLM of fact
# tables on large warehouses (task 4e372c61).
_MAX_TABLES_IN_PROMPT = 80
_MAX_FIELDS_PER_TABLE = 60
_MAX_SAMPLE_VALUES = 2
_MAX_RELATIONSHIPS_IN_PROMPT = 30
_MAX_SCHEMA_PROMPT_CHARS = 400_000
# At least this many tables per explored layer so dim tables (join context)
# survive even when facts outrank them.
_MIN_TABLES_PER_LAYER = 3

# Chinese goal term → English column/table tokens (domain-agnostic enough to
# rank the order revenue corpus; unknown terms fall back to bigram matching
# against Chinese comments).
_GOAL_KEYWORD_MAP = [
    ("应收", ("receivable", "due", "amount_receivable", "arrear")),
    ("实收", ("paid", "received", "cumulative_paid")),
    ("租金", ("payment",)),
    ("合同", ("contract",)),
    ("收款", ("receipt", "collection", "collect")),
    ("回款", ("recovery", "collect")),
    ("订单", ("order",)),
    ("商品", ("product",)),
    ("店铺", ("shop", "store")),
    ("客户", ("tenant",)),
    ("单位", ("unit", "org")),
    ("月度", ("month",)),
    ("季度", ("quarter",)),
    ("金额", ("amount", "amt", "money")),
    ("欠费", ("overdue", "arrear")),
    ("缴费", ("pay",)),
    ("押金", ("deposit", "margin")),
    ("水电", ("water", "electric", "power")),
    ("资产", ("asset",)),
]


def _goal_keywords(goal: str) -> set[str]:
    """English keyword set + Chinese bigrams derived from the modeling goal."""
    keywords: set[str] = set()
    for term, tokens in _GOAL_KEYWORD_MAP:
        if term in (goal or ""):
            keywords.update(tokens)
    # Chinese character bigrams of the goal — match table/column comments.
    chars = [c for c in (goal or "") if "一" <= c <= "鿿"]
    keywords.update(
        f"字:{chars[i]}{chars[i + 1]}" for i in range(len(chars) - 1)
    )
    return keywords


def _table_relevance_score(table: Any, keywords: set[str]) -> int:
    """Score a table's relevance to the modeling goal.

    Fact/dwd tables rank above dims so the LLM always sees the facts it must
    anchor designs on; name hits beat column hits; Chinese bigrams match
    against comments.
    """
    name = str(getattr(table, "name", "") or "").lower()
    score = 0
    if name.startswith("fct_"):
        score += 6
    elif name.startswith("dwd_"):
        score += 5
    elif name.startswith("dws_"):
        score += 4
    elif name.startswith("dim_"):
        score += 1
    for kw in keywords:
        if kw.startswith("字:"):
            comment = str(getattr(table, "comment", "") or "").lower()
            if kw[2:] in comment:
                score += 2
            continue
        if kw in name:
            score += 4
    if keywords:
        hit = 0
        for f in (getattr(table, "fields", None) or [])[:80]:
            fname = (f.name if hasattr(f, "name") else f.get("name", "")).lower()
            if not fname:
                continue
            for kw in keywords:
                if kw.startswith("字:"):
                    continue
                if kw in fname:
                    hit += 1
                    break
        score += min(hit, 8)
    if getattr(table, "row_count_estimate", None):
        score += 1
    return score


def _select_relevant_tables(
    explorations: dict, keywords: set[str], max_tables: int,
) -> list[tuple[str, Any]]:
    """Order tables by goal relevance with a per-layer floor.

    Returns [(db_id, table)] — relevant facts first, then filler, with at
    least `_MIN_TABLES_PER_LAYER` tables per layer so dim join context is
    never starved.
    """
    def _tables(exp: Any) -> list:
        tables = getattr(exp, "tables", None)
        if tables is None and isinstance(exp, dict):
            tables = exp.get("tables", [])
        return tables or []

    ranked: list[tuple[int, str, Any, str]] = []
    for db_id, exp in explorations.items():
        layer = db_id.rsplit("#", 1)[-1].lower() if "#" in db_id else ""
        for table in _tables(exp):
            ranked.append((_table_relevance_score(table, keywords), db_id, table, layer))

    selected: list[tuple[str, Any]] = []
    seen: set[int] = set()
    for layer in sorted({r[3] for r in ranked}):
        layer_items = sorted(
            (r for r in ranked if r[3] == layer), key=lambda r: -r[0],
        )
        for _, db_id, table, _layer in layer_items[:_MIN_TABLES_PER_LAYER]:
            selected.append((db_id, table))
            seen.add(id(table))
    rest = sorted(
        (r for r in ranked if id(r[2]) not in seen), key=lambda r: -r[0],
    )
    for _, db_id, table, _layer in rest:
        if len(selected) >= max_tables:
            break
        selected.append((db_id, table))
    return selected


def _field_priority(table, field) -> tuple:
    """Rank fields so PKs / commented / sampled columns survive truncation."""
    name = field.name if hasattr(field, "name") else field.get("name", "")
    pks = getattr(table, "primary_keys", None) or []
    is_pk = (
        bool(getattr(field, "is_pk", False))
        or bool(getattr(field, "primary_key", False))
        or name in pks
    )
    comment = (field.comment if hasattr(field, "comment") else field.get("comment", "")) or ""
    samples = getattr(field, "sample_values", None) or []
    return (0 if is_pk else 1, 0 if comment else 1, 0 if samples else 1, name)


def _null_ratio_mark(field) -> str:
    """Surface fill-rate so empty DATE columns are not used as grain."""
    from wren_agent.tools.column_profile import format_null_mark

    ratio = (
        field.get("null_ratio") if isinstance(field, dict)
        else getattr(field, "null_ratio", None)
    )
    return format_null_mark(ratio)


def _serialize_explorations_for_prompt(
    state: AgentState,
    *,
    compact: bool = False,
    use_cache: bool = True,
) -> str:
    """Serialize schema explorations into a compact prompt-friendly format.

    Includes non-sensitive sample values, distinct counts, and FK verification
    status so the LLM sees useful data shapes without receiving sensitive data.
    Results are cached on ``state['_schema_prompt_cache']`` for the task.
    """
    if use_cache and not compact:
        cached = state.get("_schema_prompt_cache") or ""
        if cached:
            return cached

    explorations = state.get("explorations", {})
    if not explorations:
        return "(No schema explorations available)"

    max_tables = 20 if compact else _MAX_TABLES_IN_PROMPT
    max_fields = 30 if compact else _MAX_FIELDS_PER_TABLE
    max_samples = 1 if compact else _MAX_SAMPLE_VALUES

    # Order tables by goal relevance (facts first, per-layer floor), then
    # render until the character budget is exhausted — never let the schema
    # section blow past the model context (task 4e372c61).
    total_tables = 0
    for exp in explorations.values():
        tables = getattr(exp, "tables", None)
        if tables is None and isinstance(exp, dict):
            tables = exp.get("tables", [])
        total_tables += len(tables or [])
    # DETERMINISTICALLY EXCLUDE 0-row tables from the design context — the
    # [EMPTY] mark and hard rules did not stop the LLM from anchoring DWS/ADS
    # on empty-but-well-named production tables (tasks 582633e8/73e47237/
    # 432c17b1: five consecutive "materialized targets are empty" / empty-
    # source failures). A 0-row source can only materialize an empty table.
    filtered_explorations: dict = {}
    excluded_empty = 0
    for key, exp in explorations.items():
        tables = getattr(exp, "tables", None)
        if tables is None and isinstance(exp, dict):
            tables = exp.get("tables", [])
        kept = [
            t for t in (tables or [])
            if getattr(t, "row_count_estimate", None) != 0
        ]
        excluded_empty += len(tables or []) - len(kept)
        if isinstance(exp, dict):
            exp = {**exp, "tables": kept}
        else:
            exp = exp.model_copy(update={"tables": kept})
        filtered_explorations[key] = exp
    if excluded_empty:
        logger.info(
            "[%s] Excluded %d empty (0-row) tables from the design prompt",
            state.get("task_id", "")[:8], excluded_empty,
        )
    explorations = filtered_explorations
    selected = _select_relevant_tables(
        explorations, _goal_keywords(state.get("modeling_goal") or ""), max_tables,
    )

    lines = []
    tables_emitted = 0
    emitted_dbs: set[str] = set()
    for db_id, table in selected:
        if tables_emitted >= max_tables:
            break
        if db_id not in emitted_dbs:
            emitted_dbs.add(db_id)
            lines.append(f"\n### Source DB: {db_id}")
        block_start = len(lines)
        # Show the physical database prefix — without it the LLM qualifies
        # tables with the exploration key (a UUID#layer), which is not a
        # real Doris database (task e1d3969e).
        schema = getattr(table, "schema_name", "") or ""
        qualified = f"{schema}.{table.name}" if schema else table.name
        lines.append(f"\n**Table: {qualified}**")
        # `0` is falsy — show it explicitly, or the LLM cannot tell an EMPTY
        # table from an unknown-count one and keeps anchoring designs on
        # 0-row tables by name alone (tasks 582633e8/73e47237: 4 consecutive
        # "materialized targets are empty" failures).
        if getattr(table, "row_count_estimate", None) is not None:
            est = table.row_count_estimate
            empty_mark = " [EMPTY — do NOT use as a source]" if est == 0 else ""
            lines.append(f"  Row count estimate: {est:,}{empty_mark}")
        if hasattr(table, "primary_keys") and table.primary_keys:
            lines.append(f"  Primary keys: {', '.join(table.primary_keys)}")
        if hasattr(table, "comment") and table.comment:
            lines.append(f"  Comment: {table.comment}")
        lines.append("  Columns:")
        fields = list(table.fields)
        fields_sorted = sorted(fields, key=lambda f: _field_priority(table, f))
        omitted_fields = max(0, len(fields_sorted) - max_fields)
        for f in fields_sorted[:max_fields]:
            name = f.name if hasattr(f, "name") else f.get("name", "?")
            dtype = f.data_type if hasattr(f, "data_type") else f.get("data_type", "?")
            nullable = f.nullable if hasattr(f, "nullable") else f.get("nullable", True)
            comment = (f.comment if hasattr(f, "comment") else f.get("comment", "")) or ""
            pk_mark = " [PK]" if (hasattr(f, "is_pk") and f.is_pk) or (hasattr(table, "primary_keys") and name in (table.primary_keys or [])) else ""

            # Append cardinality info from deep exploration
            cardinality = ""
            distinct = getattr(f, "distinct_count", None)
            if distinct is not None:
                cardinality = f" [cardinality≈{distinct:,}]"

            is_sensitive = bool(getattr(f, "is_sensitive", False))
            if not is_sensitive:
                from wren_agent.tools.db_explorer import detect_sensitive_field
                is_sensitive = detect_sensitive_field(table.name, name) is not None

            # Append sample values from light exploration only after a
            # defense-in-depth sensitivity check. This also protects old
            # checkpoints whose is_sensitive flag was not populated.
            sample_str = ""
            samples = getattr(f, "sample_values", None)
            if samples and not is_sensitive:
                sample_vals = [str(v)[:30] for v in samples if v is not None]
                if sample_vals:
                    sample_str = f" [samples: {', '.join(sample_vals[:max_samples])}]"

            sensitive_mark = " [SENSITIVE]" if is_sensitive else ""
            lines.append(
                f"  - {name}: {dtype}"
                f"{' NULL' if nullable else ' NOT NULL'}"
                f"{pk_mark}{sensitive_mark}{cardinality}{sample_str}"
                f"{_null_ratio_mark(f)}"
                f"{' — ' + comment if comment else ''}"
            )
        if omitted_fields:
            lines.append(f"  … ({omitted_fields} more columns omitted)")

        # Character-budget gate: drop the table that crossed the limit.
        if len("\n".join(lines)) > _MAX_SCHEMA_PROMPT_CHARS:
            del lines[block_start:]
            break
        tables_emitted += 1

    # Include FK relationships with verification status (grouped per DB).
    for db_id in emitted_dbs:
        exp = explorations.get(db_id)
        relationships = getattr(exp, "relationships", None)
        if not relationships:
            continue
        lines.append("  Relationships:")
        for rel in relationships[:_MAX_RELATIONSHIPS_IN_PROMPT]:
            verified_info = ""
            if getattr(rel, "verified", False):
                ratio = getattr(rel, "verified_ratio", None)
                verified_info = f" [✓ verified"
                if ratio is not None:
                    verified_info += f", ratio={ratio:.2f}"
                verified_info += "]"
            else:
                verified_info = " [✗ unverified]"
            lines.append(
                f"    {rel.source_table}.{rel.source_column} ↔ "
                f"{rel.target_table}.{rel.target_column}"
                f"{verified_info}"
            )
        extra_rels = len(relationships) - _MAX_RELATIONSHIPS_IN_PROMPT
        if extra_rels > 0:
            lines.append(f"    … ({extra_rels} more relationships omitted)")

    remaining = total_tables - tables_emitted
    if remaining > 0:
        lines.append(f"\n… ({remaining} more tables omitted for prompt size)")

    text = "\n".join(lines)
    if use_cache and not compact:
        state["_schema_prompt_cache"] = text
    return text


def _serialize_clarifications_for_prompt(state: AgentState) -> str:
    """Serialize answered clarifications for LLM context.

    Marks the credibility of each answer:
      [👤 confirmed] — human expert answered explicitly
      [🤖 auto]      — agent auto-answered with LLM recommendation (lower confidence)
    """
    clarifications = state.get("clarifications", [])
    answered = [
        c for c in clarifications
        if hasattr(c, "status") and str(c.status.value if hasattr(c.status, "value") else c.status) == "answered" and c.answer
    ]
    if not answered:
        return "(No human clarifications available)"

    from wren_agent.state import is_auto_mode

    lines = ["## Business Knowledge (Clarifications)"]
    for i, c in enumerate(answered, 1):
        q = c.question[:200]
        a = c.answer[:300]
        ct = c.clarification_type if hasattr(c, "clarification_type") else "general"

        # Determine credibility: auto-answered vs human-confirmed
        is_auto = is_auto_mode(state)
        recommended = getattr(c, "recommended", None)
        if is_auto and recommended and a == recommended:
            source_tag = "🤖 auto"
        elif is_auto:
            source_tag = "🤖 auto (first-candidate fallback)"
        else:
            source_tag = "👤 confirmed"

        lines.append(f"{i}. [{ct}] [{source_tag}] Q: {q}\n   A: {a}")
    return "\n".join(lines)


def _serialize_upstream_designs(state: AgentState, current_layer: str) -> str:
    """Serialize upstream layer designs already in the plan.

    Includes field-level details so downstream LLMs know exactly what columns,
    keys, measures, and grains are available — not just table names.

    Warehouse mode: there is no in-plan upstream — DWS/ADS anchor on EXISTING
    warehouse tables surfaced by exploration (see wayfinder T-08). For
    non-ADS layers those tables come from the `## Source Schema Explorations`
    section (built by _build_layer_context / the repair prompts), so this
    function contributes nothing extra — a second dump of the same tables
    only doubled the prompt size (task c0f31ae0).
    Exception: the plan's OWN freshly designed DWS tables are not yet in the
    warehouse, so ADS design/revise would never see them and falls back to DWD
    tables — breaking `derived_metric_parent_chain` (task 0de4bbfb: 3 revise
    rounds failed). ADS gets the referenced explored dws tables + the plan DWS
    designs appended explicitly.
    """
    if state.get("source_mode") == "warehouse":
        plan = state.get("design_plan")
        if current_layer == "ADS":
            # ADS reads DWS only (validation forbids DWD/DIM sources) — the
            # full dim/dwd/dws dump is pure prompt bloat that pushed the ADS
            # call past the LLM timeout (tasks a1af4bed/c0f31ae0). Inject ONLY
            # the explored dws tables this plan's DWS designs reference (the
            # real inputs); when the plan left source_tables empty, fall back
            # to dws tables whose names match the modeling goal.
            dws_sources = {
                str(t).split(".")[-1].strip("`").lower()
                for d in (plan.dws_designs or [])
                for t in (getattr(d, "source_tables", None) or [])
            }
            if not dws_sources:
                dws_sources = _goal_matching_explored_dws_names(
                    state, plan, state.get("modeling_goal", "")
                )
            sections = [
                _serialize_existing_warehouse_tables(
                    state, layers={"dws"}, referenced=dws_sources or None,
                )
            ]
        else:
            # DWS/DWD design anchors on the `## Source Schema Explorations`
            # section (built by _build_layer_context / the repair prompts) —
            # this warehouse dump re-lists the SAME tables in a second format,
            # doubling the prompt size for no information gain (task c0f31ae0:
            # ~90K exploration + warehouse dump pushed every design prompt
            # past the 100K trace cap and the LLM timeout).
            sections = []
        if current_layer == "ADS" and plan is not None and plan.dws_designs:
            sections.append(
                "\n### This Plan's DWS Layer (designed earlier in this task — "
                f"{len(plan.dws_designs)} tables, NOT yet materialized in the warehouse)\n"
                # Detail rows are filtered by relevance to the modeling goal —
                # injecting every table's full fields/measures bloated the
                # prompt past the LLM timeout and diluted focus (task
                # a1af4bed: ADS design APITimeoutError). All table NAMES stay
                # visible; only goal-relevant tables get field/measure detail.
                + _serialize_plan_dws_designs(plan, goal=state.get("modeling_goal", ""))
                + "\nADS MUST anchor on these tables: set source_dws_tables to these "
                  "names (bare or with the DWS layer db prefix) and derived metric "
                  "parent_metrics to their measure names."
            )
        return "\n".join(sections)

    plan = state.get("design_plan")
    if plan is None:
        return "(No upstream designs yet)"

    lines = []
    layer_order = ["ODS", "DIM", "DWD", "DWS"]

    for layer in layer_order:
        if layer == current_layer:
            break

        if layer == "ODS" and plan.ods_designs:
            lines.append(f"\n### Upstream: ODS Layer ({len(plan.ods_designs)} tables)")
            lines.append("  Tables: " + ", ".join(o.ods_table for o in plan.ods_designs))
            for ods in plan.ods_designs[:25]:
                field_list = ", ".join(
                    f"{f.get('name','?')}:{f.get('doris_type',f.get('source_type','?'))}"
                    for f in (ods.fields or [])[:15]
                )
                inc_info = f"incremental on {ods.incremental_column}" if ods.incremental_column else "full sync"
                lines.append(f"  - {ods.ods_table} ← {ods.source_table}")
                lines.append(f"    Sync: {ods.sync_method}, {inc_info}, schedule={ods.schedule_frequency}")
                lines.append(f"    Fields ({len(ods.fields or [])}): {field_list}")

        elif layer == "DIM" and plan.dim_designs:
            lines.append(f"\n### Upstream: DIM Layer ({len(plan.dim_designs)} tables)")
            lines.append("  Tables: " + ", ".join(d.dim_table for d in plan.dim_designs))
            for dim in plan.dim_designs[:25]:
                field_names = [f.get("name", "?") for f in (dim.fields or [])[:10]]
                lines.append(f"  - {dim.dim_table} ({dim.dim_name})")
                lines.append(f"    Source: {dim.source}, source_table={dim.source_table}")
                lines.append(f"    Surrogate Key: {dim.surrogate_key}")
                lines.append(f"    Natural Key: {dim.natural_key}")
                if field_names:
                    lines.append(f"    Fields: {', '.join(field_names)}")
                lines.append(f"    SCD Type 2: effective_date, expiration_date, is_current")

        elif layer == "DWD" and plan.dwd_designs:
            lines.append(f"\n### Upstream: DWD Layer ({len(plan.dwd_designs)} tables)")
            lines.append("  Tables: " + ", ".join(d.fact_table for d in plan.dwd_designs))
            for dwd in plan.dwd_designs[:25]:
                measure_list = ", ".join(
                    f"{m.get('name','?')}({m.get('expression','?')})"
                    for m in (dwd.measures or [])[:8]
                )
                dim_refs = dwd.dimension_refs or []
                lines.append(f"  - {dwd.fact_table} ({dwd.fact_name})")
                lines.append(f"    Grain: {dwd.grain}")
                lines.append(f"    Business Process: {dwd.business_process}")
                lines.append(f"    Source ODS: {dwd.source_ods_tables}")
                if measure_list:
                    lines.append(f"    Measures: {measure_list}")
                if dim_refs:
                    lines.append(f"    Dimension Refs: {dim_refs}")

        elif layer == "DWS" and plan.dws_designs:
            lines.append(f"\n### Upstream: DWS Layer ({len(plan.dws_designs)} tables)")
            lines.append(_serialize_plan_dws_designs(plan))

    return "\n".join(lines) if lines else "(No upstream designs)"


_GOAL_RELEVANCE_KEYWORDS = (
    # BUSINESS-entity/subject tokens connecting a DWS table to the modeling
    # goal. Time tokens (month/quarter/月/季度) are deliberately absent —
    # every periodic table matches them, so they filter nothing.
    "unit", "org", "shop", "store", "product", "contract", "receiv",
    "collect", "pay", "payment", "单位", "店铺", "商品", "合同", "应收", "实收",
    "支付", "收款", "订阅",
)


def _dws_relevant_to_goal(goal: str, dws: Any) -> bool:
    """True when the DWS table's grain/name overlaps the modeling goal's
    entities — the detail-injection filter for the ADS prompt."""
    goal_l = (goal or "").lower()
    if not goal_l:
        return True
    table_txt = " ".join([
        str(dws.summary_table or ""),
        *[str(g) for g in (dws.grain or [])],
        *[str(g) for g in (dws.group_by_dimensions or [])],
        str(dws.summary_name or ""),
    ]).lower()
    return any(kw in goal_l and kw in table_txt for kw in _GOAL_RELEVANCE_KEYWORDS)


def _serialize_plan_dws_designs(plan, goal: str = "") -> str:
    """Serialize the plan's DWS designs (table + grain + fields + measures).

    Field/measure Chinese comments let the downstream ADS LLM understand
    semantics instead of guessing column meanings (task cc841bec: invented
    `monthly_unpaid_order_amount`). Shared by the business-DB upstream block
    and the warehouse-mode "This Plan's DWS Layer" injection for ADS.

    ALL table names are always listed first — a truncated name list hides
    tables 11+ from ADS entirely and breaks parent_metrics/lineage for plans
    with many DWS tables (same failure class as task 0de4bbfb). Detail rows
    are filtered to goal-relevant tables (task a1af4bed: full-detail
    injection bloated the ADS prompt past the LLM timeout); names are never
    filtered.
    """
    dws_list = plan.dws_designs or []
    lines = []
    lines.append("  Tables: " + ", ".join(d.summary_table for d in dws_list))
    detailed = [d for d in dws_list if _dws_relevant_to_goal(goal, d)]
    for dws in detailed[:25]:
        field_list = ", ".join(
            f"{f.get('name','?')}"
            + (f"({f.get('comment','')})" if f.get("comment") else "")
            for f in (dws.fields or [])[:20]
        )
        measure_list = ", ".join(
            f"{m.get('name','?')}"
            + (f"({m.get('comment','')})" if isinstance(m, dict) and m.get("comment") else "")
            for m in (dws.measures or [])[:20]
        )
        lines.append(f"  - {dws.summary_table} ({dws.summary_name})")
        lines.append(f"    Grain: {dws.grain}")
        lines.append(f"    Source DWD: {dws.source_dwd_table}")
        if getattr(dws, "source_tables", None):
            lines.append(f"    Source Tables: {dws.source_tables}")
        lines.append(f"    Group By: {dws.group_by_dimensions}")
        if field_list:
            lines.append(f"    Fields: {field_list}")
        if measure_list:
            lines.append(f"    Measures: {measure_list}")
    skipped = len(dws_list) - len(detailed)
    if skipped > 0:
        lines.append(f"  … {skipped} more DWS tables listed above by name only (not goal-relevant)")
    elif len(dws_list) > 25:
        lines.append(f"  … {len(dws_list) - 25} more DWS tables listed above by name only")
    return "\n".join(lines)


def _serialize_existing_warehouse_tables(
    state: AgentState,
    layers: set[str] | None = None,
    referenced: set[str] | None = None,
) -> str:
    """Warehouse mode: serialize EXISTING warehouse tables from exploration.

    Exploration results are keyed "<source_id>#<layer>" (dim/dwd/dwm/dws).
    DWS/ADS design anchors on these tables — grain, measures and dimensions
    are derived from what is actually there (see wayfinder T-06/T-08).

    `layers`: only serialize these layer keys (ADS needs dws only — the full
    dim/dwd dump bloated the prompt past the LLM timeout, task c0f31ae0).
    `referenced`: bare table names; when given, ONLY those tables get the
    field/column detail rows (all names still listed — lineage needs them).
    """
    explorations = state.get("explorations", {})
    if not explorations:
        return "(No existing warehouse tables explored)"

    from collections import OrderedDict
    by_layer: "OrderedDict[str, list]" = OrderedDict()
    for key, exp in explorations.items():
        layer = key.rsplit("#", 1)[-1] if "#" in key else ""
        if layers is not None and layer not in layers:
            continue
        by_layer.setdefault(layer or "?", []).extend(exp.tables)

    if not by_layer:
        return "(No existing warehouse tables explored)"

    lines = []
    for layer, tables in by_layer.items():
        # 0-row tables are excluded — they can only materialize empty results
        # (tasks 582633e8/73e47237/432c17b1; the [EMPTY] mark alone did not
        # stop the LLM anchoring on them).
        tables = [
            t for t in tables
            if getattr(t, "row_count_estimate", None) != 0
        ]
        if not tables:
            continue
        visible = (
            [t for t in tables if str(getattr(t, "name", "")).lower() in referenced]
            if referenced is not None
            else tables
        )
        lines.append(f"\n### Existing warehouse tables — layer {layer} ({len(visible)} tables)")
        # Always list every visible table as schema.table so the LLM cannot
        # invent landing-db prefixes (db_dws.…) that fail validation/deploy.
        name_lines = []
        for table in visible:
            schema = (getattr(table, "schema_name", None) or "").strip()
            qualified = f"{schema}.{table.name}" if schema else table.name
            name_lines.append(qualified)
        lines.append("  Tables: " + ", ".join(name_lines))
        detail_tables = visible[:20]
        for table in detail_tables:
            schema = (getattr(table, "schema_name", None) or "").strip()
            qualified = f"{schema}.{table.name}" if schema else table.name
            # Prefer a complete field-name list — column hallucination is the
            # top cause of Unknown-column deploy failures.
            field_names = [f.name for f in (table.fields or [])]
            # Field-level Chinese comments are the semantic bridge the LLM
            # needs — without them it guesses column meanings and invents
            # names like `monthly_unpaid_order_amount` (task cc841bec). 99%
            # of explored fields carry Doris COMMENTs; surface them.
            field_types = ", ".join(
                f"{f.name}({f.data_type})"
                + (" [PK]" if f.primary_key else "")
                + (f" — {f.comment}" if getattr(f, "comment", None) else "")
                for f in (table.fields or [])[:20]
            )
            est = getattr(table, "row_count_estimate", None)
            est_txt = "?" if est is None else f"{est:,}"
            empty_mark = " [EMPTY]" if est == 0 else ""
            lines.append(f"  - {qualified} ({est_txt} rows){empty_mark}")
            if field_names:
                lines.append(f"    Columns: {', '.join(field_names)}")
            if field_types:
                lines.append(f"    Typed (sample): {field_types}")
            if table.comment:
                lines.append(f"    Comment: {table.comment}")
        if referenced is not None:
            excluded = len(tables) - len(visible)
            if excluded > 0:
                lines.append(
                    f"  … {excluded} more layer tables excluded (not referenced "
                    "by this plan's DWS designs)"
                )
        elif len(tables) > 20:
            lines.append(
                f"  … {len(tables) - 20} more tables listed above without field detail"
            )

    lines.append("\n### Hard rules for DWS/ADS load SQL (enforced by harness)")
    lines.append(_warehouse_sql_hard_rules(state))
    return "\n".join(lines)


def _warehouse_sql_hard_rules(state: AgentState | None = None) -> str:
    """Hard rules every warehouse DWS/ADS design prompt must carry.

    Kept as a standalone block: the rules must reach ALL layer prompts (DWS
    included) — before the upstream dedup (task c0f31ae0) they lived at the
    tail of the warehouse-table dump, which DWS no longer receives, and the
    DWS LLM lost them (task 1206ee16: multi-table JOINs without explicit
    aliases failed `warehouse_source_alias_required`).
    """
    lines = [
        "1. ONLY use schema.table names listed in this prompt — never invent databases or tables.",
        "2. NEVER anchor a DML on a table marked [EMPTY] or showing 0 rows — it "
        "would materialize an empty table. Prefer its DWD/detail counterparts "
        "or any data-bearing table (e.g. dwd_payment_event, "
        "dwd_order_payment_cycle_acc).",
        "2b. DWS load SQL must read from the DWD/DIM layer ONLY — never from "
        "another DWS summary table (aggregating aggregates; task 432c17b1). "
        "ADS reads DWS by design.",
        "2c. NEVER use a column marked `null_rate=100% — EMPTY` as a time grain "
        "or `IS NOT NULL` filter (task bb58438f: inventory_date was copied from "
        "ODS 100% NULL; DATE_FORMAT + IS NOT NULL materialized empty DWS/ADS). "
        "Pick a populated date column on the same table (create_time / "
        "update_time) listed with a lower null_rate.",
        "3. EVERY selected/joined/filtered column MUST be written as alias.column "
        "and the column MUST appear in that table's Columns list.",
        "4. Do NOT invent synonyms, pinyin, or 'obvious' column names that are "
        "absent from Columns. The compiler refuses JOIN/filter identifiers "
        "that are not on that table's explored snapshot and will not remap "
        "them (task 2efe882a: b.dim_id is not sku_id). Harness "
        "`warehouse_source_columns_exist` is the last gate.",
        "4b. Core-model DIM tables rename source `code`/`name` to `dim_code`/"
        "`dim_name` (comments keep 编号/名称). Use those physical names from "
        "Columns — never guess `card_code`/`card_name` (task 449dc3ca).",
        "5. Multi-table SQL MUST give every source an explicit alias "
        "(FROM schema.table t … JOIN schema.other o ON …).",
        "5b. In compiler mode, `sources`/`joins.right` are exact schema.table "
        "names — do NOT append aliases (`dwd_x a`); the compiler assigns "
        "a, b, c in source order.",
        "6. THIS Doris cluster does NOT support LATERAL VIEW, UNNEST, or "
        "EXPLODE_SPLIT — never write row-explosion SQL. If a column holds "
        "comma-separated IDs, aggregate at the column's own grain "
        "(contract/cycle/unit) — do not attempt to split it into rows.",
    ]
    allow = compile_source_allowlist(state, "DWS") if state else None
    if allow:
        names = ", ".join(sorted(allow))
        lines.append(
            "7. The modeling goal RESTRICTS source tables to: "
            f"{names}. DWS sources/joins MUST be a subset of that list — "
            "do not JOIN any other table (task 94a11f97). The compiler "
            "refuses extra tables and will not remap them."
        )
    return "\n".join(lines)


def _goal_matching_explored_dws_names(state: AgentState, plan: Any, goal: str) -> set[str]:
    """Bare names of explored dws-layer tables relevant to the ADS prompt.

    Fallback for the ADS upstream injection when the plan's DWS designs left
    `source_tables` empty: instead of injecting the whole dws layer (prompt
    bloat → LLM timeout, task c0f31ae0), inject only the dws tables the goal
    actually touches, plus anything the plan's DWS designs reference.
    """
    goal_l = (goal or "").lower()
    dws_sources = {
        str(t).split(".")[-1].strip("`").lower()
        for d in (plan.dws_designs or [])
        for t in (getattr(d, "source_tables", None) or [])
    }
    if not goal_l:
        return dws_sources
    for key, exp in (state.get("explorations") or {}).items():
        if key.rsplit("#", 1)[-1].lower() != "dws":
            continue
        for t in (getattr(exp, "tables", None) or []):
            name = str(getattr(t, "name", "") or "")
            if not name:
                continue
            if any(kw in goal_l and kw in name.lower() for kw in _GOAL_RELEVANCE_KEYWORDS):
                dws_sources.add(name.lower())
    return dws_sources


def _serialize_blueprints_for_prompt(state: AgentState, layer: str | None = None) -> str:
    """Serialize matched blueprints as reference templates for the LLM.

    Blueprints carry prior successful designs for similar domains/table-structures.
    The LLM should use them as starting points, adapting rather than inventing.
    Includes the blueprint's actual design content (table patterns of the layer
    being designed) so the match is actionable, not just a name + score.
    """
    blueprints = state.get("matched_blueprints", [])
    if not blueprints:
        return "(No blueprint templates matched — design from scratch)"

    lines = ["## Reference Blueprint Templates"]
    lines.append("These blueprints matched your source schema. Use them as design starting points.")
    lines.append("")

    for i, bp in enumerate(blueprints[:3], 1):
        lines.append(f"### Blueprint {i}: {bp.blueprint_name} (score: {bp.score:.0%})")
        if getattr(bp, "description", ""):
            lines.append(f"  - Intent: {bp.description[:300]}")
        if bp.matched_tables:
            lines.append(f"  - Matched tables: {', '.join(bp.matched_tables)}")
        if bp.unmatched_tables:
            lines.append(f"  - Unmatched tables: {', '.join(bp.unmatched_tables)}")
        if bp.adaptations_needed:
            lines.append(f"  - Adaptations needed: {', '.join(bp.adaptations_needed)}")
        # Design content: table patterns of the layer currently being designed
        # (fall back to all layers when the blueprint has no entry for it).
        bp_layers = getattr(bp, "layers", []) or []
        if bp_layers and i <= 2:
            relevant = [
                l for l in bp_layers
                if isinstance(l, dict) and (l.get("layer") or "").upper() == (layer or "").upper()
            ] or [l for l in bp_layers if isinstance(l, dict)]
            for l in relevant[:2]:
                patterns = l.get("table_patterns") or []
                if patterns:
                    lines.append(f"  - {l.get('layer', '?')} layer table patterns:")
                    for p in patterns[:3]:
                        lines.append(f"      {_json.dumps(p, ensure_ascii=False)[:400]}")
        lines.append("")

    return "\n".join(lines)


def _serialize_incremental_recommendations(state: AgentState) -> str:
    """Serialize pre-computed incremental strategy recommendations for ODS layer.

    These are computed programmatically from schema analysis (timestamp columns,
    auto-increment IDs) and presented to the LLM as strong hints. The LLM can
    override them but must justify why.
    """
    explorations = state.get("explorations", {})
    if not explorations:
        return ""

    lines = ["## Incremental Strategy Recommendations (Pre-computed)"]
    lines.append("These are derived from schema analysis. Use as defaults unless you have a specific reason to override.")
    lines.append("")

    for db_id, exp in explorations.items():
        for table in exp.tables:
            strategy = _recommend_incremental(table)
            lines.append(
                f"  - {table.name}: "
                f"supports_incremental={strategy.supports_incremental}, "
                f"type={strategy.incremental_type}, "
                f"column={strategy.incremental_column or 'N/A'}, "
                f"frequency={strategy.recommended_frequency}"
            )
            lines.append(f"    Reason: {strategy.reason}")

    return "\n".join(lines)


# Entity/time keywords extracted from the modeling goal to pre-compute the
# required grain combinations: (match text, canonical label). Synonyms map to
# one label so 月度/按月/月 all yield "月" and 单位/组织 both yield "单位".
_GOAL_ENTITY_KEYWORDS = [
    ("单位", "单位"), ("组织", "单位"), ("部门", "部门"), ("店铺", "店铺"),
    ("楼栋", "店铺"), ("商品", "商品"), ("楼层", "楼层"), ("客户", "客户"),
    ("合同", "合同"), ("项目", "项目"), ("区域", "区域"),
]
_GOAL_TIME_KEYWORDS = [
    ("每日", "日"), ("按天", "日"), ("天", "日"),
    ("月度", "月"), ("按月", "月"), ("月", "月"),
    ("季度", "季"), ("按季", "季"), ("季", "季"),
    ("年度", "年"), ("按年", "年"), ("年", "年"),
]


def _extract_required_grains(goal: str) -> list[str]:
    """Programmatically extract the required grain combinations from the goal.

    The LLM previously parsed grain coverage out of free-text goals and
    repeatedly under-designed thin DWS tables (task d7418ecc: org×quarter +
    product×month/quarter could not cover the requested 单位×店铺×商品 × 月/季
    analysis, so ADS illegally read DWD). Computing the combos here — no LLM
    in the loop — and injecting them explicitly into the DWS prompt makes
    coverage a checklist instead of a guess.
    """
    if not goal:
        return []
    entities = [label for kw, label in _GOAL_ENTITY_KEYWORDS if kw in goal]
    times = [label for kw, label in _GOAL_TIME_KEYWORDS if kw in goal]
    # dedupe keeping first occurrence order
    entities = list(dict.fromkeys(entities))
    times = list(dict.fromkeys(times))
    combos = []
    for entity in entities:
        for t in times:
            combos.append(f"{entity}×{t}")
    # 三级穿透：goal 同时出现多个实体时，加一个全实体组合（如 单位×店铺×商品×月）
    if len(entities) >= 2 and times:
        chain = "×".join(entities + [times[0]])
        combos.append(chain)
    return combos


async def _llm_extract_grains(state: AgentState) -> list[str] | None:
    """LLM-based grain-combination extraction (semantic, domain-agnostic).

    Fixed-keyword extraction (unit/shop/product/月/季) cannot understand
    nested structures ("各个单位、各个月、季度的店铺、商品" implies
    shop×product drill-down), cannot filter noise (a topic noun like 合同 is
    not an analysis dimension), and does not generalize to other domains.
    Returns None on any failure so the keyword fallback takes over.
    """
    from wren_agent.tools.llm_client import get_llm_client

    goal = state.get("modeling_goal") or ""
    if not goal:
        return None
    llm = get_llm_client()
    system_prompt = (
        "You extract analysis grain combinations from a data-modeling goal. "
        "Return ONLY valid JSON."
    )
    user_message = f"""Extract the REQUIRED analysis grain combinations from the modeling goal.

Rules:
- Entity dimensions: business objects the goal wants to analyze BY (单位/组织、店铺/楼栋、商品、楼层、客户、部门、产品线、渠道、区域…). A topic noun that merely frames the subject (e.g. 合同 in "销售订单") is NOT an entity dimension.
- Time dimensions: 日/月/季/年 (dedupe synonyms like 月度/按月 → 月).
- Combinations: every entity×time pair the goal asks about; respect nesting ("季度的店铺、商品" → 店铺×季 AND 商品×季; "店铺的商品" → a 店铺×商品 drill-down chain).
- If the goal names no entity or time dimension, return empty lists.

Modeling goal: {goal}

Return JSON:
{{"grains": ["单位×月", "单位×季", ...], "drilldowns": ["单位×店铺×商品×月", ...]}}
Return ONLY the JSON object."""

    try:
        response = await llm.generate_json(
            system_prompt, user_message, token_kind="grill",
        )
        import json as _json
        data = _json.loads(response) if isinstance(response, str) else response
        grains = [str(g).strip() for g in (data.get("grains") or []) if str(g).strip()]
        drilldowns = [
            str(d).strip() for d in (data.get("drilldowns") or []) if str(d).strip()
        ]
        return list(dict.fromkeys(grains + drilldowns))
    except Exception:
        logger.warning(
            "[%s] LLM grain extraction failed — keyword fallback",
            state.get("task_id", "")[:8],
        )
        return None


def _serialize_required_grains(state: AgentState, layer: str) -> str:
    """Render the required grain combinations section for the design prompt.

    Injected for the DWS layer (and warehouse-mode ADS, which anchors on the
    plan's DWS tables) so every requested 实体×时间 combo is a checklist item
    instead of something the LLM must infer. Prefers the LLM-extracted
    combinations cached on the state; falls back to keyword extraction.

    Multi-entity combinations (`单位×店铺×月`) are a hard requirement: the
    design LLM used to build single-entity tables only (org / shop
    separately), then marked the JOINed grain `unsupported` — the PLAN check
    honestly rejected it and the task died in the revision loop (task
    8e76b357). The checklist now states explicitly that a multi-entity combo
    MUST materialize as its own table whose grain carries ALL entity keys
    (sources JOIN to provide them), not be declared unsupported.
    """
    if layer not in ("DWS", "ADS"):
        return ""
    grains = state.get("required_grains") or _extract_required_grains(
        state.get("modeling_goal") or ""
    )
    if not grains:
        return ""
    lines = ["\n## Required Grain Combinations (MANDATORY coverage checklist)"]
    lines.append(
        "Every combination below must be materialized as grain columns in at "
        "least one table of this plan — record each in `requirement_coverage` "
        "with the physical evidence columns. Never mark one unsupported when "
        "the source data can provide it."
    )
    for combo in grains:
        lines.append(f"- {combo}")
    # Multi-entity combos carry extra hard rules — this is the acceptance
    # failure behind task 8e76b357 (`单位×店铺×月` had no table at all).
    multi = [c for c in grains if _grain_entity_count(c) >= 2]
    if multi:
        lines.append(
            "\n### Multi-entity grains (HARD REQUIREMENT — do NOT mark unsupported)"
        )
        lines.append(
            "A combination with 2+ entities (e.g. 单位×店铺×月) MUST be materialized "
            "as its OWN summary table whose grain columns include ALL the entity "
            "keys + the time key. If the keys live in different source tables, "
            "JOIN them (the exploration lists verified relationship hints). "
            "Declaring it `unsupported` because only single-entity tables exist "
            "is NOT acceptable — build the JOINed table."
        )
        lines.append("Affected: " + ", ".join(multi))
    return "\n".join(lines)


def _grain_entity_count(combo: str) -> int:
    """Entity count of a grain combo: tokens split by ×, minus time dimensions
    (月/季/日/年 and synonyms). `单位×店铺×月` → 2 entities."""
    parts = [p.strip() for p in str(combo).split("×") if p.strip()]
    time_tokens = {"月", "季", "日", "年", "月度", "季度", "年度", "每天", "每月", "每周"}
    return sum(1 for p in parts if p not in time_tokens)


def _build_layer_context(state: AgentState, layer: str, compact: bool = False) -> dict[str, str]:
    """Build system_prompt and user_message for LLM-driven layer design.

    Returns {"system_prompt": ..., "user_message": ...}
    compact=True drops the optional context sections (blueprints, incremental
    hints) — used for the retry after a truncated LLM response.
    """
    from wren_agent.tools.llm_client import build_system_prompt

    # Map layer to skill name(s)
    skill_map = {
        "ODS": ["design-ods"],
        "DIM": ["design-dim"],
        "DWD": ["design-dwd-dws"],
        "DWS": ["design-dwd-dws"],
        "ADS": ["design-ads"],
    }
    skill_names = skill_map.get(layer, [])

    # System prompt: CONTEXT.md rules + skill content
    extra_context = (
        "You are a senior data warehouse architect designing DDL for Apache Doris. "
        "You MUST return ONLY valid JSON — no markdown, no explanation outside the JSON. "
        "Follow the design rules in the skill exactly. "
        "CRITICAL: Doris forbids STRING type in any KEY column (DUPLICATE/PRIMARY/AGGREGATE). "
        "Use VARCHAR(65533) instead of STRING when a key column would be STRING. "
        "Use TINYINT instead of BOOLEAN. Use DEFAULT 1 instead of DEFAULT TRUE. "
        "Doris DDL clause order: (columns) → KEY clause (UNIQUE/DUPLICATE/AGGREGATE "
        "KEY(...)) → COMMENT → DISTRIBUTED BY → PROPERTIES. COMMENT must NEVER precede "
        "the KEY clause — that is a parse error."
    )
    # Warehouse mode: the design LLM keeps qualifying new tables with another
    # layer's database (e.g. ADS DDL targeting dws_ecommerce_orders_sh, or a
    # invented ads_ecommerce_SH) — it is never told the landing db
    # (tasks c0f31ae0 / a2b0cea0). State it explicitly.
    if state.get("source_mode") == "warehouse":
        from wren_agent.nodes.deployment import _warehouse_layers
        layers = _warehouse_layers(state)
        landing_parts = [
            f"{layer.upper()} layer tables land in database `{db}`"
            for layer, db in layers.items() if db
        ]
        if landing_parts:
            extra_context += (
                "\n" + "; ".join(landing_parts)
                + ". DDL targets MUST use that layer's own database prefix — "
                "never another layer's database, never an invented one."
            )
    # Compiler mode (ADR-0015): the DWS/ADS LLM outputs semantic structure
    # only — the DesignCompiler renders DDL/load_sql. The skill's legacy
    # output template (ddl/load_sql text) would otherwise win; state the
    # contract explicitly.
    from wren_agent.config import settings
    if settings.llm.compiler_enabled and layer in ("DWS", "ADS"):
        ads_hint = (
            "\nADS specifics: `layer` must be \"ADS\", business metrics use "
            "is_measure=true with metric_type (atomic|derived) and, for "
            "derived metrics, parent_metrics listing the base DWS metric "
            "physical column names; declare target_audience."
            if layer == "ADS" else ""
        )
        bind_hint = (
            " ADS `source_expr` MUST use the EXACT physical column names from "
            "This Plan's DWS Layer (and that table's Columns) — do NOT copy "
            "skill-example identifiers such as stat_date / org_name / daily_amt "
            "when the DWS table has stat_month and no org_name (task 9f42f542)."
            if layer == "ADS" else ""
        )
        extra_context += (
            "\n\nCOMPILER MODE (" + layer + "): do NOT write ddl/load_sql text. "
            "Return a `semantic_designs` array — one object per table with "
            "columns (name, dtype, comment, is_key, is_measure, source_expr), "
            "sources, joins, filters, requirement_coverage. `source_expr` is the "
            "SELECT expression producing the column (SUM(amount), "
            "DATE(trans_dt), MAX(name), or a plain column). Business measures "
            "declare is_measure=true; descriptive attributes (MAX(name)) do "
            "not. `sources` (and `joins.right`) list exact schema.table "
            "names WITHOUT aliases — the compiler assigns a, b, c in source "
            "order. Multi-source designs MUST declare joins "
            "`{\"right\": \"<same exact table name>\", \"on\": \"...\"}` "
            "using those compiler aliases in `on`/`source_expr`. Grain columns "
            "(is_key=true) MUST lead the column list. "
            f"See the skill section \"Compiler Mode\".{ads_hint}{bind_hint}"
            "\nTable names: DWS ``summary_table`` is "
            "`dws_{topic}_{grain_entities}_{period}` so distinct grains never "
            "share a name (card×location → `dws_stock_inventory_card_loc_monthly`, "
            "org+month → `dws_stock_inventory_org_monthly` — NOT both "
            "`dws_stock_inventory_monthly`). Entity tokens: card, loc, org, "
            "bldg, store, product, unit, company. Period last: daily / monthly / "
            "quarterly / yearly. ADS: `ads_{topic}_{entities}_{purpose}` "
            "(质量/quality → quality). Doris table names MUST stay ≤ 64 "
            "characters — drop topic fillers (info/detail/snapshot) first; "
            "never drop the layer prefix or grain/period tokens. The compiler "
            "rewrites generic names to this pattern and suffixes `_vN` when "
            "the landing database already has that table."
            f"{_occupied_names_prompt_block(state, layer)}"
        )
    system_prompt = build_system_prompt(skill_names=skill_names, extra_context=extra_context)

    # User message: all available state context
    goal = state.get("modeling_goal", "(No modeling goal)")
    dw_target = state.get("dw_target", {})
    # Warehouse ADS anchors on the plan's DWS designs + the referenced
    # explored dws tables (narrowed inside _serialize_upstream_designs) — the
    # cross-layer exploration dump (up to 80 tables × 60 fields, 400K-char
    # budget) is pure redundant bloat that pushed the ADS call past the LLM
    # timeout (tasks a1af4bed/c0f31ae0: ADS design APITimeoutError, compact
    # retry). DWS still gets the full dump — it anchors on all layers.
    if layer == "ADS" and state.get("source_mode") == "warehouse":
        explorations_text = ""
    else:
        explorations_text = _serialize_explorations_for_prompt(state, compact=compact)
    clarifications_text = _serialize_clarifications_for_prompt(state)
    upstream_text = _serialize_upstream_designs(state, layer)
    blueprints_text = "" if compact else _serialize_blueprints_for_prompt(state, layer)
    incremental_text = (
        "" if compact
        else (_serialize_incremental_recommendations(state) if layer == "ODS" else "")
    )
    explorations_section = (
        f"## Source Schema Explorations\n{explorations_text}"
        if explorations_text else ""
    )
    # Warehouse DWS/ADS: the SQL hard rules (aliases, no 0-row sources, no
    # invented columns) must reach every layer prompt — they used to ride the
    # warehouse-table dump that the upstream dedup removed from DWS (task
    # 1206ee16: JOINs without explicit aliases).
    if state.get("source_mode") == "warehouse":
        explorations_section += f"\n\n{_warehouse_sql_hard_rules(state)}"

    user_message = f"""## Modeling Goal
{goal}

## Target Data Warehouse
Doris at {dw_target.get('host', 'unknown')}:{dw_target.get('port', 'unknown')}/{dw_target.get('database', 'unknown')}

{_INJECTION_ANCHOR}
{explorations_section}

{clarifications_text}

{blueprints_text}

{incremental_text}

{upstream_text}
{_serialize_required_grains(state, layer)}

## Task
Design the **{layer}** layer following the rules in the system prompt.
{_output_mode_line(state, layer)}

Current layer: {layer}"""

    return {"system_prompt": system_prompt, "user_message": user_message}


# ── Shared compact-retry suffix + context builder ──────────────────
# The three LLM design paths (first-call truncation retry, DWS/ADS compact
# retry, batch retry) all rebuild the SAME compact context and append the
# SAME "return compact JSON" instruction — they used to be three drifted
# copies. One construction path keeps the instruction text in one place.

# Injected sections (field semantics, registry) land at this anchor, right
# after the Target Data Warehouse header and BEFORE the schema dump. Appended
# to the tail of a huge user_message they were cut off by the trace cap / LLM
# timeout before the model ever saw them (task d28431de: 98 receivable-domain
# metrics existed in the registry but zero reached the DWS prompt — the prompt
# died at 100K chars mid-schema). The anchor is stripped after injections.
_INJECTION_ANCHOR = "__wren_injection_anchor__"


def _inject_into_user_message(user_message: str, text: str) -> str:
    """Insert an injected section at the anchor (before the schema dump);
    append when the anchor is gone. Empty text is a no-op."""
    if not text:
        return user_message
    if _INJECTION_ANCHOR in user_message:
        return user_message.replace(
            _INJECTION_ANCHOR, text + "\n" + _INJECTION_ANCHOR, 1)
    return user_message + text


_COMPACT_RETRY_SUFFIX = (
    "\n\nIMPORTANT: your previous generation failed or was cut off. "
    "Return COMPACT valid JSON only — short Chinese comments (≤20 chars), "
    "omit optional fields, no prose outside the JSON. "
    "At most 20 fields/measures per table, and finish with the exact "
    "closing braces of the JSON — never stop early."
)


async def _build_layer_context_with_injections(
    state: AgentState, layer: str, *, compact: bool = False,
    extra_suffix: str = "",
) -> dict[str, str]:
    """Build the layer context, inject field semantics + registry context,
    then append an optional suffix — the single construction path for every
    LLM design call (first attempt, truncation retry, compact retry, batch).
    """
    context = _build_layer_context(state, layer, compact=compact)
    await _inject_field_semantics(state, context)
    await _inject_registry_context(state, context, layer)
    context["user_message"] = context["user_message"].replace(_INJECTION_ANCHOR, "")
    if extra_suffix:
        context["user_message"] += extra_suffix
    return context


def _output_mode_line(state: AgentState, layer: str) -> str:
    """The `## Task` output-mode instruction. In compiler mode it MUST say
    `semantic_designs` explicitly — the legacy sentence ("Return designs,
    DDL statements…") sits in the user message and outweighs the system
    prompt's compiler-mode hint, so the LLM kept returning legacy shapes for
    ADS (acceptance finding: compiler runs 1/2 fell back to LLM-written ADS
    SQL)."""
    from wren_agent.config import settings

    if settings.llm.compiler_enabled and layer in ("DWS", "ADS"):
        return (
            "Return the layer's semantic designs as `semantic_designs` "
            "(columns with name/dtype/comment/is_key/is_measure/source_expr, "
            "sources, joins, filters, requirement_coverage). "
            "Do NOT write `ddl` / `load_sql` / `designs` — the DesignCompiler "
            "renders the SQL from the semantic structure."
        )
    return (
        "Return a complete JSON object with designs, DDL statements, and any "
        "warnings."
    )


# The field-semantics dump used to inject EVERY explored table's fields
# (~73K chars / 1100 lines / 48 tables — the largest prompt section, bigger
# than the exploration itself, task f295b345). Filter to goal-relevant tables
# and cap the line budget (tightened again in a659788b: still 25.7K of a
# 53K prompt — the second-biggest section).
_FIELD_SEMANTICS_MAX_LINES = 250
_FIELD_SEMANTICS_MAX_FIELDS_PER_TABLE = 15


def _semantics_table_relevant(table_name: str, goal_l: str) -> bool:
    """Table name contains a goal keyword — only these tables' semantics are
    injected; the rest are noise for the design LLM."""
    name = str(table_name).lower()
    return any(kw in goal_l and kw in name for kw in _GOAL_RELEVANCE_KEYWORDS)


def _format_field_semantics_prompt(
    semantics: dict[str, dict[str, dict]], goal: str = "",
) -> str:
    """Format table→field→meta semantics dict into a prompt fragment.

    Goal-filtered and line-capped: the full 48-table dump bloated every
    design prompt past the LLM timeout (task f295b345: ~73K of a 120K+ prompt).
    """
    if not semantics:
        return ""
    goal_l = (goal or "").lower()
    # When the goal keywords match nothing (e.g. a generic goal), fall back to
    # the first tables instead of dropping ALL semantics — the line cap still
    # bounds the dump.
    relevant = {
        t for t in semantics
        if not goal_l or _semantics_table_relevant(t, goal_l)
    }
    if goal_l and not relevant:
        relevant = set(semantics)
    auth_lines = ["\n## Known Field Semantics (authoritative)"]
    vec_lines = ["\n## Vector-Similar Field Semantics (medium confidence)"]
    kg_lines = ["\n## KG Similar-Table Field Semantics (lower confidence)"]
    kept = 0
    for table in sorted(relevant, key=lambda t: t.lower()):
        fields = semantics[table]
        if kept >= _FIELD_SEMANTICS_MAX_LINES:
            break
        for field, meta in list(fields.items())[:_FIELD_SEMANTICS_MAX_FIELDS_PER_TABLE]:
            if not isinstance(meta, dict) or not meta.get("chinese_meaning"):
                continue
            role = meta.get("field_role", "")
            meaning = meta["chinese_meaning"]
            if meta.get("sem_from") or meta.get("similarity") is not None:
                prov = (
                    f" (语义相似 {meta.get('sem_from')}, sim={meta.get('similarity')})"
                    if meta.get("sem_from") else ""
                )
                vec_lines.append(f"- {table}.{field}: {meaning} [{role}]{prov}")
            elif meta.get("kg_from"):
                kg_lines.append(
                    f"- {table}.{field}: {meaning} [{role}] "
                    f"(来自相似表 {meta.get('kg_from')})"
                )
            else:
                enum = meta.get("enum_values") or []
                enum_txt = f" 枚举: {'、'.join(str(e) for e in enum)}" if enum else ""
                auth_lines.append(f"- {table}.{field}: {meaning} [{role}]{enum_txt}")
            kept += 1
    parts = []
    if len(auth_lines) > 1:
        parts.append("\n".join(auth_lines))
    if len(vec_lines) > 1:
        parts.append("\n".join(vec_lines))
    if len(kg_lines) > 1:
        parts.append("\n".join(kg_lines))
    return "\n".join(parts)


async def _inject_field_semantics(state: AgentState, context: dict[str, str]) -> None:
    """Inject authoritative field semantics (dictionary + KG fallback) into the
    design prompt so the LLM does not guess or re-clarify known fields.

    Prefer task-scoped caches from grill (`field_semantics` /
    `_field_semantics_prompt`) so embeddings are not re-run on every layer.
    """
    cached = state.get("_field_semantics_prompt") or ""
    if cached:
        context["user_message"] = _inject_into_user_message(context["user_message"], cached)
        return

    semantics = state.get("field_semantics") or {}
    if semantics:
        text = _format_field_semantics_prompt(
            semantics, goal=state.get("modeling_goal", ""),
        )
        if text:
            state["_field_semantics_prompt"] = text
            context["user_message"] = _inject_into_user_message(
                context["user_message"], text)
        return

    try:
        from wren_agent.tools.pg_store import query_field_semantics

        tables = [
            t.name for exp in (state.get("explorations") or {}).values()
            for t in exp.tables
        ]
        rows = await query_field_semantics(tables) if tables else []
        built: dict[str, dict[str, dict]] = {}
        for r in rows:
            if not r.get("chinese_meaning"):
                continue
            built.setdefault(r["table_name"], {})[r["field_name"]] = r

        # Fallback chain for fields the dictionary does not cover exactly:
        #   1. vector similarity over embedded dictionary rows (synonyms:
        #      amt vs amount vs 金额) — medium confidence
        #   2. KG similar-table lookup (exact field name, earlier tasks) — low
        covered = {(t, f) for t, fields in built.items() for f in fields}
        pairs = [
            (t.name, f.name)
            for exp in (state.get("explorations") or {}).values()
            for t in exp.tables
            for f in t.fields
            if (t.name, f.name) not in covered
        ]
        if pairs:
            from wren_agent.tools.pg_store import semantic_field_semantics_fallback

            sem = await semantic_field_semantics_fallback(pairs)
            for table, fields in sem.items():
                built.setdefault(table, {}).update(fields)
            pairs = [(t, f) for t, f in pairs if f not in (sem.get(t) or {})]

        if pairs:
            from wren_agent.nodes.deployment import task_relevant_db_names
            from wren_agent.tools.neo4j_store import kg_field_semantics_fallback

            prefixes = sorted({f"{t.split('_')[0]}_" for t, _ in pairs if "_" in t})
            db_names = sorted(task_relevant_db_names(state))
            fb = await kg_field_semantics_fallback(
                pairs, prefixes, db_names=db_names or None)
            for table, fields in fb.items():
                built.setdefault(table, {}).update(fields)

        state["field_semantics"] = built
        text = _format_field_semantics_prompt(built)
        if text:
            state["_field_semantics_prompt"] = text
            context["user_message"] = _inject_into_user_message(
                context["user_message"], text)
    except Exception:
        logger.warning(
            "Field semantics injection failed — design continues without dictionary",
            exc_info=True,
        )


async def _inject_registry_context(state: AgentState, context: dict[str, str], layer: str) -> None:
    """Inject Dimension/Metric Registry summaries into design prompts.

    Layer-scoped + goal-scoped: DIM sees dimensions of the task's dim-layer
    databases; DWS sees dimensions AND metrics of the task's dim∪dwd-layer
    databases (what DWS design consumes); ADS sees metrics of the dws-layer
    databases. Entries are further narrowed to those semantically related to
    the modeling goal (embedding-based, best-effort). Business mode has no
    db scope — only the goal filter applies (empty db set = no db filter).
    """
    if layer not in ("DIM", "DWS", "ADS"):
        return
    from wren_agent.nodes.deployment import layer_relevant_db_names
    from wren_agent.tools.pg_store import query_dimension_registry, query_metric_registry

    goal = state.get("modeling_goal") or ""
    try:
        injected: list[str] = []  # (section, count) — logged for diagnosis
        if layer == "DIM":
            dim_dbs = sorted(layer_relevant_db_names(state, {"dim"}))
            dims = await query_dimension_registry(
                db_names=dim_dbs or None, goal=goal or None)
            if dims:
                lines = [
                    "\n## Registered Dimensions (dimension_registry — REUSE these "
                    "instead of creating duplicates)"
                ]
                if dim_dbs:
                    lines.insert(0, f"Scoped to dim layer databases: {', '.join(dim_dbs)}")
                for d in dims[:20]:
                    lines.append(
                        f"- {d.get('dim_name')} — table={d.get('dim_table')}, "
                        f"grain={d.get('grain') or '?'}"
                    )
                context["user_message"] = _inject_into_user_message(
                    context["user_message"], "\n" + "\n".join(lines))
                injected.append(f"dimensions={len(dims)}")
        elif layer == "DWS":
            dws_dbs = sorted(layer_relevant_db_names(state, {"dim", "dwd"}))
            dims = await query_dimension_registry(
                db_names=dws_dbs or None, goal=goal or None)
            if dims:
                lines = [
                    "\n## Registered Dimensions (dimension_registry — REUSE these "
                    "in DWS dimension columns instead of creating duplicates)"
                ]
                if dws_dbs:
                    lines.insert(0, f"Scoped to dim/dwd layer databases: {', '.join(dws_dbs)}")
                for d in dims[:20]:
                    lines.append(
                        f"- {d.get('dim_name')} — table={d.get('dim_table')}, "
                        f"grain={d.get('grain') or '?'}"
                    )
                context["user_message"] = _inject_into_user_message(
                    context["user_message"], "\n" + "\n".join(lines))
                injected.append(f"dimensions={len(dims)}")
            metrics = await query_metric_registry(
                db_names=dws_dbs or None, goal=goal or None)
            if metrics:
                lines = [
                    "\n## Registered Metrics (metric_registry — reuse as atomic "
                    "measures in DWS where applicable)"
                ]
                if dws_dbs:
                    lines.insert(0, f"Scoped to dim/dwd layer databases: {', '.join(dws_dbs)}")
                for m in metrics[:30]:
                    lines.append(
                        f"- {m.get('metric_name')} ({m.get('metric_type')}) — "
                        f"table={m.get('source_table')}, grain={m.get('grain') or '?'}"
                    )
                context["user_message"] = _inject_into_user_message(
                    context["user_message"], "\n" + "\n".join(lines))
                injected.append(f"metrics={len(metrics)}")
        else:  # ADS
            ads_dbs = sorted(layer_relevant_db_names(state, {"dws"}))
            metrics = await query_metric_registry(
                db_names=ads_dbs or None, goal=goal or None)
            if metrics:
                lines = [
                    "\n## Registered Metrics (metric_registry — check name/grain "
                    "conflicts before registering new ones)"
                ]
                if ads_dbs:
                    lines.insert(0, f"Scoped to dws layer databases: {', '.join(ads_dbs)}")
                lines.insert(0, (
                    "RULE: reuse the REGISTERED metric name, formula and grain for any "
                    "indicator that already exists in the list below — do NOT create a "
                    "near-duplicate with a new name. Derived variants (rates/ratios) "
                    "build on the registered atomic measures' physical column names."
                ))
                for m in metrics[:30]:
                    lines.append(
                        f"- {m.get('metric_name')} ({m.get('metric_type')}) — "
                        f"table={m.get('source_table')}, grain={m.get('grain') or '?'}"
                    )
                context["user_message"] = _inject_into_user_message(
                    context["user_message"], "\n" + "\n".join(lines))
                injected.append(f"metrics={len(metrics)}")
        if injected:
            logger.info(
                "[%s] registry context injected for %s: %s (dbs=%s)",
                state.get("task_id", "")[:8], layer,
                ", ".join(injected),
                sorted(
                    layer_relevant_db_names(
                        state, {"dim"} if layer == "DIM"
                        else {"dim", "dwd"} if layer == "DWS" else {"dws"})
                ),
            )
        else:
            logger.warning(
                "[%s] registry context EMPTY for %s — no dimensions/metrics "
                "matched the goal+db scope (db scope=%s); registry reuse is "
                "skipped for this layer",
                state.get("task_id", "")[:8], layer,
                sorted(
                    layer_relevant_db_names(
                        state, {"dim"} if layer == "DIM"
                        else {"dim", "dwd"} if layer == "DWS" else {"dws"})
                ),
            )
    except Exception:
        logger.warning(
            "Registry context injection failed — design continues without registry",
            exc_info=True,
        )


async def _llm_generate_layer(state: AgentState, layer: str) -> dict:
    """Call LLM to generate designs for a layer. Returns parsed JSON dict.

    Raises Exception on LLM failure — caller should fall back to programmatic.
    A truncated response (unbalanced braces) triggers ONE retry with a compact
    prompt before giving up.
    """
    from wren_agent.tools.llm_client import get_llm_client

    llm = get_llm_client()
    task = state.get("task_id", "")[:8]

    # Semantic grain extraction for the DWS/ADS checklist — one small LLM
    # call, cached on state, keyword fallback on failure.
    if layer in ("DWS", "ADS") and not state.get("required_grains"):
        grains = await _llm_extract_grains(state)
        if grains:
            state["required_grains"] = grains

    context = await _build_layer_context_with_injections(state, layer)

    logger.info("[%s] Generating %s layer via LLM...", task, layer)
    response = await llm.generate_json(
        system_prompt=context["system_prompt"],
        user_message=context["user_message"],
        token_kind="design",
    )
    try:
        result = _extract_json_from_llm_response(response)
    except ValueError as e:
        if "truncated" not in str(e):
            raise
        # Retry once with a slimmed prompt: drop optional context sections
        # (blueprints, incremental hints) and demand compact output.
        logger.warning(
            "[%s] %s layer LLM response truncated — retrying with compact prompt",
            task, layer,
        )
        context = await _build_layer_context_with_injections(
            state, layer, compact=True, extra_suffix=_COMPACT_RETRY_SUFFIX,
        )
        response = await llm.generate_json(
            system_prompt=context["system_prompt"],
            user_message=context["user_message"],
            token_kind="design",
        )
        result = _extract_json_from_llm_response(response)

    logger.info("[%s] %s layer LLM generation complete: %d designs",
                task, layer,
                len(result.get("designs", [])) + len(result.get("dwd_designs", [])) + len(result.get("dws_designs", [])))
    return result


# ── Helper: incremental strategy recommendation ─────────────────


def _recommend_incremental(table) -> IncrementalStrategy:
    """Recommend incremental strategy based on schema analysis."""
    table_name = table.name if hasattr(table, "name") else table["name"]
    fields = table.fields if hasattr(table, "fields") else table.get("fields", [])

    incremental_col = None
    for f in fields:
        name = f.name if hasattr(f, "name") else f.get("name", "")
        dtype = (f.data_type if hasattr(f, "data_type") else f.get("data_type", "")).upper()
        # Look for timestamp / datetime columns
        if any(kw in name.lower() for kw in ("updated_at", "modified", "update_time", "modify_time", "gmt_modified")):
            incremental_col = name
            break
        # Look for auto-increment IDs
        if any(kw in name.lower() for kw in ("auto_increment",)):
            if dtype in ("BIGINT", "INT", "INTEGER"):
                incremental_col = name
                break

    if incremental_col:
        return IncrementalStrategy(
            table_name=table_name,
            supports_incremental=True,
            incremental_column=incremental_col,
            incremental_type="incremental_with_full_reconcile",
            recommended_frequency="daily",
            reason=f"Detected incremental column: {incremental_col}",
        )
    else:
        return IncrementalStrategy(
            table_name=table_name,
            supports_incremental=False,
            incremental_type="full",
            recommended_frequency="daily",
            reason="No incremental column detected — full sync recommended",
        )


def _is_realtime_required(modeling_goal: str) -> bool:
    """Check if the modeling goal requires realtime sync."""
    keywords = ["实时", "准实时", "realtime", "real-time", "streaming", "秒级", "分钟级"]
    return any(kw in modeling_goal.lower() for kw in keywords)


# ── LLM result → pydantic model parsers ───────────────────────────


def _ods_table_ref(source_table: str, ods_database: str = "") -> str:
    """Bare ODS table, optionally qualified with the ODS landing database.

    DIM/DWD load SQL runs on the dim/dwd datasource, so an unqualified
    `FROM ods_x` looks in the wrong database (task 9c52047f).
    """
    bare = str(source_table or "").split(".")[-1].strip("`")
    if not bare:
        return ""
    if not bare.lower().startswith("ods_"):
        bare = f"ods_{bare}"
    db = str(ods_database or "").strip()
    return f"{db}.{bare}" if db else bare


def _insert_select_sql(
    target: str, columns: list[str], select_exprs: list[str], source: str,
) -> str:
    col_list = ", ".join(columns)
    sel_list = ", ".join(select_exprs)
    return (
        f"INSERT INTO {target} ({col_list})\n"
        f"SELECT {sel_list}\n"
        f"FROM {source}"
    )


def _dim_field_source_column(field_name: str) -> str:
    if field_name in {"dim_id", "dim_name", "dim_code"}:
        return field_name[4:]
    return field_name


def _dim_bootstrap_load_sql(dim: DIMDesign, ods_database: str = "") -> str:
    """INSERT ... SELECT that bootstraps a new SCD2 dimension from ODS."""
    source = _ods_table_ref(dim.source_table or "", ods_database)
    surrogate = dim.surrogate_key or f"{dim.dim_name}_id"
    natural_key = (dim.natural_key or ["id"])[0]
    field_names = [
        str(f.get("name") or "") for f in (dim.fields or [])
        if isinstance(f, dict) and f.get("name")
    ]
    columns = [surrogate, *field_names, "effective_date", "expiration_date", "is_current"]
    selects = [natural_key, *(_dim_field_source_column(n) for n in field_names),
               "CURRENT_DATE()", "CAST('9999-12-31' AS DATE)", "1"]
    return _insert_select_sql(dim.dim_table, columns, selects, source)


def _dwd_bootstrap_load_sql(dwd: DWDDesign, ods_database: str = "") -> str:
    """INSERT ... SELECT that loads DWD attribute columns from the first ODS source."""
    source = _ods_table_ref(
        (dwd.source_ods_tables or [""])[0], ods_database,
    )
    columns: list[str] = []
    selects: list[str] = []
    for field in (dwd.fields or []):
        if not isinstance(field, dict) or not field.get("name"):
            continue
        columns.append(str(field["name"]))
        selects.append(str(field.get("source_column") or field["name"]).split(".")[-1])
    return _insert_select_sql(dwd.fact_table, columns, selects, source)


def _append_materialized_sql_artifacts(
    artifacts: list[SQLArtifact],
    *,
    layer: str,
    target_table: str,
    design: dict,
    dependencies: list[str],
) -> None:
    """Convert a materialized layer response into executable DDL + DML.

    A CREATE TABLE alone is not a warehouse pipeline.  DIM/DWD/DWS/ADS
    responses must also provide the INSERT/MERGE statement that populates
    the target table.  Keeping both statements as first-class artifacts lets
    deployment preserve their dependency order and lets validation reject
    empty-table designs.
    """
    ddl = design.get("ddl")
    if ddl:
        artifacts.append(SQLArtifact(
            layer=layer,
            target_table=target_table,
            sql_type="DDL",
            sql=ddl,
            dependencies=dependencies,
        ))

    load_sql = design.get("load_sql") or design.get("dml")
    if load_sql:
        sql_type = "DML_MERGE" if load_sql.lstrip().upper().startswith("MERGE") else "DML_INSERT"
        artifacts.append(SQLArtifact(
            layer=layer,
            target_table=target_table,
            sql_type=sql_type,
            sql=load_sql,
            dependencies=dependencies,
            description=f"Populate {target_table} from its declared upstream tables",
        ))


def _explored_table_count(state: AgentState) -> int:
    explorations = state.get("explorations") or {}
    return sum(len(exp.tables) for exp in explorations.values())


def _require_llm_sql_artifacts(
    state: AgentState,
    layer: str,
    sql_artifacts: list,
    *,
    designs: list | None = None,
) -> None:
    """Raise to trigger programmatic fallback when LLM returned shell designs.

    Only two cases are incomplete (not "legitimately empty"):
      1. designs exist but no SQL for this layer (metadata without DDL)
      2. ODS with explored tables but zero designs (ODS must materialize sources)
    DIM/DWD/ADS may legitimately return no designs — do not force fallback.
    """
    if state.get("source_mode") == "warehouse" and layer in ("ODS", "DIM", "DWD"):
        return
    layer_artifacts = [
        a for a in sql_artifacts
        if a.layer.upper() == layer.upper()
    ]
    if layer_artifacts:
        return
    designs = designs or []
    if designs:
        raise ValueError(
            f"{layer} LLM returned {len(designs)} design(s) without SQL artifacts"
        )
    if layer == "ODS" and _explored_table_count(state) > 0:
        raise ValueError(
            "ODS LLM returned no designs despite explored tables"
        )


def _materialization_targets_complete(
    artifacts: list[SQLArtifact], layer: str, targets: list[str]
) -> bool:
    """Return True when every materialized target has both DDL and load SQL."""
    for target in targets:
        target_types = {
            a.sql_type.upper()
            for a in artifacts
            if a.layer.upper() == layer and a.target_table == target
        }
        if "DDL" not in target_types or not any(t.startswith("DML_") for t in target_types):
            return False
    return True


# Keys each layer's parser understands. A response whose keys match NONE of
# them is a schema violation — the LLM changed the output format, and the
# lenient `.get()` below would silently parse it as an empty design (root of
# several insufficient_warehouse false positives + blind repair rounds).
# Raising here funnels into the existing attempt→compact-retry→fail-fast
# ladder, where the failure is at least visible.
_LAYER_RESULT_KEYS: dict[str, tuple[str, ...]] = {
    "ODS": ("designs", "warnings"),
    "DIM": ("designs", "warnings"),
    "DWD": ("dwd_designs", "dws_designs", "warnings"),
    "DWS": ("dwd_designs", "dws_designs", "semantic_designs", "warnings"),
    "ADS": ("designs", "skip", "semantic_designs", "warnings"),
}


def _require_recognized_output(result: dict, layer: str) -> None:
    """Raise when a non-empty LLM result has none of the layer's known keys.

    Empty objects and warnings-only responses stay valid — a legitimately
    empty layer (e.g. ADS with no metrics) must not be misrouted.
    """
    if not result:
        return
    recognized = _LAYER_RESULT_KEYS[layer]
    if any(key in result for key in recognized):
        return
    raise ValueError(
        f"{layer} LLM response has no recognized keys {recognized}; "
        f"got: {sorted(result.keys())[:10]} — the LLM returned an "
        f"off-schema object, not an empty design."
    )


def _parse_ods_llm_result(result: dict, state: AgentState) -> tuple[list[ODSDesign], list[DataXConfig], list[FlinkCDCConfig], list[SQLArtifact], list[IncrementalStrategy]]:
    """Parse LLM-generated ODS JSON into pydantic models."""
    _require_recognized_output(result, "ODS")
    ods_designs = []
    datax_configs = []
    flink_configs = []
    sql_artifacts = []
    incremental_strategies = []

    for d in result.get("designs", []):
        ods_design = ODSDesign(
            source_table=d["source_table"],
            source_db_id=d.get("source_db_id", ""),
            ods_table=d["ods_table"],
            fields=d.get("fields", []),
            sync_method=d.get("sync_method", "datax"),
            incremental_strategy=d.get("incremental_strategy", "full"),
            incremental_column=d.get("incremental_column"),
            schedule_frequency=d.get("schedule_frequency", "daily"),
        )
        ods_designs.append(ods_design)

        if d.get("ddl"):
            sql_artifacts.append(SQLArtifact(
                layer="ODS",
                target_table=d["ods_table"],
                sql_type="DDL",
                sql=_ensure_ods_metadata_defaults(d["ddl"]),
                description=f"ODS table for {d['source_table']}",
            ))

        incremental_strategies.append(IncrementalStrategy(
            table_name=d["source_table"],
            supports_incremental=d.get("incremental_strategy") != "full",
            incremental_column=d.get("incremental_column"),
            incremental_type=d.get("incremental_strategy", "full"),
            recommended_frequency=d.get("schedule_frequency", "daily"),
            reason=f"LLM recommended: {d.get('incremental_strategy', 'full')}",
        ))

    return ods_designs, datax_configs, flink_configs, sql_artifacts, incremental_strategies


def _parse_dim_llm_result(result: dict) -> tuple[list[DIMDesign], list[SQLArtifact]]:
    """Parse LLM-generated DIM JSON into pydantic models."""
    _require_recognized_output(result, "DIM")
    dim_designs = []
    sql_artifacts = []

    for d in result.get("designs", []):
        dim_designs.append(DIMDesign(
            dim_name=d["dim_name"],
            dim_table=d["dim_table"],
            source=d.get("source", "from_source_table"),
            source_table=d.get("source_table"),
            existing_dim_ref=d.get("existing_dim_ref"),
            fields=d.get("fields", []),
            natural_key=d.get("natural_key", []),
            surrogate_key=d.get("surrogate_key"),
            version=d.get("version", 1),
            registered=d.get("registered", False),
        ))
        if d.get("source") == "from_existing_dim" and not d.get("ddl"):
            continue
        _append_materialized_sql_artifacts(
            sql_artifacts,
            layer="DIM",
            target_table=d["dim_table"],
            design=d,
            dependencies=[d["source_table"]] if d.get("source_table") else [],
        )

    return dim_designs, sql_artifacts


def _parse_dwd_dws_llm_result(result: dict) -> tuple[list[DWDDesign], list[DWSDesign], list[SQLArtifact]]:
    """Parse LLM-generated DWD+DWS JSON into pydantic models."""
    # DWD and DWS recognize the same key set (both layers use the shared
    # design-dwd-dws skill JSON) — one guard covers both call sites.
    _require_recognized_output(result, "DWD")
    dwd_designs = []
    dws_designs = []
    sql_artifacts = []

    for d in result.get("dwd_designs", []):
        dwd_designs.append(DWDDesign(
            fact_name=d["fact_name"],
            fact_table=d["fact_table"],
            business_process=d.get("business_process", ""),
            source_ods_tables=d.get("source_ods_tables", []),
            fields=d.get("fields", []),
            measures=d.get("measures", []),
            dimension_refs=d.get("dimension_refs", []),
            grain=d.get("grain", []),
        ))
        _append_materialized_sql_artifacts(
            sql_artifacts,
            layer="DWD",
            target_table=d["fact_table"],
            design=d,
            dependencies=d.get("source_ods_tables", []) + d.get("dimension_refs", []),
        )

    for d in result.get("dws_designs", []):
        source_tables = d.get("source_tables", [])
        source_dwd_table = d.get("source_dwd_table", "")
        dependencies = source_tables or ([source_dwd_table] if source_dwd_table else [])
        dws_designs.append(DWSDesign(
            summary_name=d["summary_name"],
            summary_table=d["summary_table"],
            source_dwd_table=source_dwd_table,
            source_tables=source_tables,
            fields=d.get("fields", []),
            measures=d.get("measures", []),
            group_by_dimensions=d.get("group_by_dimensions", []),
            grain=d.get("grain", []),
            requirement_coverage=d.get("requirement_coverage", []),
            refresh_logic=d.get("refresh_logic", "incremental"),
        ))
        _append_materialized_sql_artifacts(
            sql_artifacts,
            layer="DWS",
            target_table=d["summary_table"],
            design=d,
            dependencies=dependencies,
        )

    return dwd_designs, dws_designs, sql_artifacts


def _parse_ads_llm_result(result: dict) -> tuple[list[ADSDesign], list[SQLArtifact], bool, list[str]]:
    """Parse LLM-generated ADS JSON into pydantic models."""
    _require_recognized_output(result, "ADS")
    if result.get("skip", False):
        return [], [], True, result.get("warnings", [])

    ads_designs = []
    sql_artifacts = []

    for d in result.get("designs", []):
        ads_designs.append(ADSDesign(
            application_name=d["application_name"],
            ads_table=d["ads_table"],
            description=d.get("description", ""),
            source_dws_tables=d.get("source_dws_tables", []),
            dimensions=d.get("dimensions", []),
            fields=d.get("fields", []),
            metrics=d.get("metrics", []),
            requirement_coverage=d.get("requirement_coverage", []),
            target_audience=d.get("target_audience", ""),
        ))
        _append_materialized_sql_artifacts(
            sql_artifacts,
            layer="ADS",
            target_table=d["ads_table"],
            design=d,
            dependencies=d.get("source_dws_tables", []),
        )

    return ads_designs, sql_artifacts, False, result.get("warnings", [])


# ── Compiler-mode DWS path (ADR-0015 Phase 1) ───────────────────────
# When settings.llm.compiler_enabled, the DWS LLM outputs `semantic_designs`
# (columns with source_expr, sources, filters) instead of ddl/load_sql text;
# the DesignCompiler renders both statements deterministically. The legacy
# path below stays intact and is chosen whenever the semantic key is absent
# or the flag is off.


def _parse_semantic_result(result: dict, layer: str) -> list[SemanticDesign] | None:
    """Parse compiler-mode semantic designs; None → LLM did not emit the key.

    Every entry must declare the expected ``layer`` (DWS or ADS); malformed or
    wrong-layer entries raise (→ compact retry / fail-fast ladder) instead of
    silently dropping tables.
    """
    raw = result.get("semantic_designs")
    if raw is None:
        return None
    designs = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("semantic_designs entries must be objects")
        declared = str(item.get("layer") or layer).upper()
        if declared != layer:
            raise ValueError(
                f"semantic_designs entry declares layer={declared}, "
                f"expected {layer} — wrong-layer designs are not consumed here"
            )
        designs.append(SemanticDesign.model_validate(item))
    return designs


def _dws_design_from_semantic(sd: SemanticDesign, target: str) -> DWSDesign:
    """Map a compiled semantic design onto DWSDesign so every downstream
    consumer (ADS parent_metrics, requirement coverage, reports) works
    unchanged. Measure columns = aggregations; everything else = fields."""
    from wren_agent.compiler.rules import (
        map_doris_type, render_comment, strip_source_alias,
    )

    measure_names = {m.name for m in sd.measure_columns}
    fields = [
        {
            "name": c.name,
            "type": map_doris_type(c.dtype, is_key=c.is_key),
            "comment": render_comment(c.comment, c.name),
        }
        for c in sd.columns if c.name not in measure_names
    ]
    measures = [
        {
            "name": m.name,
            "expression": m.source_expr or m.name,
            "type": map_doris_type(m.dtype),
            "comment": render_comment(m.comment, m.name),
        }
        for m in sd.measure_columns
    ]
    bare_sources = [strip_source_alias(s) for s in sd.sources]
    return DWSDesign(
        summary_name=sd.summary_name or target,
        summary_table=target,
        source_dwd_table=bare_sources[0] if len(bare_sources) == 1 else "",
        source_tables=bare_sources,
        fields=fields,
        measures=measures,
        group_by_dimensions=sd.grain,
        grain=sd.grain,
        requirement_coverage=sd.requirement_coverage,
        refresh_logic=sd.refresh_logic,
    )


async def _try_compiler_dws(
    state: AgentState, result: dict,
) -> tuple[bool, list[DWSDesign], list[SQLArtifact]]:
    """Compiler-mode DWS: parse semantic designs, compile, map to plan models.

    Returns (used, dws_designs, sql_artifacts). ``used=False`` when the
    compiler path does not apply (flag off, or the LLM did not emit
    `semantic_designs`) — the caller then runs the legacy parse. Designs that
    fail to compile safely keep their semantic design and a
    ``compiler_failures`` record (closed-world JOIN) so design_revise can
    pick a real column; the compiler never emits SQL it cannot vouch for.
    """
    from wren_agent.compiler import landing_db_for_layer
    from wren_agent.compiler.rules import apply_composed_table_name, strip_source_alias
    from wren_agent.config import settings
    from wren_agent.tools.compiler_fn_admit import compile_design_with_admit

    if not settings.llm.compiler_enabled:
        return False, [], []
    semantic = _parse_semantic_result(result, "DWS")
    if semantic is None:
        return False, [], []

    landing = landing_db_for_layer(state, "DWS")
    occupied = await occupied_landing_names(state, "DWS")
    dws_designs: list[DWSDesign] = []
    artifacts: list[SQLArtifact] = []
    for sd in semantic:
        bare = apply_composed_table_name(sd, occupied=occupied)
        occupied.add(bare)
        target = f"{landing}.{bare}" if landing else bare
        compiled, problems = await compile_design_with_admit(
            state, sd, layer_landing_db=landing, table_comment=sd.summary_name,
            source_columns=explored_columns_for_sources(state, sd.sources),
            allowed_source_tables=compile_source_allowlist(state, "DWS"),
        )
        # Keep the semantic source even when compile is refused — closed-world
        # JOIN failures (task 2efe882a: b.dim_id) must reach design_revise
        # with Available columns, not T-05 insufficient_warehouse.
        compiled_semantics = dict(state.get("compiled_semantics") or {})
        compiled_semantics[target] = sd
        state["compiled_semantics"] = compiled_semantics
        dws_designs.append(_dws_design_from_semantic(sd, target))
        if compiled is None:
            _record_compiler_failure(state, "DWS", target, sd, problems)
            continue
        _clear_compiler_failure(state, target)
        artifacts.append(SQLArtifact(
            layer="DWS", target_table=compiled.target_table,
            sql_type="DDL", sql=compiled.ddl,
            dependencies=[strip_source_alias(s) for s in sd.sources],
            description=f"Compiled DDL for {target} (DesignCompiler)",
        ))
        artifacts.append(SQLArtifact(
            layer="DWS", target_table=compiled.target_table,
            sql_type="DML_INSERT", sql=compiled.load_sql,
            dependencies=[strip_source_alias(s) for s in sd.sources],
            description=f"Compiled load for {target} (DesignCompiler)",
        ))
    return True, dws_designs, artifacts


def _compiler_check_name(problems: list[str]) -> str:
    if any("goal source allowlist" in p or "Allowed tables" in p for p in problems):
        return "compiler_source_allowlist"
    if any("not in explored source" in p or "Available columns" in p for p in problems):
        return "compiler_closed_world"
    return "compiler_uncompilable"


def _record_compiler_failure(
    state: AgentState, layer: str, target: str, sd, problems: list[str],
) -> None:
    """Persist a refused compile so harness/design_revise see Available columns."""
    detail = "; ".join(problems[:8]) if problems else "uncompilable semantic design"
    failures = [
        f for f in (state.get("compiler_failures") or [])
        if f.get("target_table") != target
    ]
    failures.append({
        "layer": layer,
        "check": _compiler_check_name(problems),
        "target_table": target,
        "detail": f"{target}: {detail}",
    })
    state["compiler_failures"] = failures
    state["warnings"] = state.get("warnings", []) + [
        f"[compiler] {layer} table `{getattr(sd, 'summary_table', target)}` "
        f"not compiled — {detail}"
    ]


def _clear_compiler_failure(state: AgentState, target: str) -> None:
    state["compiler_failures"] = [
        f for f in (state.get("compiler_failures") or [])
        if f.get("target_table") != target
    ]


def dws_columns_for_sources(plan, sources: list[str]) -> set[str] | None:
    """Physical column names of plan DWS tables matching ``sources``.

    Returns None when no source matches a plan DWS table (caller skips
    binding). Used by the ADS compiler so skill-example names like
    ``stat_date`` / ``org_name`` are rewritten or dropped against THIS
    plan's DWS (task 9f42f542).
    """
    from wren_agent.compiler.rules import strip_db_prefix, strip_source_alias

    if plan is None or not sources:
        return None
    wanted: set[str] = set()
    for src in sources:
        clean = strip_source_alias(src)
        wanted.add(clean.lower())
        wanted.add(strip_db_prefix(clean).lower())
        wanted.add(str(src).lower().strip())
    cols: set[str] = set()
    matched = False
    for design in (getattr(plan, "dws_designs", None) or []):
        tbl = str(getattr(design, "summary_table", "") or "").lower()
        if not tbl:
            continue
        bare = tbl.rsplit(".", 1)[-1]
        if not ({tbl, bare} & wanted):
            continue
        matched = True
        for field in (getattr(design, "fields", None) or []):
            name = (
                field.get("name") if isinstance(field, dict)
                else getattr(field, "name", "")
            )
            if name:
                cols.add(str(name).lower())
        for measure in (getattr(design, "measures", None) or []):
            if isinstance(measure, dict):
                name = measure.get("field_name") or measure.get("name") or ""
            else:
                name = getattr(measure, "name", "")
            if name:
                cols.add(str(name).lower())
        for grain_col in (getattr(design, "grain", None) or []):
            cols.add(str(grain_col).lower())
        for grain_col in (getattr(design, "group_by_dimensions", None) or []):
            cols.add(str(grain_col).lower())
    return cols if matched and cols else None


def ads_bind_source_columns(state: AgentState, plan, sources: list[str]) -> set[str] | None:
    """Columns ADS compile/repair must bind against (same set as harness).

    Harness ``setdefault`` prefers explored tables over this plan's DDL, so
    a leftover DWS from an earlier run wins (task f7055bbf: explored
    ``monthly_inventory_row_count`` vs this plan/ADS ``line_count``). Bind
    against explored columns when present; otherwise this plan's DWS.
    """
    explored = explored_columns_for_sources(state, sources)
    if explored:
        cols: set[str] = set()
        for value in explored.values():
            cols |= set(value)
        if cols:
            return cols
    return dws_columns_for_sources(plan, sources)


def explored_columns_for_sources(
    state: AgentState, sources: list[str],
) -> dict[str, set[str]] | None:
    """Per-alias explored columns for DWS compiler bind (task 449dc3ca).

    Core modeling publishes DIM ``dim_code``/``dim_name``; warehouse DWS
    guesses ``card_code``/``card_name``. Binding ``b.*`` against the DIM
    table (not a union of every JOIN source) lets the compiler remap or
    drop those identifiers before harness.
    """
    from wren_agent.compiler.rules import strip_db_prefix, strip_source_alias
    from wren_agent.compiler.templates import _source_aliases

    if not sources:
        return None
    index: dict[str, set[str]] = {}
    for exploration in (state.get("explorations") or {}).values():
        tables = getattr(exploration, "tables", None)
        if tables is None and isinstance(exploration, dict):
            tables = exploration.get("tables", [])
        for table in tables or []:
            name = getattr(table, "name", None)
            schema = getattr(table, "schema_name", None)
            fields = getattr(table, "fields", None)
            if isinstance(table, dict):
                name = name or table.get("name")
                schema = schema or table.get("schema_name")
                fields = fields if fields is not None else table.get("fields", [])
            if not name:
                continue
            cols: set[str] = set()
            for field in fields or []:
                fname = (
                    field.get("name") if isinstance(field, dict)
                    else getattr(field, "name", None)
                )
                if fname:
                    cols.add(str(fname).lower())
            if not cols:
                continue
            bare = str(name).lower()
            index[bare] = cols
            if schema:
                index[f"{str(schema).lower()}.{bare}"] = cols

    if not index:
        return None
    aliases = _source_aliases(list(sources))
    out: dict[str, set[str]] = {}
    resolved = 0
    for src in sources:
        clean = strip_source_alias(src)
        bare = strip_db_prefix(clean)
        cols = (
            index.get(clean.lower())
            or index.get(bare.lower())
            or index.get(str(src).lower().strip())
        )
        if cols is None:
            continue
        resolved += 1
        alias = aliases.get(src) or aliases.get(clean) or aliases.get(bare)
        if alias:
            out[alias] = cols
        out[clean.lower()] = cols
        out[bare.lower()] = cols
    return out if resolved else None


def explored_columns_prompt_block(state: AgentState, sources: list[str]) -> str:
    """Closed-world column list for the semantic repair prompt (task 2efe882a).

    JOIN ``on`` / filters must pick physical names from this snapshot. The
    compiler will not remap a missing JOIN key to a similar column.
    """
    from wren_agent.compiler.rules import strip_source_alias
    from wren_agent.compiler.templates import _source_aliases

    explored = explored_columns_for_sources(state, sources)
    if not explored or not sources:
        return ""
    aliases = _source_aliases(list(sources))
    lines = [
        "## Explored source columns (closed world)",
        "JOIN `on` / filters MUST use these physical names. The compiler will "
        "not rewrite a missing JOIN key (e.g. dim_id) to another column — pick "
        "an identifier from the list for that alias.",
    ]
    seen: set[tuple[str, str]] = set()
    for src in sources:
        clean = strip_source_alias(src)
        alias = aliases.get(src) or aliases.get(clean) or "?"
        cols = (
            explored.get(alias)
            or explored.get(clean.lower())
            or explored.get(str(src).lower().strip())
        )
        if not cols:
            continue
        key = (alias, clean.lower())
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- `{clean}` (alias {alias}): {', '.join(sorted(cols))}")
    if len(lines) <= 3:
        return ""
    return "\n".join(lines)


def explored_bare_table_names(state: AgentState) -> set[str]:
    """Bare table names from this task's exploration snapshot."""
    names: set[str] = set()
    for exploration in (state.get("explorations") or {}).values():
        tables = getattr(exploration, "tables", None)
        if tables is None and isinstance(exploration, dict):
            tables = exploration.get("tables", [])
        for table in tables or []:
            name = getattr(table, "name", None)
            if isinstance(table, dict):
                name = name or table.get("name")
            if name:
                names.add(str(name).lower())
    return names


def occupied_landing_names_from_state(state: AgentState, layer: str) -> set[str]:
    """Bare names already in this layer's explored catalog (not this plan)."""
    layer_l = (layer or "").strip().lower()
    names: set[str] = set()
    warehouse = state.get("source_mode") == "warehouse"
    for key, exp in (state.get("explorations") or {}).items():
        exp_layer = str(key).rsplit("#", 1)[-1] if "#" in str(key) else ""
        if warehouse and exp_layer and layer_l and exp_layer != layer_l:
            continue
        catalogs = getattr(exp, "catalog_table_names", None)
        if catalogs is None and isinstance(exp, dict):
            catalogs = exp.get("catalog_table_names")
        for n in catalogs or []:
            if n:
                names.add(str(n).split(".")[-1].lower().strip("`"))
        tables = getattr(exp, "tables", None)
        if tables is None and isinstance(exp, dict):
            tables = exp.get("tables", [])
        for table in tables or []:
            name = getattr(table, "name", None)
            if isinstance(table, dict):
                name = name or table.get("name")
            if name:
                names.add(str(name).lower().strip("`"))
    return names


async def fetch_landing_table_names(database: str = "") -> list[str]:
    """Live ``SHOW TABLES`` for a landing database. Empty on any failure."""
    if not database:
        return []
    try:
        from wren_agent.tools.doris_client import DorisClient
        client = DorisClient(database=database)
        return [t for t in (await client.list_tables()) if t]
    except Exception:
        logger.warning(
            "landing table catalog unavailable for database %s",
            database, exc_info=True,
        )
        return []


async def occupied_landing_names(state: AgentState, layer: str) -> set[str]:
    """Occupied bare names: exploration catalog ∪ live landing ``SHOW TABLES``."""
    names = occupied_landing_names_from_state(state, layer)
    try:
        from wren_agent.compiler import landing_db_for_layer
        landing = landing_db_for_layer(state, layer)
    except Exception:
        landing = ""
    if landing:
        live = await fetch_landing_table_names(landing)
        names |= {str(t).lower() for t in live if t}
    return names


def _occupied_names_prompt_block(state: AgentState, layer: str) -> str:
    names = sorted(occupied_landing_names_from_state(state, layer))
    if not names:
        return ""
    shown = names[:80]
    extra = f" …+{len(names) - 80} more" if len(names) > 80 else ""
    return (
        f"\nExisting {layer} landing tables (do NOT reuse as a new "
        f"`summary_table`): {', '.join(shown)}{extra}. Pick a distinct "
        "grain-encoded name; the compiler suffixes `_vN` on collision."
    )


def plan_dws_bare_names(plan) -> set[str]:
    names: set[str] = set()
    for design in (getattr(plan, "dws_designs", None) or []):
        tbl = str(getattr(design, "summary_table", "") or "")
        if tbl:
            names.add(tbl.rsplit(".", 1)[-1].lower().strip("`"))
    return names


def compile_source_allowlist(state: AgentState, layer: str) -> set[str] | None:
    """Goal closed set of source tables for compiler-mode DWS/ADS.

    None when the goal does not restrict sources. ADS may also read this
    plan's own DWS (layer contract); extra DIM/DWD still must be on the
    goal list (task 94a11f97).
    """
    from wren_agent.compiler.allowlist import parse_goal_source_allowlist

    allow = parse_goal_source_allowlist(
        state.get("modeling_goal") or "", explored_bare_table_names(state),
    )
    if not allow:
        return None
    out = set(allow)
    if str(layer).upper() == "ADS":
        out |= plan_dws_bare_names(state.get("design_plan"))
    return out


def goal_source_allowlist_prompt_block(state: AgentState, layer: str = "DWS") -> str:
    allow = compile_source_allowlist(state, layer)
    if not allow:
        return ""
    names = ", ".join(sorted(allow))
    extra = (
        " ADS may also read this plan's DWS tables listed above."
        if str(layer).upper() == "ADS" else ""
    )
    return (
        "## Goal source allowlist (closed world)\n"
        f"sources / joins.right MUST be these tables only: {names}.{extra} "
        "Do not JOIN any other explored table (task 94a11f97: dim_category "
        "is not allowed when the goal names only dwd_stock_inventory_info + "
        "dim_sku). The compiler will not remap an extra table to an "
        "allowed name."
    )


def _ads_design_from_semantic(sd: SemanticDesign, target: str) -> ADSDesign:
    """Map a compiled semantic ADS design onto ADSDesign for downstream
    consumers (metric registry, lineage, requirement coverage, reports).

    ``fields`` mirrors the legacy contract: ALL physical columns (dimensions,
    descriptive, AND metric columns) — the harness's
    ``metric_fields_materialized`` requires every metric's field_name to be a
    declared field, not just a DDL column.
    """
    from wren_agent.compiler.rules import (
        map_doris_type, render_comment, strip_source_alias,
    )

    fields = [
        {
            "name": c.name,
            "type": map_doris_type(c.dtype),
            "comment": render_comment(c.comment, c.name),
        }
        for c in sd.columns
    ]
    metrics = [
        {
            "name": m.name,
            "field_name": m.name,
            "expression": m.source_expr or m.name,
            "metric_type": m.metric_type or ("derived" if m.parent_metrics else "atomic"),
            "parent_metrics": list(m.parent_metrics),
            "comment": render_comment(m.comment, m.name),
        }
        for m in sd.measure_columns
    ]
    return ADSDesign(
        application_name=sd.summary_name or target,
        ads_table=target,
        description="",
        source_dws_tables=[strip_source_alias(s) for s in sd.sources],
        dimensions=sd.grain,
        fields=fields,
        metrics=metrics,
        requirement_coverage=sd.requirement_coverage,
        target_audience=sd.target_audience,
    )


async def _try_compiler_ads(
    state: AgentState, result: dict,
) -> tuple[bool, list[ADSDesign], list[SQLArtifact]]:
    """Compiler-mode ADS: parse semantic designs, compile, map to plan models.

    Returns (used, ads_designs, sql_artifacts). ``used=False`` when the
    compiler path does not apply (flag off, or no `semantic_designs` key) —
    the caller then runs the legacy parse. Same contract as
    ``_try_compiler_dws``: uncompilable tables keep their semantic design and
    a ``compiler_failures`` record; SQL is not emitted.
    """
    from wren_agent.compiler import landing_db_for_layer
    from wren_agent.compiler.rules import apply_composed_table_name, strip_source_alias
    from wren_agent.config import settings
    from wren_agent.tools.compiler_fn_admit import compile_design_with_admit

    if not settings.llm.compiler_enabled:
        return False, [], []
    semantic = _parse_semantic_result(result, "ADS")
    if semantic is None:
        return False, [], []

    landing = landing_db_for_layer(state, "ADS")
    occupied = await occupied_landing_names(state, "ADS")
    ads_designs: list[ADSDesign] = []
    artifacts: list[SQLArtifact] = []
    for sd in semantic:
        bare = apply_composed_table_name(sd, occupied=occupied)
        occupied.add(bare)
        target = f"{landing}.{bare}" if landing else bare
        compiled, problems = await compile_design_with_admit(
            state, sd, layer_landing_db=landing, table_comment=sd.summary_name,
            source_columns=ads_bind_source_columns(
                state, state.get("design_plan"), sd.sources,
            ),
            allowed_source_tables=compile_source_allowlist(state, "ADS"),
        )
        compiled_semantics = dict(state.get("compiled_semantics") or {})
        compiled_semantics[target] = sd
        state["compiled_semantics"] = compiled_semantics
        ads_designs.append(_ads_design_from_semantic(sd, target))
        if compiled is None:
            _record_compiler_failure(state, "ADS", target, sd, problems)
            continue
        _clear_compiler_failure(state, target)
        artifacts.append(SQLArtifact(
            layer="ADS", target_table=compiled.target_table,
            sql_type="DDL", sql=compiled.ddl,
            dependencies=[strip_source_alias(s) for s in sd.sources],
            description=f"Compiled DDL for {target} (DesignCompiler)",
        ))
        artifacts.append(SQLArtifact(
            layer="ADS", target_table=compiled.target_table,
            sql_type="DML_INSERT", sql=compiled.load_sql,
            dependencies=[strip_source_alias(s) for s in sd.sources],
            description=f"Compiled load for {target} (DesignCompiler)",
        ))
    return True, ads_designs, artifacts


# ── LLM-first design wrappers ──────────────────────────────────────


async def _generate_with_compact_retry(state: AgentState, layer: str) -> dict:
    """Retry LLM generation once with a compact prompt after a failure.

    Replaces the programmatic fallback for the DESIGN layers (DWS/ADS): a
    programmatic "design" is a pseudo-design with no requirement alignment —
    task 4e372c61 produced unrelated floor/store/product tables whose
    requirement_coverage was marked "supported", so they sailed through
    validation. A compact retry (relevant top-12 tables, fewer columns)
    handles the common failure mode (context overflow); failing loudly with
    the real exception beats silently producing garbage.
    """
    from wren_agent.tools.llm_client import get_llm_client

    llm = get_llm_client()
    task = state.get("task_id", "")[:8]
    logger.warning(
        "[%s] %s layer LLM generation failed — retrying once with compact prompt",
        task, layer,
    )
    context = await _build_layer_context_with_injections(
        state, layer, compact=True, extra_suffix=_COMPACT_RETRY_SUFFIX,
    )
    response = await llm.generate_json(
        system_prompt=context["system_prompt"],
        user_message=context["user_message"],
        token_kind="design",
    )
    return _extract_json_from_llm_response(response)


# Large designs used to be generated in ONE call whose 16-table JSON output
# (~40K chars) timed out after ~15 min and burned another ~15 min on the
# compact retry (task 432c17b1: design_dws 28 min, design_ads 15 min — the
# design phase was 96% of the whole task). Plan-then-batch splits the OUTPUT
# into size-limited calls; the DS DAG derives purely from the final artifact
# list, so batching is transparent to deployment.
_DESIGN_BATCH_SIZE = 5
_DESIGN_BATCH_THRESHOLD = 8


async def _plan_layer_tables(state: AgentState, layer: str) -> tuple[list[str], dict]:
    """Small LLM call returning the layer's planned table names (no SQL).

    Returns (names, full_result) — the LLM sometimes ignores the planning
    instruction and returns the FULL design JSON instead (task ab8fadcf:
    complete dws_designs with ddl/load_sql came back as the "plan" and the
    missing `dws_designs` key made the plan parse empty → insufficient
    warehouse). When the response already carries complete designs, `full`
    is returned for direct use.
    """
    from wren_agent.tools.llm_client import get_llm_client

    llm = get_llm_client()
    context = await _build_layer_context_with_injections(
        state, layer, compact=True,
    )
    key = "dws_tables" if layer == "DWS" else "ads_tables"
    context["user_message"] += (
        f"\n\nPLANNING STEP — do NOT generate DDL/SQL/fields yet. "
        f"List the {layer} tables this plan will create (one per requested "
        f"grain; names only). Return ONLY this small JSON plan:\n"
        f'{{"{key}": [{{"table": "dws_...", "grain": ["col1", "col2"]}}]}}'
    )
    # Typed generation: response must be a JSON object with well-formed plan
    # entries (a full-design response is accepted — see LayerPlanOutput).
    # Schema failures regenerate once with the validation error fed back
    # instead of silently parsing to an empty plan (task ab8fadcf: an
    # off-format "plan" was read as zero tables → insufficient_warehouse).
    data = await llm.generate_typed_json(
        context["system_prompt"], context["user_message"],
        LayerPlanOutput, token_kind="design",
    )
    names = data.table_names()
    complete = data.complete_designs_present
    return names, (data.model_dump() if complete else {})


async def _generate_layer_batch(
    state: AgentState, layer: str, batch: list[str], done: list[str],
    retry: bool = False, context: dict[str, str] | None = None,
) -> dict:
    """Generate FULL designs for one batch of tables.

    The already-designed tables (`done`) stay in context so sibling references
    remain coherent. `retry=True` appends the compact-output instruction.
    `context` may be passed from the batch loop so the full layer context
    (exploration dump + field semantics) is serialized ONCE per layer instead
    of once per batch — N batches × full context was O(N²) token cost.
    """
    from wren_agent.tools.llm_client import get_llm_client

    llm = get_llm_client()
    if context is None:
        context = await _build_layer_context_with_injections(
            state, layer, compact=True,
        )
    # Build per-batch message WITHOUT mutating the shared context dict —
    # later batches must not accumulate earlier batches' instructions.
    from wren_agent.config import settings

    if settings.llm.compiler_enabled and layer in ("DWS", "ADS"):
        object_desc = (
            "one SEMANTIC design object per table (columns with name/dtype/"
            "comment/is_key/is_measure/source_expr, sources, joins, filters, "
            "requirement_coverage) — do NOT write ddl/load_sql text"
        )
    else:
        object_desc = (
            "one FULL design object per table (fields, measures, grain, "
            "requirement_coverage, ddl, load_sql)"
        )
    user_message = context["user_message"] + (
        f"\n\nDESIGN ONLY these {layer} tables — {object_desc}, "
        f"complete JSON with the standard schema:\n{batch}\n"
        f"Tables already designed (context only — do NOT repeat them): {done}\n"
        "Never include tables outside this list."
    )
    if retry:
        user_message += _COMPACT_RETRY_SUFFIX
    response = await llm.generate_json(
        context["system_prompt"], user_message, token_kind="design",
    )
    return _extract_json_from_llm_response(response)


async def _generate_layer_designs(state: AgentState, layer: str) -> dict:
    """Design generation with output batching for large layers.

    Plan call first (small), then: ≤ _DESIGN_BATCH_THRESHOLD tables → single
    full call (unchanged behavior); more → batches of _DESIGN_BATCH_SIZE with
    the earlier designs as context and a per-batch compact retry. Returns the
    merged result dict in the parser's expected shape.
    """
    task = state.get("task_id", "")[:8]
    plan_tables, full_result = await _plan_layer_tables(state, layer)
    if full_result:
        logger.info(
            "[%s] %s plan call returned complete designs — using directly",
            task, layer,
        )
        return full_result
    if not plan_tables:
        logger.warning("[%s] %s plan call returned no tables", task, layer)
        return {}
    if len(plan_tables) <= _DESIGN_BATCH_THRESHOLD:
        logger.info("[%s] %s layer: %d tables, single-call design",
                    task, layer, len(plan_tables))
        return await _llm_generate_layer(state, layer)

    logger.info("[%s] %s layer: %d tables, batching by %d",
                task, layer, len(plan_tables), _DESIGN_BATCH_SIZE)
    merged: dict[str, Any] = {"warnings": []}
    done: list[str] = []
    done_l: set[str] = set()
    # Serialize the full layer context ONCE — every batch shares it (the old
    # per-batch rebuild re-dumped explorations + field semantics N times).
    batch_context = await _build_layer_context_with_injections(
        state, layer, compact=True,
    )

    def _tbl(e: dict) -> str:
        return str(e.get("summary_table") or e.get("ads_table")
                   or e.get("fact_table") or "").strip().lower()

    for i in range(0, len(plan_tables), _DESIGN_BATCH_SIZE):
        batch = plan_tables[i:i + _DESIGN_BATCH_SIZE]
        result: dict | None = None
        for attempt in (1, 2):
            try:
                result = await _generate_layer_batch(
                    state, layer, batch, done, retry=(attempt == 2),
                    context=batch_context,
                )
                break
            except Exception as exc:
                if attempt == 2:
                    raise
                logger.warning(
                    "[%s] %s batch %d LLM generation failed — compact retry: %s",
                    task, layer, i // _DESIGN_BATCH_SIZE, str(exc)[:120],
                )
        if result is None:
            continue
        for key in ("dws_designs", "dwd_designs", "designs"):
            entries = result.get(key) or []
            if key in ("dws_designs", "designs"):
                entries = [e for e in entries if _tbl(e) not in done_l]
            merged.setdefault(key, []).extend(entries)
        merged.setdefault("warnings", []).extend(result.get("warnings") or [])
        done.extend(batch)
        done_l.update(b.lower() for b in batch)
    return merged


def _fail_design_layer(state: AgentState, layer: str, exc: Exception) -> AgentState:
    """Fail fast when a design layer cannot be generated at all.

    The task routes to generate_report (graph route_after_design) with the
    real exception instead of a programmatic pseudo-design.
    """
    from wren_agent.models.task import TaskStatus
    from wren_agent.tools.event_emitter import emit_phase

    state["status"] = TaskStatus.FAILED
    state["failure_kind"] = "llm"
    err = (
        f"{layer} 层 LLM 设计生成失败（compact 重试后仍失败）："
        f"{type(exc).__name__}: {str(exc)[:300]}"
    )
    if err not in state.get("errors", []):
        state["errors"] = state.get("errors", []) + [err]
    emit_phase(state, layer.lower())


def _surface_llm_warnings(state: AgentState, layer: str, warnings: list[str]) -> None:
    """Record LLM self-flagged warnings for a layer and, in auto mode,
    live-emit the risky ones (假设/assume/无法/需补充) as WS warning events —
    unattended tasks must not bury risky assumptions in the final report.
    (Revived from the removed `_llm_or_fallback` dead path onto the live
    DWS/ADS warning collection sites.)"""
    for w in warnings:
        state["warnings"] = state.get("warnings", []) + [f"[LLM-{layer}] {w}"]
    if not warnings:
        return
    from wren_agent.state import is_auto_mode
    if not is_auto_mode(state):
        return
    from wren_agent.tools.event_emitter import emit_warning
    risky = [w for w in warnings
             if any(k in w for k in ("假设", "assume", "无法", "需补充"))]
    for w in risky:
        emit_warning(state, f"[auto-risk][{layer}] {w[:300]}")
    return state


# ── Node: design_ods ────────────────────────────────────────────


def _append_approved_drift_fix_artifacts(state: AgentState) -> None:
    """Add approved, sandbox-valid drift DDL to the ODS deployment plan."""
    plan = state.get("design_plan")
    if plan is None:
        return

    artifacts = plan.sql_artifacts or []
    existing_sql = {artifact.sql.strip() for artifact in artifacts}
    for drift in state.get("schema_drifts", []):
        if not (drift.human_approved_fix and drift.auto_fix_sql):
            continue
        sql = drift.auto_fix_sql.strip()
        if sql in existing_sql:
            continue
        artifacts.append(SQLArtifact(
            layer="ODS",
            target_table=f"ods_{drift.table_name}",
            sql_type="DDL",
            sql=drift.auto_fix_sql,
            description=(
                f"Approved schema drift repair for {drift.table_name} "
                f"({drift.drift_type.value})"
            ),
        ))
        existing_sql.add(sql)

    plan.sql_artifacts = artifacts
    state["design_plan"] = plan


async def design_ods(state: AgentState) -> AgentState:
    """Design ODS layer — LLM-first with programmatic fallback.

    Warehouse mode: short-circuited (T-08) — ODS is out of scope; the design
    plan is still initialized so downstream nodes find it.
    """
    from wren_agent.tools.event_emitter import emit_phase

    now = datetime.now(timezone.utc)
    state["updated_at"] = now
    state["phase"] = TaskPhase.DESIGN_ODS

    if (state.get("source_mode") == "warehouse"
            or state.get("business_task_type") == "core_model"):
        if state.get("design_plan") is None:
            state["design_plan"] = DWDesignPlan(
                task_id=state["task_id"],
                modeling_goal=state.get("modeling_goal", ""),
            )
        return state

    # Initialize design plan if not exists
    if state.get("design_plan") is None:
        state["design_plan"] = DWDesignPlan(
            task_id=state["task_id"],
            modeling_goal=state.get("modeling_goal", ""),
        )

    # Whole-DB ODS migration must design every explored table. The LLM
    # consistently emits a handful of designs (task 755c5c60: 5 of 291).
    if is_ods_migration_task(state):
        state = await _design_ods_programmatic(state)
    else:
        # Try LLM generation
        try:
            result = await _llm_generate_layer(state, "ODS")
            ods_designs, datax_configs, flink_configs, sql_artifacts, inc_strategies = _parse_ods_llm_result(result, state)
            _require_llm_sql_artifacts(
                state, "ODS", sql_artifacts, designs=ods_designs,
            )

            plan = state["design_plan"]
            plan.ods_designs = ods_designs
            plan.datax_configs = datax_configs
            plan.flink_cdc_configs = flink_configs
            plan.sql_artifacts = (plan.sql_artifacts or []) + sql_artifacts
            state["incremental_strategies"] = inc_strategies
            state["design_plan"] = plan

            for w in result.get("warnings", []):
                state["warnings"] = state.get("warnings", []) + [f"[LLM-ODS] {w}"]

            logger.info("[%s] ODS layer: LLM generated %d designs",
                        state.get("task_id", "")[:8], len(ods_designs))
        except Exception:
            if not settings.llm.allow_programmatic_fallback:
                raise
            logger.warning(
                "[%s] LLM generation failed for ODS, falling back to programmatic",
                state.get("task_id", "")[:8], exc_info=True,
            )
            state["warnings"] = state.get("warnings", []) + [
                "[ODS] LLM generation failed — used programmatic fallback"
            ]
            state = await _design_ods_programmatic(state)

    _append_approved_drift_fix_artifacts(state)
    await _sandbox_validate(state, "ODS")
    await _llm_review_layer(state, "ODS")
    emit_phase(state, "design_ods")
    return state


async def _design_ods_programmatic(state: AgentState) -> AgentState:
    """[PROGRAMMATIC FALLBACK] Design ODS layer: table structures, sync configs, incremental strategies.

    For each source table:
    1. Map source column types to Doris types
    2. Add _ods_load_time etc. metadata columns
    3. Choose sync method (DataX vs Flink CDC)
    4. Recommend incremental strategy
    5. Generate DataX JSON or Flink CDC config
    """
    now = datetime.now(timezone.utc)
    state["updated_at"] = now
    state["phase"] = TaskPhase.DESIGN_ODS

    explorations = state.get("explorations", {})
    modeling_goal = state["modeling_goal"]
    realtime = _is_realtime_required(modeling_goal)

    # Initialize design plan if not exists
    if state.get("design_plan") is None:
        state["design_plan"] = DWDesignPlan(
            task_id=state["task_id"],
            modeling_goal=modeling_goal,
        )

    plan = state["design_plan"]
    ods_designs: list[ODSDesign] = []
    datax_configs: list[DataXConfig] = []
    flink_configs: list[FlinkCDCConfig] = []
    sql_artifacts: list[SQLArtifact] = []
    incremental_strategies: list[IncrementalStrategy] = []

    from wren_agent.tools.db_explorer import map_type_to_doris

    for db_id, exploration in explorations.items():
        for table in exploration.tables:
            table_name = table.name
            ods_table = f"ods_{table_name}"

            # Map fields to Doris
            doris_fields = []
            for f in table.fields:
                doris_type, unsupported = map_type_to_doris(f.data_type, exploration.source_db_id or "mysql")
                doris_fields.append({
                    "name": f.name,
                    "source_type": f.data_type,
                    "doris_type": doris_type if not unsupported else "UNSUPPORTED",
                    "nullable": f.nullable,
                    "comment": _zh_comment(f.name, f.comment),
                    "is_sensitive": f.is_sensitive,
                })

            # Add metadata columns (Chinese comments — CONTEXT.md hard rule).
            # DEFAULT lets DataX Stream Load omit these columns (task 755c5c60).
            doris_fields.extend(_ods_metadata_fields())

            # Incremental strategy
            strategy = _recommend_incremental(table)
            incremental_strategies.append(strategy)

            # Sync method: fact tables → CDC if realtime, dim candidate → DataX
            row_count = table.row_count_estimate or 0
            is_fact_candidate = row_count > 100_000
            sync_method = "flink_cdc" if (realtime and is_fact_candidate) else "datax"

            # Generate ODS DDL
            # Doris forbids STRING (TEXT/BLOB) in DUPLICATE KEY columns.
            # If the first field — which becomes the sort key — is STRING,
            # downgrade it to VARCHAR(65533) so the DDL passes sandbox validation.
            _fields_for_ddl = []
            for f in doris_fields:
                _fd = dict(f)
                if _fd.get("doris_type") == "STRING":
                    _fd["doris_type"] = "VARCHAR(65533)"
                _fields_for_ddl.append(_fd)

            cols_ddl = ",\n  ".join(
                _format_ods_column_ddl(f) for f in _fields_for_ddl
            )
            ods_ddl = (
                f"CREATE TABLE IF NOT EXISTS {ods_table} (\n  {cols_ddl}\n)\n"
                f"DUPLICATE KEY({_fields_for_ddl[0]['name']})\n"
                f"COMMENT 'ODS 原始数据表: {_esc_comment(table_name)}'\n"
                f"{_ddl_properties()};"
            )

            sql_artifacts.append(SQLArtifact(
                layer="ODS",
                target_table=ods_table,
                sql_type="DDL",
                sql=ods_ddl,
                description=f"ODS table for source table {table_name}",
            ))

            # Sync configs
            datax_cfg = None
            flink_cfg = None

            if sync_method == "datax":
                incremental_where = ""
                if strategy.supports_incremental and strategy.incremental_column:
                    incremental_where = f"WHERE {strategy.incremental_column} > '${{last_run_time}}'"

                datax_cfg = DataXConfig(
                    target_table=ods_table,
                    source_db_type=exploration.explored_at.isoformat() or "mysql",
                    source_query=f"SELECT * FROM {table_name}",
                    doris_table=ods_table,
                    doris_columns=[f["name"] for f in doris_fields],
                    incremental_where=incremental_where,
                    json_config="",  # Will be generated when deploying
                )
                datax_configs.append(datax_cfg)

            elif sync_method == "flink_cdc":
                flink_cfg = FlinkCDCConfig(
                    target_table=ods_table,
                    source_db_type="mysql",
                    source_table=table_name,
                    source_host="",  # Filled at deploy time
                    source_port=3306,
                    source_database="",
                    doris_table=ods_table,
                    startup_mode="initial",
                )
                flink_configs.append(flink_cfg)

            ods_design = ODSDesign(
                source_table=table_name,
                source_db_id=db_id,
                ods_table=ods_table,
                fields=doris_fields,
                sync_method=sync_method,
                datax_config=datax_cfg,
                flink_cdc_config=flink_cfg,
                incremental_strategy=strategy.incremental_type,
                incremental_column=strategy.incremental_column,
                schedule_frequency=strategy.recommended_frequency,
            )
            ods_designs.append(ods_design)

    # Update state
    plan.ods_designs = ods_designs
    plan.datax_configs = datax_configs
    plan.flink_cdc_configs = flink_configs
    plan.sql_artifacts = plan.sql_artifacts + sql_artifacts if plan.sql_artifacts else sql_artifacts
    state["incremental_strategies"] = incremental_strategies
    state["design_plan"] = plan

    await _sandbox_validate(state, "ODS")
    await _llm_review_layer(state, "ODS")

    from wren_agent.tools.event_emitter import emit_phase
    emit_phase(state, "design_ods")

    return state


# ── Node: design_dim ────────────────────────────────────────────


async def design_dim(state: AgentState) -> AgentState:
    """Design DIM layer — LLM-first with programmatic fallback.

    Warehouse mode: short-circuited (T-08) — existing dim tables are the
    input, new ones are out of scope.
    """
    from wren_agent.tools.event_emitter import emit_phase

    now = datetime.now(timezone.utc)
    state["updated_at"] = now
    state["phase"] = TaskPhase.DESIGN_DIM

    if state.get("source_mode") == "warehouse":
        return state

    plan = state.get("design_plan")
    if plan is None:
        return state

    try:
        result = await _llm_generate_layer(state, "DIM")
        dim_designs, sql_artifacts = _parse_dim_llm_result(result)
        new_tables = [
            d.dim_table for d in dim_designs
            if d.source != "from_existing_dim"
        ]
        if new_tables and not _materialization_targets_complete(
            sql_artifacts, "DIM", new_tables,
        ):
            raise ValueError("DIM design is missing load_sql for one or more tables")
        _require_llm_sql_artifacts(
            state, "DIM", sql_artifacts, designs=dim_designs,
        )

        plan.dim_designs = dim_designs
        plan.sql_artifacts = (plan.sql_artifacts or []) + sql_artifacts
        state["design_plan"] = plan

        for w in result.get("warnings", []):
            state["warnings"] = state.get("warnings", []) + [f"[LLM-DIM] {w}"]

        logger.info("[%s] DIM layer: LLM generated %d designs",
                    state.get("task_id", "")[:8], len(dim_designs))
    except Exception:
        if not settings.llm.allow_programmatic_fallback:
            raise
        logger.warning(
            "[%s] LLM generation failed for DIM, falling back to programmatic",
            state.get("task_id", "")[:8], exc_info=True,
        )
        state["warnings"] = state.get("warnings", []) + [
            "[DIM] LLM generation failed — used programmatic fallback"
        ]
        state = await _design_dim_programmatic(state)

    await _sandbox_validate(state, "DIM")
    await _llm_review_layer(state, "DIM")
    emit_phase(state, "design_dim")
    return state


async def _design_dim_programmatic(state: AgentState) -> AgentState:
    """[PROGRAMMATIC FALLBACK] Design DIM layer: check DimensionRegistry, create or reference.

    Identifies dimension tables from source (small lookup tables, reference tables).
    Checks registry for existing dimensions before creating new ones.
    """
    now = datetime.now(timezone.utc)
    state["updated_at"] = now
    state["phase"] = TaskPhase.DESIGN_DIM

    from wren_agent.tools.pg_store import query_dimension_registry

    explorations = state.get("explorations", {})
    plan = state.get("design_plan")
    if plan is None:
        return state

    dim_designs: list[DIMDesign] = []
    sql_artifacts = list(plan.sql_artifacts) if plan.sql_artifacts else []

    for db_id, exploration in explorations.items():
        for table in exploration.tables:
            # Dimension candidates: small tables, mostly descriptive columns
            row_count = table.row_count_estimate or 0
            if row_count > 500_000:
                # Too large for a dimension — likely a fact table
                continue

            # Heuristic: if primary key is a single column and row count < 500K,
            # likely a dimension/lookup table
            if not table.primary_keys or len(table.primary_keys) != 1:
                # Composites could be bridge tables — skip for now
                continue

            dim_name = f"dim_{table.name}"
            dim_table = dim_name
            # Scope the registry check to the task's dim-layer candidate
            # databases (configured/landing + explored real schemas) so a
            # same-named dimension is reused regardless of whether it was
            # registered with the configured or the real (backfilled) db
            # name — while still never reaching across to other sources' dim
            # databases (P2: cross-db reuse was matching nothing, so every
            # DIM was recreated; now it matches the task's own dim dbs).
            from wren_agent.nodes.deployment import layer_relevant_db_names
            dim_dbs = sorted(layer_relevant_db_names(state, {"dim"}))

            # Check registry for existing
            existing = await query_dimension_registry(dim_name, db_names=dim_dbs or None)

            if existing:
                # Reuse existing dimension
                dim_designs.append(DIMDesign(
                    dim_name=dim_name,
                    dim_table=existing[0]["dim_table"],
                    source="from_existing_dim",
                    existing_dim_ref=existing[0]["dim_name"],
                    fields=[],
                    natural_key=existing[0].get("natural_keys", []),
                    version=existing[0]["version"],
                    registered=True,
                ))
            else:
                # Create new dimension
                fields = []
                for f in table.fields:
                    fields.append({
                        "name": f"dim_{f.name}" if f.name in ("name", "id", "code") else f.name,
                        "source_type": f.data_type,
                        "comment": _zh_comment(f.name, f.comment),
                    })

                dim_design = DIMDesign(
                    dim_name=dim_name,
                    dim_table=dim_table,
                    source="from_source_table",
                    source_table=table.name,
                    fields=fields,
                    natural_key=table.primary_keys,
                    surrogate_key=f"{dim_name}_id",
                    version=1,
                    registered=False,  # Will be registered after approval
                )
                dim_designs.append(dim_design)

                # Generate DDL (Chinese comments — CONTEXT.md hard rule)
                cols = [f"{dim_design.surrogate_key} BIGINT NOT NULL COMMENT '代理键'"]
                for fld in fields:
                    cols.append(
                        f"{fld['name']} VARCHAR(255) COMMENT '{_esc_comment(fld.get('comment', ''))}'"
                    )
                cols.append("effective_date DATE COMMENT 'SCD 类型2 生效日期'")
                cols.append("expiration_date DATE COMMENT 'SCD 类型2 失效日期'")
                cols.append("is_current TINYINT DEFAULT 1 COMMENT 'SCD 类型2 当前标志(1=当前,0=已失效)'")

                dim_ddl = (
                    f"CREATE TABLE IF NOT EXISTS {dim_table} (\n  "
                    + ",\n  ".join(cols)
                    + f"\n)\nDUPLICATE KEY({dim_design.surrogate_key})\n"
                    + f"COMMENT '维度表: {_esc_comment(table.name)}'\n"
                    + f"{_ddl_properties()};"
                )

                sql_artifacts.append(SQLArtifact(
                    layer="DIM",
                    target_table=dim_table,
                    sql_type="DDL",
                    sql=dim_ddl,
                    dependencies=[f"ods_{table.name}"],
                ))
                ods_db = str((state.get("landing") or {}).get("ods") or "")
                sql_artifacts.append(SQLArtifact(
                    layer="DIM",
                    target_table=dim_table,
                    sql_type="DML_INSERT",
                    sql=_dim_bootstrap_load_sql(dim_design, ods_db),
                    dependencies=[f"ods_{table.name}"],
                    description=f"Populate {dim_table} from ODS",
                ))

    plan.dim_designs = dim_designs
    plan.sql_artifacts = sql_artifacts
    state["design_plan"] = plan

    await _sandbox_validate(state, "DIM")
    await _llm_review_layer(state, "DIM")

    from wren_agent.tools.event_emitter import emit_phase
    emit_phase(state, "design_dim")

    return state


# ── Node: design_dwd ────────────────────────────────────────────


async def design_dwd(state: AgentState) -> AgentState:
    """Design DWD layer — LLM-first with programmatic fallback.

    Warehouse mode: short-circuited (T-08) — existing dwd/dwm tables are the
    input, new facts are out of scope.
    """
    from wren_agent.tools.event_emitter import emit_phase

    now = datetime.now(timezone.utc)
    state["updated_at"] = now
    state["phase"] = TaskPhase.DESIGN_DWD

    if state.get("source_mode") == "warehouse":
        return state

    plan = state.get("design_plan")
    if plan is None:
        return state

    try:
        result = await _llm_generate_layer(state, "DWD")
        dwd_designs, dws_designs, sql_artifacts = _parse_dwd_dws_llm_result(result)
        if dwd_designs and not _materialization_targets_complete(
            sql_artifacts, "DWD", [d.fact_table for d in dwd_designs],
        ):
            raise ValueError("DWD design is missing load_sql for one or more tables")
        _require_llm_sql_artifacts(
            state, "DWD", sql_artifacts, designs=dwd_designs,
        )

        plan.dwd_designs = dwd_designs
        # NOTE: DWS designs from the DWD LLM call are intentionally discarded.
        # The DWD skill prompt covers both layers for context sharing, but DWS
        # is designed independently in design_dws with full DWD context.
        # Writing DWS here would cause a double-LLM-call race where design_dws
        # overwrites these results with its own generation.
        plan.sql_artifacts = (plan.sql_artifacts or []) + [
            a for a in sql_artifacts if a.layer == "DWD"
        ]
        state["design_plan"] = plan

        for w in result.get("warnings", []):
            state["warnings"] = state.get("warnings", []) + [f"[LLM-DWD] {w}"]

        logger.info("[%s] DWD layer: LLM generated %d designs",
                    state.get("task_id", "")[:8], len(dwd_designs))
    except Exception:
        if not settings.llm.allow_programmatic_fallback:
            raise
        logger.warning(
            "[%s] LLM generation failed for DWD, falling back to programmatic",
            state.get("task_id", "")[:8], exc_info=True,
        )
        state["warnings"] = state.get("warnings", []) + [
            "[DWD] LLM generation failed — used programmatic fallback"
        ]
        state = await _design_dwd_programmatic(state)

    await _sandbox_validate(state, "DWD")
    await _llm_review_layer(state, "DWD")
    emit_phase(state, "design_dwd")
    return state


async def _design_dwd_programmatic(state: AgentState) -> AgentState:
    """[PROGRAMMATIC FALLBACK] Design DWD layer: fact tables from business processes.

    Identifies fact tables (large transaction/event tables) and structures
    them with explicit grain declarations and dimension references.
    """
    now = datetime.now(timezone.utc)
    state["updated_at"] = now
    state["phase"] = TaskPhase.DESIGN_DWD

    explorations = state.get("explorations", {})
    plan = state.get("design_plan")
    if plan is None:
        return state

    dim_tables = [d.dim_table for d in (plan.dim_designs or [])]
    sql_artifacts = list(plan.sql_artifacts) if plan.sql_artifacts else []
    dwd_designs: list[DWDDesign] = []

    for db_id, exploration in explorations.items():
        for table in exploration.tables:
            # Fact candidate: tables with numeric columns (measures) or
            # large row counts. Previously gated at 100K which silently
            # dropped smaller domains (financial, SMB) → empty DWD → DWS → ADS.
            row_count = table.row_count_estimate or 0
            has_genuine_measures = any(
                _is_genuine_measure(f.name, f.data_type or "",
                                    f.name in (table.primary_keys or []))
                for f in table.fields
            )
            # Skip only when there is truly nothing to aggregate —
            # no genuine measures AND negligible row count.
            if not has_genuine_measures and row_count == 0:
                continue

            dwd_table = f"dwd_{table.name}"
            fields = []
            measures = []

            for f in table.fields:
                dtype = (f.data_type or "").upper()
                is_pk = f.name in (table.primary_keys or [])

                if _is_genuine_measure(f.name, dtype, is_pk):
                    measures.append({
                        "name": f.name,
                        "expression": f"SUM({f.name})",
                        "type": "additive" if "INT" in dtype or "DECIMAL" in dtype else "non_additive",
                    })
                else:
                    fields.append({
                        "name": f.name,
                        "source_column": f"{table.name}.{f.name}",
                        "type": dtype,
                        "comment": _zh_comment(f.name, f.comment),
                    })

            # Match to DIM tables
            dim_refs = []
            for dt in dim_tables:
                dim_base = dt.replace("dim_", "")
                if dim_base in table.name or any(
                    dim_base in f.name.lower() for f in table.fields
                ):
                    dim_refs.append(dt)

            dwd_design = DWDDesign(
                fact_name=f"fact_{table.name}",
                fact_table=dwd_table,
                business_process=table.name,
                source_ods_tables=[f"ods_{table.name}"],
                fields=fields,
                measures=measures,
                dimension_refs=dim_refs,
                grain=[pk for pk in (table.primary_keys or [])] or ["_row_"],
            )
            dwd_designs.append(dwd_design)

            # Generate DWD DDL (Chinese comments — CONTEXT.md hard rule)
            grain_str = ", ".join(dwd_design.grain)
            cols_ddl = [
                f"{f['name']} {f['type']} COMMENT '{_esc_comment(f.get('comment', f['name']))}'"
                for f in fields
            ]
            ddl = (
                f"-- 粒度: 每行代表 ({grain_str})\n"
                f"CREATE TABLE IF NOT EXISTS {dwd_table} (\n  "
                + ",\n  ".join(cols_ddl)
                + f"\n)\nDUPLICATE KEY({grain_str.split(',')[0].strip()})\n"
                + f"COMMENT '明细事实层: {_esc_comment(table.name)}'\n"
                + f"{_ddl_properties()};"
            )

            sql_artifacts.append(SQLArtifact(
                layer="DWD",
                target_table=dwd_table,
                sql_type="DDL",
                sql=ddl,
                dependencies=[f"ods_{table.name}"] + dim_refs,
            ))
            ods_db = str((state.get("landing") or {}).get("ods") or "")
            sql_artifacts.append(SQLArtifact(
                layer="DWD",
                target_table=dwd_table,
                sql_type="DML_INSERT",
                sql=_dwd_bootstrap_load_sql(dwd_design, ods_db),
                dependencies=[f"ods_{table.name}"] + dim_refs,
                description=f"Populate {dwd_table} from ODS",
            ))

    plan.dwd_designs = dwd_designs
    plan.sql_artifacts = sql_artifacts
    state["design_plan"] = plan

    await _sandbox_validate(state, "DWD")
    await _llm_review_layer(state, "DWD")

    from wren_agent.tools.event_emitter import emit_phase
    emit_phase(state, "design_dwd")

    return state


# ── Node: design_dws ────────────────────────────────────────────


async def design_dws(state: AgentState) -> AgentState:
    """Design DWS layer — LLM-first, one compact retry, fail fast.

    Warehouse mode (T-08): anchors on EXISTING dwd/dwm/dws tables surfaced by
    exploration; multi-fact combination via `source_tables`. If nothing can be
    composed, an `insufficient_warehouse` clarification is raised (T-05).
    Programmatic fallback is intentionally NOT used for the DWS layer: it
    produces pseudo-designs with no requirement alignment (task 4e372c61).
    """
    from wren_agent.tools.event_emitter import emit_phase

    now = datetime.now(timezone.utc)
    state["updated_at"] = now
    state["phase"] = TaskPhase.DESIGN_DWS

    plan = state.get("design_plan")
    if plan is None:
        return state

    if state.get("source_mode") == "warehouse":
        return await _design_dws_warehouse(state)

    for attempt in (1, 2):
        try:
            if attempt == 1:
                result = await _generate_layer_designs(state, "DWS")
            else:
                result = await _generate_with_compact_retry(state, "DWS")

            # Compiler path (ADR-0015): semantic designs → deterministic SQL.
            used, dws_designs, sql_artifacts = await _try_compiler_dws(state, result)
            if used:
                plan.dws_designs = dws_designs
                plan.sql_artifacts = (plan.sql_artifacts or []) + sql_artifacts
                state["design_plan"] = plan
                _surface_llm_warnings(state, "DWS", result.get("warnings", []))
                logger.info("[%s] DWS layer: compiler rendered %d tables",
                            state.get("task_id", "")[:8], len(dws_designs))
                break

            # Legacy path: LLM writes DDL/load_sql itself.
            _, dws_designs, sql_artifacts = _parse_dwd_dws_llm_result(result)

            if dws_designs and not _materialization_targets_complete(
                sql_artifacts, "DWS", [d.summary_table for d in dws_designs]
            ):
                raise ValueError("DWS design is missing load_sql for one or more tables")
            _require_llm_sql_artifacts(
                state, "DWS", sql_artifacts, designs=dws_designs,
            )

            if dws_designs:
                plan.dws_designs = dws_designs
            plan.sql_artifacts = (plan.sql_artifacts or []) + [
                a for a in sql_artifacts if a.layer == "DWS"
            ]
            state["design_plan"] = plan

            _surface_llm_warnings(state, "DWS", result.get("warnings", []))

            logger.info("[%s] DWS layer: LLM generated %d designs",
                        state.get("task_id", "")[:8], len(dws_designs))
            break
        except Exception as exc:
            if attempt == 2:
                return _fail_design_layer(state, "DWS", exc)

    await _sandbox_validate(state, "DWS")
    await _llm_review_layer(state, "DWS")
    emit_phase(state, "design_dws")
    return state


async def _design_dws_warehouse(state: AgentState) -> AgentState:
    """Warehouse-mode DWS design: consume insufficient answers, then design
    against existing tables; raise `insufficient_warehouse` when nothing is
    composable (T-05/T-08)."""
    from wren_agent.models.task import TaskStatus
    from wren_agent.tools.event_emitter import emit_phase

    state = await _consume_insufficient_answers(state)
    if state.get("status") == TaskStatus.CANCELLED:
        return state

    plan = state.get("design_plan")
    if plan is None:
        return state

    for attempt in (1, 2):
        try:
            if attempt == 1:
                result = await _generate_layer_designs(state, "DWS")
            else:
                result = await _generate_with_compact_retry(state, "DWS")

            # Compiler path (ADR-0015): semantic designs → deterministic SQL.
            used, dws_designs, sql_artifacts = await _try_compiler_dws(state, result)
            if used:
                if not dws_designs:
                    # Semantic designs present but nothing compiled — treat as
                    # empty output so the T-05 escalation below decides.
                    logger.info(
                        "[%s] DWS warehouse: compiler produced no tables — escalating",
                        state.get("task_id", "")[:8],
                    )
                    break
                plan.dws_designs = dws_designs
                plan.sql_artifacts = (plan.sql_artifacts or []) + sql_artifacts
                state["design_plan"] = plan
                _surface_llm_warnings(state, "DWS", result.get("warnings", []))
                logger.info("[%s] DWS warehouse: compiler rendered %d tables",
                            state.get("task_id", "")[:8], len(dws_designs))
                break

            # Legacy path: LLM writes DDL/load_sql itself.
            _, dws_designs, sql_artifacts = _parse_dwd_dws_llm_result(result)

            if dws_designs and not _materialization_targets_complete(
                sql_artifacts, "DWS", [d.summary_table for d in dws_designs]
            ):
                raise ValueError("DWS design is missing load_sql for one or more tables")
            # Designs declared but no SQL artifacts — real inconsistency, retry.
            if dws_designs and not [
                a for a in sql_artifacts if a.layer == "DWS"
            ]:
                raise ValueError("DWS LLM result produced designs but no SQL artifacts")
            # Empty-but-valid output: break and let the post-loop T-05
            # escalation raise insufficient_warehouse (never a programmatic
            # compose that has no requirement alignment).
            if not dws_designs:
                logger.info(
                    "[%s] DWS warehouse: LLM produced no designs — escalating",
                    state.get("task_id", "")[:8],
                )
                break

            plan.dws_designs = dws_designs
            plan.sql_artifacts = (plan.sql_artifacts or []) + [
                a for a in sql_artifacts if a.layer == "DWS"
            ]
            state["design_plan"] = plan

            _surface_llm_warnings(state, "DWS", result.get("warnings", []))

            logger.info("[%s] DWS warehouse: LLM generated %d designs",
                        state.get("task_id", "")[:8], len(dws_designs))
            break
        except Exception as exc:
            if attempt == 2:
                return _fail_design_layer(state, "DWS", exc)

    # Nothing composable despite explored tables → ask the human (T-05)
    if not (plan.dws_designs or []) and state.get("explorations"):
        await _raise_insufficient_warehouse(state, "DWS")

    await _sandbox_validate(state, "DWS")
    await _llm_review_layer(state, "DWS")
    emit_phase(state, "design_dws")
    return state


# ── Node: design_ads ────────────────────────────────────────────


async def _consume_insufficient_answers(state: AgentState) -> AgentState:
    """Apply answered `insufficient_warehouse` clarifications (T-05):

    - 放弃       → task CANCELLED (graph ends via routing)
    - 部分支撑    → gaps recorded into `insufficient_gaps`, continue
    - 调整需求    → modeling_goal replaced by the user's answer, continue
    """
    from wren_agent.models.task import TaskStatus

    clarifications = state.get("clarifications", [])
    remaining = []
    for c in clarifications:
        if c.clarification_type == "insufficient_warehouse" and c.answer:
            answer = (c.answer or "").strip()
            if "放弃" in answer:
                state["status"] = TaskStatus.CANCELLED
                state["warnings"] = state.get("warnings", []) + [
                    "用户选择放弃 — 数仓不足以支撑，任务终止"
                ]
                continue
            if "部分支撑" in answer:
                gaps = state.get("insufficient_gaps", [])
                gaps.append({
                    "type": "insufficient_warehouse",
                    "object": c.question[:200],
                    "detail": c.context[:500],
                })
                state["insufficient_gaps"] = gaps
                state["warnings"] = state.get("warnings", []) + [
                    "用户选择部分支撑 — 跳过缺口继续"
                ]
                continue
            if "调整需求" in answer:
                new_goal = answer.replace("调整需求", "", 1).strip(" :：\n")
                if new_goal:
                    state["modeling_goal"] = new_goal
                    state["warnings"] = state.get("warnings", []) + [
                        f"用户调整需求 — 新目标: {new_goal[:100]}"
                    ]
                continue
        remaining.append(c)
    state["clarifications"] = remaining
    return state


async def _raise_insufficient_warehouse(state: AgentState, layer: str) -> None:
    """Raise an `insufficient_warehouse` blocking clarification (T-05).

    Called when a warehouse-mode design stage produced no composable tables
    despite exploration having surfaced tables. The user picks one of the
    candidates; the answer is consumed on the next design pass.

    Auto mode: a capability-boundary decision must not be auto-answered —
    fail fast with a clear reason instead (observed infinite
    human_approval→dag_deploy→execute loop on task 85b063e9).
    """
    from wren_agent.models.task import ClarificationItem, TaskStatus
    from wren_agent.state import is_auto_mode

    if is_auto_mode(state):
        state["status"] = TaskStatus.FAILED
        state["errors"] = state.get("errors", []) + [
            f"数仓不足以支撑当前建模目标（{layer} 层无可组合产出）— "
            "auto 模式不自动决策能力边界，任务失败"
        ]
        return

    explorations = state.get("explorations", {})
    explored_tables = sum(len(e.tables) for e in explorations.values())
    gaps = state.get("insufficient_gaps", [])

    context_parts = [f"已探索 {explored_tables} 张数仓表，但无法组合出请求的指标。"]
    if gaps:
        context_parts.append("已记录缺口: " + "; ".join(g["object"] for g in gaps))
    context_parts.append("可选：调整需求（重新澄清建模目标）｜部分支撑（跳过缺口继续）｜放弃（任务终止）")

    clarifications = state.get("clarifications", [])
    clarifications.append(ClarificationItem(
        question=f"数仓不足以支撑当前建模目标（{layer} 层无可组合产出），请选择处理方式",
        context="\n".join(context_parts),
        candidates=["调整需求", "部分支撑", "放弃"],
        recommended="部分支撑",
        recommended_reason="能算的指标先产出，缺口记入报告",
        impact_level="high",
        blocking=True,
        clarification_type="insufficient_warehouse",
    ))
    state["clarifications"] = clarifications
    state["pending_human_input"] = True
    state["human_input_type"] = "clarification"


async def design_ads(state: AgentState) -> AgentState:
    """Design ADS layer — LLM-first with programmatic fallback.

    Warehouse mode (T-08): ADS stays chained to the task's NEW DWS designs;
    if DWS was fully skipped (partial support), ADS short-circuits empty and
    the report carries the gaps.
    """
    from wren_agent.tools.event_emitter import emit_phase

    now = datetime.now(timezone.utc)
    state["updated_at"] = now
    state["phase"] = TaskPhase.DESIGN_ADS

    plan = state.get("design_plan")
    if plan is None:
        return state

    if state.get("source_mode") == "warehouse":
        state = await _consume_insufficient_answers(state)
        if state.get("status") == "cancelled":
            return state
        if not (plan.dws_designs or []):
            # Chain broke upstream — nothing to build ADS on (T-08)
            plan.ads_designs = []
            state["design_plan"] = plan
            state["warnings"] = state.get("warnings", []) + [
                "[ADS] warehouse mode: DWS empty (partial support) — ADS skipped"
            ]
            await _sandbox_validate(state, "ADS")
            await _llm_review_layer(state, "ADS")
            emit_phase(state, "design_ads")
            return state

    for attempt in (1, 2):
        try:
            if attempt == 1:
                result = await _generate_layer_designs(state, "ADS")
            else:
                result = await _generate_with_compact_retry(state, "ADS")

            # Compiler path (ADR-0015 Phase 2): semantic designs → deterministic SQL.
            used, ads_designs, sql_artifacts = await _try_compiler_ads(state, result)
            if used:
                if not ads_designs:
                    # nothing compiled — same semantics as the legacy skip
                    state["warnings"] = state.get("warnings", []) + [
                        "[ADS] compiler produced no tables — ADS skipped"
                    ]
                    plan.ads_designs = []
                else:
                    plan.ads_designs = ads_designs
                    plan.sql_artifacts = (plan.sql_artifacts or []) + sql_artifacts
                state["design_plan"] = plan
                _surface_llm_warnings(state, "ADS", result.get("warnings", []))
                logger.info("[%s] ADS layer: compiler rendered %d tables",
                            state.get("task_id", "")[:8], len(ads_designs))
                break

            # Legacy path: LLM writes DDL/load_sql itself.
            ads_designs, sql_artifacts, skipped, warnings = _parse_ads_llm_result(result)

            if ads_designs and not _materialization_targets_complete(
                sql_artifacts, "ADS", [d.ads_table for d in ads_designs]
            ):
                raise ValueError("ADS design is missing load_sql for one or more tables")

            if skipped:
                _surface_llm_warnings(state, "ADS", warnings)
                state["warnings"] = state.get("warnings", []) + [
                    "[ADS] LLM determined no metrics available — skipping ADS table generation"
                ]
                logger.warning("design_ads (LLM): no metrics available, skipping ADS design")
                plan.ads_designs = []
            else:
                _require_llm_sql_artifacts(
                    state, "ADS", sql_artifacts, designs=ads_designs,
                )
                plan.ads_designs = ads_designs
                plan.sql_artifacts = (plan.sql_artifacts or []) + sql_artifacts
                _surface_llm_warnings(state, "ADS", warnings)
                logger.info("[%s] ADS layer: LLM generated %d designs",
                            state.get("task_id", "")[:8], len(ads_designs))

            state["design_plan"] = plan
            break
        except Exception as exc:
            if attempt == 2:
                return _fail_design_layer(state, "ADS", exc)

    await _sandbox_validate(state, "ADS")
    await _llm_review_layer(state, "ADS")
    emit_phase(state, "design_ads")
    return state


# ── Sandbox validation budget ──────────────────────────────────────────
# Maximum number of sandbox validation calls allowed per task.
# Each call executes DDL on a live Doris cluster — without a cap, the
# design_revise → harness_validate → design_revise loop can burn through
# the LangGraph recursion_limit before the MAX_DESIGN_REVISIONS guard trips.
_MAX_SANDBOX_VALIDATIONS_PER_TASK = 30


async def _sandbox_validate(state, layer: str) -> None:
    """Run sandbox SQL validation for the current design layer.

    Normalizes DDL PROPERTIES (replication_num) before validation to prevent
    spurious failures from LLM-generated or programmatic DDLs that exceed
    the available BE count in the target Doris cluster.
    """
    # Budget guard: skip validation if we've done too many rounds already.
    count = state.get("_sandbox_validation_count", 0)
    if count >= _MAX_SANDBOX_VALIDATIONS_PER_TASK:
        logger.warning(
            "[%s] Sandbox validation budget exhausted (%d calls) — "
            "skipping %s validation to preserve step budget",
            state.get("task_id", "")[:8], count, layer,
        )
        return
    state["_sandbox_validation_count"] = count + 1

    # Pre-normalize all SQL artifacts for this layer to match configured replication_num
    plan = state.get("design_plan")
    if plan and plan.sql_artifacts:
        for artifact in plan.sql_artifacts:
            if artifact.layer == layer and artifact.sql_type == "DDL":
                artifact.sql = _normalize_ddl_properties(artifact.sql)
        # Drop `col != ''` / NULLIF(col,'') on BIGINT/DATE columns before the
        # sandbox INSERT-strict probe (task 0af225c9).
        from wren_agent.tools.sql_empty_guards import sanitize_sql_artifacts
        sanitize_sql_artifacts(
            plan.sql_artifacts, state.get("explorations") or {}, layer=layer,
        )

    try:
        from wren_agent.tools.sandbox_sql import validate_node_sql
        await validate_node_sql(state, f"design_{layer.lower()}")
    except Exception:
        logger.warning(
            "Sandbox validation failed for %s (non-blocking)", layer, exc_info=True,
        )


async def _llm_review_layer(state: AgentState, layer: str) -> None:
    """Use LLM to review and enrich the generated design for a layer.

    Only adds suggestions; does not remove anything.
    Gated by ``LLM_LAYER_REVIEW`` (default false) to avoid ~5 serial LLM calls.
    """
    from wren_agent.config import settings

    if not settings.llm.layer_review:
        logger.debug("Skipping LLM review for %s (LLM_LAYER_REVIEW=false)", layer)
        return

    skill_map = {
        "ODS": "design-ods",
        "DIM": "design-dim",
        "DWD": "design-dwd-dws",
        "DWS": "design-dwd-dws",
        "ADS": "design-ads",
    }
    skill_name = skill_map.get(layer)
    if not skill_name:
        return

    plan = state.get("design_plan")
    if plan is None:
        return

    try:
        from wren_agent.tools.llm_client import get_llm_client, build_system_prompt

        design_summary = _build_design_summary(plan, layer)
        if not design_summary:
            # Nothing was designed for this layer — a review request would just
            # burn tokens and return an empty/meaningless suggestion list.
            logger.info("Skipping LLM review for %s: no designs to review", layer)
            return

        llm = get_llm_client()

        system_prompt = build_system_prompt(
            skill_names=[skill_name],
            extra_context=(
                f"You are reviewing a generated {layer} layer design for a data warehouse. "
                "Review the design for completeness, correctness, and adherence to best practices. "
                "Provide specific suggestions for improvement in JSON format."
            ),
        )

        user_message = (
            f"Review this {layer} layer design and suggest up to 3 improvements.\n\n"
            f"Current design:\n{design_summary}\n\n"
            'Return suggestions in JSON. Use severity="error" only for a concrete '
            'skill-rule violation that makes the table incorrect or non-materializable:\n'
            '{"suggestions": [\n'
            '  {"severity": "error|warning|info", "rule_id": "stable_rule_id", '
            '"type": "comment|missing_column|naming|optimization", "detail": "specific suggestion"}\n'
            ']}\n'
            'If the design is adequate, return {"suggestions": []}. Return ONLY JSON.'
        )

        # Typed generation: schema validation + one regeneration with error
        # feedback replaces the regex-extraction of free-form JSON here.
        data = await llm.generate_typed_json(
            system_prompt, user_message, ReviewOutput, token_kind="review",
        )
        suggestions = data.suggestions
        if suggestions:
            normalized = []
            for suggestion in suggestions[:3]:
                if not suggestion.detail:
                    continue
                severity = str(suggestion.severity).lower()
                if severity not in {"error", "warning", "info"}:
                    severity = "warning"
                normalized.append({
                    "layer": layer,
                    "severity": severity,
                    "rule_id": suggestion.rule_id or suggestion.type or "llm_review",
                    "detail": str(suggestion.detail),
                })
            state["design_review_issues"] = (
                state.get("design_review_issues", []) + normalized
            )
            state["warnings"] = state.get("warnings", []) + [
                f"[LLM Review - {layer}][{s['severity'].upper()}] {s['detail']}"
                for s in normalized
            ]

    except Exception:
        # Review is optional and never blocks the pipeline, but stay visible —
        # a silent pass here hid a NameError and real LLM failures for weeks.
        logger.warning("LLM review for %s layer failed", layer, exc_info=True)


def _build_design_summary(plan, layer: str) -> str:
    """Build a text summary of the current design for a layer."""
    lines = []

    if layer == "ODS":
        for ods in (plan.ods_designs or [])[:5]:
            lines.append(f"  {ods.ods_table}: sync={ods.sync_method}, incremental={ods.incremental_strategy}")

    elif layer == "DIM":
        for dim in (plan.dim_designs or [])[:5]:
            lines.append(f"  {dim.dim_table}: source={dim.source}, keys={dim.natural_key}")

    elif layer == "DWD":
        for dwd in (plan.dwd_designs or [])[:5]:
            lines.append(f"  {dwd.fact_table}: grain={dwd.grain}, measures={len(dwd.measures)}, dims={dwd.dimension_refs}")

    elif layer == "DWS":
        for dws in (plan.dws_designs or [])[:5]:
            lines.append(f"  {dws.summary_table}: grain={dws.grain}, source={dws.source_dwd_table}")

    elif layer == "ADS":
        for ads in (plan.ads_designs or [])[:5]:
            metric_names = [m.get("name", "?") if isinstance(m, dict) else str(m) for m in (ads.metrics or [])]
            lines.append(f"  {ads.ads_table}: metrics={metric_names}")

    return "\n".join(lines) if lines else ""
