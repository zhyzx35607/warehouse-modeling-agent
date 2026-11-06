"""Shared LLM-JSON extraction utilities (stdlib only, no heavy deps).

Both ``wren_agent.tools.llm_client`` (typed generation + schema validation)
and ``wren_agent.nodes.design_layers`` (design JSON parsing) use these —
moving them out of design_layers.py keeps the extractor logic in exactly
one place.
"""

from __future__ import annotations

import json as _json
import re as _re


def repair_truncated_json(response: str, missing: int) -> dict | None:
    """Best-effort recovery for lightly truncated JSON (LLM stopped early).

    The gateway-served qwen-plus occasionally stops before closing the JSON
    (task 37a632c6: 45 open vs 42 close braces at ~7k chars — well under any
    token cap, unlike the 16.5k-char responses that succeed). The cut lands
    between list items, so the closing tokens (and their order — objects must
    close before the arrays containing them) are unknown; every short
    `]`/`}` combination is tried. Returns None when the cut lands mid-string
    (e.g. after `"type":`) and cannot be repaired.
    """
    import itertools

    max_len = missing + 6
    for length in range(missing, max_len):
        for combo in itertools.product("}]", repeat=length):
            try:
                return _json.loads(response + "".join(combo))
            except _json.JSONDecodeError:
                continue
    return None


def extract_json_from_llm_response(response: str) -> dict:
    """Extract and parse JSON from LLM response, handling markdown wrapping.

    Includes a completeness check — if the response appears truncated (unbalanced
    braces), it raises early with a clear diagnostic rather than failing silently
    inside json.loads. Lightly truncated responses (missing ≤5 closing braces)
    are first repaired best-effort — a truncation cut after a field list is
    often perfectly parseable once closed.
    """
    # ── Completeness check: balanced braces ──────────────────────────
    # Count braces after stripping strings to avoid false positives from
    # braces inside string values.
    stripped = _re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', '""', response)
    open_braces = stripped.count("{")
    close_braces = stripped.count("}")
    if open_braces != close_braces:
        if open_braces > close_braces and open_braces - close_braces <= 5:
            repaired = repair_truncated_json(response, open_braces - close_braces)
            if repaired is not None:
                return repaired
        raise ValueError(
            f"LLM response appears truncated: "
            f"{open_braces} open vs {close_braces} close braces. "
            f"First 500 chars: {response[:500]}..."
        )

    # Try direct parse first
    try:
        return _json.loads(response)
    except _json.JSONDecodeError:
        pass

    # Try extracting from code blocks
    json_match = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response, _re.DOTALL)
    if json_match:
        try:
            return _json.loads(json_match.group(1))
        except _json.JSONDecodeError:
            pass

    # Try finding any JSON object
    json_match = _re.search(r"\{.*\}", response, _re.DOTALL)
    if json_match:
        try:
            return _json.loads(json_match.group(0))
        except _json.JSONDecodeError:
            pass

    raise ValueError(f"Could not extract valid JSON from LLM response: {response[:500]}...")
