"""Semantic design models — the compiler-architecture contract (ADR-0015).

In compiler mode the design LLM outputs SEMANTIC structure only (columns,
their SELECT expressions, sources, filters) — never DDL/load_sql text. The
DesignCompiler renders both SQL statements deterministically from this model,
so the ``fields ⊆ DDL ⊆ INSERT`` consistency invariants hold by construction
instead of being patched after the fact.

Legacy mode keeps the old output shape (``dws_designs`` with ``ddl`` /
``load_sql`` text) — see ``settings.llm.compiler_enabled``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class SemanticColumn(BaseModel):
    """One physical column of a materialized table.

    ``source_expr`` is the SELECT-list expression that produces the column in
    the load SQL (e.g. ``SUM(amount)``, ``DATE(trans_dt)``, ``MAX(company_name)``,
    or a plain ``column``). The compiler validates it against a Doris-safe
    expression whitelist before rendering.
    """

    name: str
    dtype: str  # Doris type — compiler normalizes (STRING→VARCHAR(65533) etc.)
    comment: str = ""
    is_key: bool = False  # grain member → UNIQUE KEY column, must lead the schema
    is_measure: bool = False  # business measure (SUM/COUNT/AVG) vs descriptive (MAX(name))
    source_expr: str = ""  # SELECT expression; empty = same as name
    # ADS layer only: metric lineage (ADR-0015 Phase 2). Derived metrics
    # reference base metrics by physical column name via parent_metrics.
    metric_type: str = ""  # "atomic" | "derived"
    parent_metrics: list[str] = Field(default_factory=list)


class SemanticDesign(BaseModel):
    """Semantic design of ONE materialized table (DWS/ADS)."""

    layer: Literal["DWS", "ADS"] = "DWS"
    summary_name: str = ""
    summary_table: str  # bare or db-qualified name; compiler normalizes
    columns: list[SemanticColumn]
    sources: list[str] = Field(default_factory=list)  # upstream tables (db-qualified ok)
    joins: list[dict] = Field(default_factory=list)  # {right, on} — multi-source only
    filters: list[str] = Field(default_factory=list)  # WHERE conditions (AND-joined)
    requirement_coverage: list[dict] = Field(default_factory=list)
    refresh_logic: str = "incremental"
    # ADS layer only
    target_audience: str = ""

    @property
    def grain(self) -> list[str]:
        """Key columns in declaration order (is_key=True)."""
        return [c.name for c in self.columns if c.is_key]

    @property
    def measure_columns(self) -> list[SemanticColumn]:
        """Columns explicitly declared as business measures (is_measure)."""
        return [c for c in self.columns if c.is_measure]

    @model_validator(mode="after")
    def _grain_columns_lead(self):
        """The grain must be an ordered prefix of the column list — this is a
        Doris hard requirement (KEY columns first), enforced here so the DDL
        template can never render an invalid key arrangement."""
        grain = [c.name for c in self.columns if c.is_key]
        leading = [c.name for c in self.columns[: len(grain)]]
        if leading != grain:
            raise ValueError(
                f"grain columns {grain} must be the first {len(grain)} columns "
                f"in declaration order; got leading columns {leading}"
            )
        return self


class CompiledSQL(BaseModel):
    """Deterministic compile output for one table."""

    layer: str
    target_table: str  # normalized (landing-db qualified where applicable)
    ddl: str
    load_sql: str
    warnings: list[str] = Field(default_factory=list)
