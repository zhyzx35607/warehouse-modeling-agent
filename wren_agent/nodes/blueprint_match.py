"""Blueprint matching node — finds reusable patterns from Blueprint Library."""

from __future__ import annotations

from datetime import datetime, timezone

from wren_agent.state import AgentState


async def blueprint_match(state: AgentState) -> AgentState:
    """Match the current task against the Blueprint Library.

    Steps:
    1. Extract source table signatures from task
    2. Search blueprint library by domain/tables/tags
    3. Compute similarity scores
    4. Present top matches (stored in state, human selects later)
    """
    from wren_agent.tools.pg_store import search_blueprints
    from wren_agent.models.blueprint import BlueprintMatch

    now = datetime.now(timezone.utc)
    state["updated_at"] = now

    # Extract table names from source databases
    source_dbs = state.get("source_databases", [])
    table_names: list[str] = []
    for db in source_dbs:
        if hasattr(db, "tables"):
            table_names.extend(db.tables)
        elif isinstance(db, dict):
            table_names.extend(db.get("tables", []))

    # Extract domain hints from modeling goal
    goal = state["modeling_goal"].lower()
    domain_hints = {
        "交易": "电商交易域",
        "订单": "电商交易域",
        "用户": "用户域",
        "会员": "用户域",
        "营销": "营销域",
        "商品": "商品域",
        "物流": "物流域",
        "财务": "财务域",
        "供应链": "供应链域",
    }
    possible_domains = [v for k, v in domain_hints.items() if k in goal]

    # Search blueprints — try domain match first
    matches = []
    if possible_domains:
        for domain in possible_domains[:2]:  # Try top 2 domains
            results = await search_blueprints(domain=domain, limit=3)
            for bp in results:
                score = _compute_similarity(table_names, bp)
                if score > 0:
                    matches.append(BlueprintMatch(
                        blueprint_id=bp["blueprint_id"],
                        blueprint_name=bp["name"],
                        score=score,
                        matched_tables=_find_matched_tables(table_names, bp),
                        unmatched_tables=_find_unmatched_tables(table_names, bp),
                        adaptations_needed=[],
                        **_blueprint_content(bp),
                    ))

    # If no domain matches, try generic search
    if not matches:
        results = await search_blueprints(table_names=table_names, limit=5)
        for bp in results:
            score = _compute_similarity(table_names, bp)
            if score > 0.1:
                matches.append(BlueprintMatch(
                    blueprint_id=bp["blueprint_id"],
                    blueprint_name=bp["name"],
                    score=round(score, 3),
                    matched_tables=_find_matched_tables(table_names, bp),
                    unmatched_tables=_find_unmatched_tables(table_names, bp),
                    adaptations_needed=[],
                    **_blueprint_content(bp),
                ))

    # Sort by score descending, keep top 3
    matches.sort(key=lambda m: m.score, reverse=True)
    state["matched_blueprints"] = matches[:3]

    from wren_agent.tools.event_emitter import emit_phase
    emit_phase(state, "blueprint_match")

    return state


def _parse_jsonb(value):
    """asyncpg may return JSONB as str or already-decoded object."""
    import json
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value or {}


def _blueprint_content(bp: dict) -> dict:
    """Extract design content carried into the BlueprintMatch for prompts."""
    layers = _parse_jsonb(bp.get("layers"))
    return {
        "description": bp.get("description", "") or "",
        "layers": layers if isinstance(layers, list) else [],
    }


def _compute_similarity(
    source_tables: list[str],
    blueprint: dict,
) -> float:
    """Compute similarity between source tables and blueprint signature.

    Uses Jaccard similarity on table name sets.
    """
    bp_sig = blueprint.get("source_tables_signature", {})
    if isinstance(bp_sig, str):
        import json
        bp_sig = json.loads(bp_sig)

    bp_tables = set(bp_sig.get("tables", []) or [])
    source_set = set(t.lower() for t in source_tables) if source_tables else set()

    if not bp_tables or not source_set:
        return 0.0

    bp_lower = set(t.lower() for t in bp_tables)
    intersection = len(source_set & bp_lower)
    union = len(source_set | bp_lower)

    return intersection / union if union > 0 else 0.0


def _find_matched_tables(
    source_tables: list[str],
    blueprint: dict,
) -> list[str]:
    """Find source tables that match blueprint table patterns."""
    bp_sig = blueprint.get("source_tables_signature", {})
    if isinstance(bp_sig, str):
        import json
        bp_sig = json.loads(bp_sig)

    bp_tables = set(t.lower() for t in (bp_sig.get("tables", []) or []))
    source_lower = [t.lower() for t in (source_tables or [])]
    return [t for t in source_lower if t in bp_tables]


def _find_unmatched_tables(
    source_tables: list[str],
    blueprint: dict,
) -> list[str]:
    """Find source tables NOT covered by the blueprint."""
    bp_sig = blueprint.get("source_tables_signature", {})
    if isinstance(bp_sig, str):
        import json
        bp_sig = json.loads(bp_sig)

    bp_tables = set(t.lower() for t in (bp_sig.get("tables", []) or []))
    source_lower = [t.lower() for t in (source_tables or [])]
    return [t for t in source_lower if t not in bp_tables]
