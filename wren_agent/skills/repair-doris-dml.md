---
name: repair-doris-dml
description: Use when Doris execution or EXPLAIN fails with type-cast errors such as can't cast to bool in strict mode, can't cast to decimal, parse number fail, or an unexpected token that appears in the SQL as a string literal next to || && + CONCAT. Use after DAG/SQL execution errors, design_revise of failed INSERT, or when an error token like -Q is not a column name.
---

# Repair Doris DML — try candidates, don't freeze one rewrite

Doris error text is often **misleading**. The quoted token is frequently an **operand**, not a dirty BOOLEAN/DECIMAL column. Do not jump to `WHERE col IN ('true','false')` or `!= ''`.

## Diagnose (mandatory)

1. Copy the token from `detailMessage` (`-Q can't cast to bool` → token `-Q`).
2. Find that token in the failing INSERT/SELECT.
3. Name the **operator touching it** (`||`, `&&`, `+`, `IF`, `CAST`, comparison).
4. Score each candidate's **P(success)** = 0.55×how well it matches this error + 0.45×Laplace history (wins/losses). If Doris EXPLAIN accepts a candidate, boost it; if EXPLAIN still throws the same class of error, drop it. Apply the **highest untried P** this round.

Do not use a fixed CONCAT-first order. After execute, record win/loss for the pending label so later tasks learn.

## Forbidden (task 60a570e6)

- Treating `can't cast to bool` as VARCHAR dirty booleans.
- `WHERE col != '' AND col IN ('true','false')` as the primary fix.
- One hardcoded rewrite for every future error of this class.

## Candidate families (score, don't freeze order)

| Label | When | Patch |
|-------|------|--------|
| `pipe_concat` | Token sits in `expr \|\| 'lit' \|\| expr` | `CONCAT(expr, 'lit', expr)` — Doris `\|\|` is **OR** |
| `concat_ceil_int` | Quarter built with `CEIL(...)` | `CAST(CEIL(...) AS INT)` then concat |
| `concat_cast_varchar` | Mixed types in CONCAT | `CAST(non_literal AS VARCHAR)` |
| `concat_ws` | NULLs in pieces | `CONCAT_WS('', ...)` |
| `pipe_as_or` | `\|\|` between **predicates**, no string literal | `OR` keyword |

Output `p_success` on every candidate. Sort descending. Apply the top untried label.

Other families when the token/operator differs: `CONCAT` vs `+` on strings; `AND` vs `&&`; `NULLIF`/`WHERE != ''` **only** on VARCHAR dates/numbers (`parse number fail`); **remove** `!= ''` on BIGINT/DATE (`cast to decimal`).

## Output (repair JSON)

```json
{
  "token": "-Q",
  "operator_hypothesis": "||",
  "tried": [],
  "candidates": [
    {"label": "pipe_concat", "p_success": 0.81, "why": "|| is OR", "sql": "INSERT ... CONCAT(...) ..."},
    {"label": "concat_ceil_int", "p_success": 0.44, "why": "CEIL is DOUBLE", "sql": "INSERT ... CAST(CEIL(...) AS INT) ..."}
  ]
}
```

Put the **next untried** candidate's SQL into the layer repair `sql` field. At least two candidates. Never emit the forbidden filters as a candidate.

## Example (task 60a570e6)

Error: `-Q can't cast to bool in strict mode`

SQL: `SUBSTRING(a.stat_month, 1, 4) || '-Q' || CEIL(...)`

That is **not** a BOOLEAN column named `-Q`. Rank by P(success). A first-seen `|| '-Q'` usually scores `pipe_concat` highest; if CONCAT already failed in history or EXPLAIN rejects it, try `concat_ceil_int` / `CAST … VARCHAR` next.
