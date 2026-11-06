---
name: grill-expert
description: Generate structured ClarificationItems to extract domain knowledge from human experts. Cross-examine unclear metrics, table relationships, and business logic discovered during schema exploration.
---

# Grill Expert

Extract missing domain knowledge from human experts. Be relentless — do not assume, always confirm.

## When to Grill

Ask humans when:
1. **Metric definitions are ambiguous**: The same column name in different tables may represent different concepts, or a metric's calculation rule is unclear from data alone.
2. **Table relationships are uncertain**: Schema exploration found FK constraints, but the actual business meaning of the relationship needs confirmation.
3. **Business logic is opaque**: Data values imply rules (like "status=3 means cancelled") that need explicit human confirmation.
4. **Sensitive fields detected**: Each detected sensitive field needs a mask strategy decision from a human.
5. **Incompatible data types**: Fields with no Doris equivalent need a human to decide how to handle them.

## How to Grill

For each unknown, generate a `ClarificationItem` with:
- `question`: Clear, specific question (not open-ended)
- `context`: Why this matters and what was discovered from data
- `candidates`: 2-4 suggested answers (help the human choose quickly)
- `recommended`: Your single best-guess answer. MUST be copied verbatim from `candidates` when candidates exist; free text otherwise. Never leave empty.
- `recommended_reason`: One sentence explaining why, grounded in the data evidence.
- `impact_level`: low | medium | high | critical (based on downstream impact)
- `blocking`: True if this question must be answered before design can continue

## Rules

1. **Data facts override human claims**. If data exploration shows a 1:N relationship but the human claims 1:1, flag the conflict and ask for resolution — do not silently accept either side.
2. **Non-blocking items allow the Agent to proceed**. Unanswered low-impact questions don't block the pipeline.
3. **Batch questions but track individually**. Present related questions together, but each remains independently answerable and trackable.
4. **Refer to KG context**. Before asking, check if the Knowledge Graph already has the answer from a previous task.
5. **Always recommend**. Every question ships with a `recommended` answer so the pipeline can run unattended (auto mode); the human overrides it in manual mode.

## Output Contract

Return ONLY valid JSON (no markdown, no prose) in exactly this shape.
Limit to the 8 most important questions.

```json
{"clarifications": [
  {
    "question": "specific question",
    "context": "why this matters",
    "candidates": ["option1", "option2", "option3"],
    "recommended": "your single best-guess answer — copy verbatim from candidates when candidates exist",
    "recommended_reason": "one-line justification grounded in the schema evidence",
    "impact_level": "low|medium|high|critical",
    "blocking": true,
    "clarification_type": "metric_definition|table_relation|business_logic|data_quality"
  }
]}
```

## Example

Input evidence: table `orders` has `status TINYINT` with distinct values {0,1,2,9}; table `order_item` has `amount DECIMAL(10,2)`.

Output:

```json
{"clarifications": [
  {
    "question": "orders.status 的取值 0/1/2/9 分别代表什么业务状态？",
    "context": "探索发现 status 有 4 个取值但无字典表，成交口径依赖状态过滤条件",
    "candidates": ["0=待支付,1=已支付,2=已发货,9=已取消", "0=已取消,1=待支付,2=已支付,9=退款", "其他（请补充）"],
    "recommended": "0=待支付,1=已支付,2=已发货,9=已取消",
    "recommended_reason": "9 出现频率最低且与取消单量分布吻合，符合常见电商状态机约定",
    "impact_level": "critical",
    "blocking": true,
    "clarification_type": "metric_definition"
  }
]}
```

## Warehouse Mode (source_mode="warehouse")

- The context already includes registered dimensions/metrics (dimension_registry / metric_registry) when they exist — do NOT re-clarify established terms.
- If the requested metrics cannot be composed from the explored warehouse tables, raise an `insufficient_warehouse` clarification (type `insufficient_warehouse`) with candidates `["调整需求", "部分支撑", "放弃"]` instead of inventing upstream tables.
- Gaps are classified as: 缺表 (no relevant table), 缺列 (table exists but field missing), 粒度不匹配 (existing grain cannot support the requested grain).
- **Empty date columns**: a DATE/period column with `null_rate=100% — EMPTY` is not a usable time grain (core modeling copied an empty ODS column). Do NOT ask "how to extract month from inventory_date" for such a column. Ask which **populated** date column to use (`create_time` / `update_time` listed in the exploration summary) with candidates naming those columns.
