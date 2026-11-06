"""DesignCompiler — deterministic DDL/DML generation from semantic designs.

ADR-0015 Phase 1: the design LLM outputs semantic structure (columns, SELECT
expressions, sources); this package renders the Doris DDL and load SQL with
unit-tested templates, making the ``fields ⊆ DDL ⊆ INSERT`` consistency
invariants hold by construction.
"""

from wren_agent.compiler.render import (
    compile_semantic_design,
    landing_db_for_layer,
    try_compile_semantic_design,
)
from wren_agent.compiler.rules import (
    apply_composed_table_name,
    compose_layer_table_name,
    map_doris_type,
    normalize_table_name,
    uniquify_table_name,
)

__all__ = [
    "compile_semantic_design",
    "try_compile_semantic_design",
    "landing_db_for_layer",
    "map_doris_type",
    "normalize_table_name",
    "compose_layer_table_name",
    "apply_composed_table_name",
    "uniquify_table_name",
]
