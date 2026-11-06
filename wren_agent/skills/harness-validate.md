---
name: harness-validate
description: Three-level Harness validation for each DW layer design. Level 1: structure validation (auto). Level 2: data consistency validation (auto). Level 3: semantic validation (agent generates assertions, human reviews, agent executes).
---

# Harness Validation

Run three levels of validation for each layer (ODS → DIM → DWD → DWS → ADS).

## Level 1: Structure Validation (Fully Automatic)

Execute DDL and basic queries against the target Doris to verify:
1. DDL syntax is valid
2. All referenced tables exist
3. All referenced fields exist in their tables
4. JOIN conditions reference valid columns
5. Data types are compatible in expressions

**Failure response**: Stop. Fix the SQL. Re-execute validation.

## Level 2: Data Consistency Validation (Fully Automatic)

Compare data between layers to verify logic:
1. **Row count**: ODS row count == source table row count (for full sync)
2. **DWD dedup**: DWD row count <= ODS row count (after dedup)
3. **DWS aggregation**: SUM(DWS measure) == SUM(DWD measure) for the same filters
4. **ADS derivation**: ADS metric == formula(DWS measures)

**Failure response**: Flag the specific inconsistency. Agent auto-retries with corrected SQL.

## Level 3: Semantic Validation (Semi-Automatic)

1. **Agent generates assertions** based on knowledge collected during Grill phase:
   - "月活用户数应该约等于 X" (from human's confirmation)
   - "GMV 应该大于退货金额" (business invariant)
   - "城市数量应该等于 N" (from schema exploration)

2. **Human reviews assertions** before execution
   - Are the assertions correct?
   - Are the expected values reasonable?
   - Any missing assertions?

3. **Agent executes assertions** against the DW and reports results
   - PASS: value matches expectation within tolerance
   - FAIL: value differs — include both expected and actual for human review

**Failure response**: Report to human with expected vs actual values. Do not auto-fix semantic issues.

## Validation Report

For each layer, produce:
- Pass/fail status per level
- List of passed checks
- List of failures with details
- For semantic: assertion list with expected vs actual

**All Level 1 and Level 2 checks must pass before proceeding. Level 3 failures block only if `critical`.**
