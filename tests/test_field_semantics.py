"""Field semantic dictionary tests (migration 011, A+B).

A-side: grill's rule detector skips fields covered by the dictionary.
B-side: KG export mirrors semantics as SourceField nodes (covered in
neo4j_store via create_modeling_task_subgraph — DB-dependent, not unit-tested).
"""

import pytest

from wren_agent.models.schema import FieldSchema, SchemaExploration, TableSchema
from wren_agent.nodes.explore import _collect_comment_semantics
from wren_agent.nodes.grill import _rule_detect_clarifications


def _table(name: str, fields: list[tuple[str, str]]) -> TableSchema:
    return TableSchema(
        name=name, db_type="doris",
        fields=[FieldSchema(name=f, data_type=t) for f, t in fields],
    )


def _explorations(*tables: TableSchema) -> dict:
    return {"src#dim": SchemaExploration(source_db_id="src#dim", tables=list(tables))}


def _semantics(table: str, *fields: str) -> dict:
    return {
        table: {
            f: {"chinese_meaning": f"{f}的含义", "field_role": "维度属性"}
            for f in fields
        }
    }


def test_rule_detect_skips_known_fields():
    """字段语义字典覆盖的字段不再生成澄清问题。"""
    table = _table("dim_building_area_category", [
        ("status", "INT"),          # numeric + enum-like → 3 question families
        ("area_type", "INT"),       # numeric + enum-like
        ("store_id", "BIGINT"),
        ("source_updated_at", "DATETIME"),  # time
        ("etl_loaded_at", "DATETIME"),
    ])
    exps = _explorations(table)

    # Without semantics: questions are generated
    without = _rule_detect_clarifications(exps, [])
    questions = [c.question for c in without]
    assert any("status" in q for q in questions)
    assert any("source_updated_at" in q for q in questions)
    assert len(without) >= 5

    # With semantics covering all fields: nothing is asked
    covered = _semantics("dim_building_area_category",
                         "status", "area_type", "store_id",
                         "source_updated_at", "etl_loaded_at")
    with_dict = _rule_detect_clarifications(exps, [], covered)
    assert with_dict == []


def test_merge_kg_semantics():
    """KG 兜底合并:未覆盖字段采纳相似表语义,已覆盖字段不被覆盖。"""
    from wren_agent.tools.neo4j_store import merge_kg_semantics

    semantics = {"dws_a": {"known_f": {"chinese_meaning": "已有含义"}}}
    kg_rows = [
        {"table_name": "dws_b", "field_name": "amount_receivable",
         "chinese_meaning": "应收金额", "field_role": "度量指标"},
        {"table_name": "dws_a", "field_name": "known_f",
         "chinese_meaning": "不应覆盖已有"},
    ]
    n = merge_kg_semantics(semantics, kg_rows, ["dws_a", "dws_x"])
    # amount_receivable → dws_a + dws_x; known_f → dws_x (dws_a 已有则不覆盖)
    assert n == 3
    assert semantics["dws_a"]["amount_receivable"]["source"] == "kg_similar"
    assert semantics["dws_a"]["amount_receivable"]["kg_from"] == "dws_b"
    assert semantics["dws_x"]["amount_receivable"]["chinese_meaning"] == "应收金额"
    assert semantics["dws_x"]["known_f"]["chinese_meaning"] == "不应覆盖已有"
    # 已覆盖字段保持原义
    assert semantics["dws_a"]["known_f"]["chinese_meaning"] == "已有含义"
    assert "source" not in semantics["dws_a"]["known_f"]


def test_kg_similar_semantics_skip_rule_detection():
    """KG 兜底得到的语义同样让规则检测跳过(与字典同效)。"""
    table = _table("dws_unknown", [("org_id", "BIGINT"), ("amount_receivable", "DECIMAL(20,2)")])
    exps = _explorations(table)
    kg_tagged = _semantics("dws_unknown", "org_id", "amount_receivable")
    for f in kg_tagged["dws_unknown"].values():
        f["source"] = "kg_similar"
        f["kg_from"] = "dws_b"
    items = _rule_detect_clarifications(exps, [], kg_tagged)
    assert items == []


def test_collect_comment_semantics():
    """Doris 列注释采集:非空且非字段名本身的注释进入字典行。"""
    table = TableSchema(
        name="dws_order_payment_monthly", db_type="doris",
        fields=[
            FieldSchema(name="contract_id", data_type="BIGINT", comment="合同"),
            FieldSchema(name="amount_receivable", data_type="DECIMAL(20,2)", comment="应收金额合计（元）"),
            FieldSchema(name="etl_loaded_at", data_type="DATETIME", comment=""),  # 空注释跳过
            FieldSchema(name="dummy", data_type="VARCHAR(10)", comment="dummy"),  # 注释=字段名跳过
        ],
    )
    exps = {"w#dws": SchemaExploration(source_db_id="w#dws", tables=[table])}
    rows = _collect_comment_semantics(exps)
    assert {r["field_name"] for r in rows} == {"contract_id", "amount_receivable"}
    assert rows[0]["chinese_meaning"] == "合同"


def test_rule_detect_partial_coverage():
    """部分覆盖:已覆盖字段跳过,未覆盖字段仍问。"""
    table = _table("dim_contract_preferential_term", [
        ("contract_id", "BIGINT"),
        ("start_number", "INT"),
        ("start_date", "DATE"),
        ("source_type", "TINYINT"),
    ])
    exps = _explorations(table)
    covered = _semantics("dim_contract_preferential_term", "contract_id", "start_date")
    items = _rule_detect_clarifications(exps, [], covered)
    questions = [c.question for c in items]
    assert not any("contract_id" in q for q in questions)
    assert not any("start_date" in q for q in questions)
    assert any("start_number" in q for q in questions)   # numeric, uncovered
    assert any("source_type" in q for q in questions)    # enum-like, uncovered


# ── Vector semantic fallback (migration 013) ──────────────────


class _FakeConn:
    def __init__(self, row):
        self._row = row

    async def fetchrow(self, *args, **kwargs):
        return self._row


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


@pytest.mark.asyncio
async def test_semantic_fallback_threshold(monkeypatch):
    """向量回退:相似度 ≥ 阈值的命中并带来源标注,低于阈值的不返回。"""
    from wren_agent.tools import pg_store

    hits = {
        # sim 0.85 → accepted; sim 0.50 → below threshold, dropped
        ("fct_a", "amt"): {"table_name": "dws_b", "field_name": "amount",
                           "chinese_meaning": "金额", "field_role": "度量", "sim": 0.85},
        ("fct_a", "xyz"): {"table_name": "dws_c", "field_name": "zzz",
                           "chinese_meaning": "无关", "field_role": "", "sim": 0.50},
    }
    calls = []

    async def fake_embed(texts):
        calls.extend(texts)
        return [[0.1] * 4 for _ in texts]

    monkeypatch.setattr("wren_agent.tools.embedding.embed_texts", fake_embed)

    class Conn(_FakeConn):
        async def fetchrow(self, query, vec):
            # Return the scripted row per call order
            return scripted.pop(0)

    scripted = [hits[("fct_a", "amt")], hits[("fct_a", "xyz")]]
    monkeypatch.setattr(pg_store, "get_pg_pool", lambda: _FakePool(Conn(None)))

    out = await pg_store.semantic_field_semantics_fallback(
        [("fct_a", "amt"), ("fct_a", "xyz")], threshold=0.72,
    )
    assert "amt" in out["fct_a"]
    assert out["fct_a"]["amt"]["chinese_meaning"] == "金额"
    assert out["fct_a"]["amt"]["source"] == "vector_similar"
    assert out["fct_a"]["amt"]["sem_from"] == "dws_b.amount"
    assert "xyz" not in out.get("fct_a", {})  # below threshold
    # Query text is the bare field name (measured best separation)
    assert calls == ["amt", "xyz"]


def test_embedding_text_format():
    """嵌入文本格式:表.字段 + 中文含义 + 角色。"""
    from wren_agent.tools.embedding import embedding_text

    assert embedding_text("t", "f") == "t.f"
    assert embedding_text("t", "f", "金额") == "t.f: 金额"
    assert embedding_text("t", "f", "金额", "度量") == "t.f: 金额 [度量]"
