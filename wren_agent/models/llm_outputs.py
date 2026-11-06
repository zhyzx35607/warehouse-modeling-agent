"""Pydantic schemas for LLM-generated JSON outputs.

Every free-text "LLM returns a JSON" call historically parsed with lenient
``.get(key, [])`` — a key-name drift silently became an empty design (the
root of several ``insufficient_warehouse`` false positives and endless
repair rounds). These schemas give each call a typed contract: the caller
uses ``LLMClient.generate_typed_json`` which validates the response and
regenerates once with the validation errors fed back when it does not fit.

Design notes:
- Container types are strict (a list of objects must be a list of objects);
  key ALIASES stay tolerant (``table`` / ``summary_table`` / ``ads_table``)
  because they reflect real, observed LLM variance.
- ``extra="allow"`` on repair outputs preserves unknown keys so legacy
  aliases (e.g. ``ddl`` for ``sql``) survive ``model_dump()`` and the apply
  code keeps working unchanged.
"""

from __future__ import annotations

from typing import Any, Union

from pydantic import BaseModel, ConfigDict, Field


# ── Planner call (`_plan_layer_tables`) ─────────────────────────────


class PlanTableEntry(BaseModel):
    """One planned table entry — either a bare name or a small object.

    The planner asks for ``{"table": ..., "grain": [...]}`` only; the LLM
    sometimes returns full design entries instead (task ab8fadcf) — ``ddl``
    / ``load_sql`` presence marks such an entry as a complete design.
    """

    model_config = ConfigDict(extra="allow")

    table: str = ""
    name: str = ""
    summary_table: str = ""
    ads_table: str = ""
    fact_table: str = ""
    grain: list[str] = Field(default_factory=list)
    # Present only when the LLM returned a FULL design instead of a plan
    ddl: str = ""
    load_sql: str = ""


class LayerPlanOutput(BaseModel):
    """Small planning-step output: table names only (no SQL/fields)."""

    model_config = ConfigDict(extra="ignore")

    dws_tables: list[Union[str, PlanTableEntry]] = Field(default_factory=list)
    ads_tables: list[Union[str, PlanTableEntry]] = Field(default_factory=list)
    designs: list[Union[str, PlanTableEntry]] = Field(default_factory=list)
    tables: list[Union[str, PlanTableEntry]] = Field(default_factory=list)
    # Full designs returned instead of a plan — consumed directly
    dws_designs: list[Union[str, PlanTableEntry]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def complete_designs_present(self) -> bool:
        """True when any entry carries full design content (ddl/load_sql)."""
        for key in ("dws_designs", "designs", "tables"):
            for entry in getattr(self, key, []):
                if isinstance(entry, PlanTableEntry) and (entry.ddl or entry.load_sql):
                    return True
        return False

    def table_names(self) -> list[str]:
        """Flatten all planned table names across the alias keys, in order."""
        names: list[str] = []
        for key in ("dws_tables", "ads_tables", "designs", "tables", "dws_designs"):
            for entry in getattr(self, key, []):
                if isinstance(entry, str):
                    names.append(entry.strip())
                    continue
                name = (
                    entry.table or entry.name or entry.summary_table
                    or entry.ads_table or entry.fact_table
                ).strip()
                if name:
                    names.append(name)
        return [n for n in names if n]


# ── Repair calls (`_llm_repair_artifact` / `_llm_repair_plan_metadata`) ─


class RepairItem(BaseModel):
    """One repair entry — either a SQL fix or a metadata fix.

    ``metadata`` is free-form on purpose: the apply code handles several
    shapes (``metrics[].parent_metrics``, ``fields`` kept list,
    ``requirement_coverage`` claims) — see ``_apply_repair_metadata``.
    """

    model_config = ConfigDict(extra="allow")

    target_table: str = ""
    sql_type: str = ""  # "DDL" | "DML_INSERT" — empty means metadata-only
    sql: str = ""
    metadata: dict[str, Any] | None = None


class RepairOutput(BaseModel):
    """`{"repairs": [...]}` — container validation only, entries tolerant.

    ``repairs`` is REQUIRED (no default): an off-schema response that omits
    the key must fail validation and trigger the regeneration-with-feedback
    path, not silently parse into "no repairs" (the old `data.get("repairs",
    [])` swallow that made ineffective repairs look successful).
    """

    model_config = ConfigDict(extra="allow")

    repairs: list[RepairItem]


class SemanticRepairOutput(BaseModel):
    """Compiler-mode repair response (ADR-0015 Phase 4): the LLM returns the
    REPAIRED semantic designs (structure diff — no SQL text). Entries are
    validated as SemanticDesign by the caller (layer check included).
    """

    model_config = ConfigDict(extra="ignore")

    semantic_designs: list[dict[str, Any]]


# ── Post-design LLM review (`_llm_review_layer`) ───────────────────


class ReviewSuggestion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    severity: str = "warning"  # error | warning | info
    rule_id: str = ""
    type: str = ""
    detail: str = ""


class ReviewOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    suggestions: list[ReviewSuggestion] = Field(default_factory=list)
