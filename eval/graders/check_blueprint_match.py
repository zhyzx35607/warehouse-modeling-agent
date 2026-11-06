"""Grade blueprint-match: verify similarity scores + matched tables."""
import sys; sys.path.insert(0, __file__.rsplit("/", 2)[0]+"/..")
from common import load_params, report

output, inp = load_params()
errors = []

matches = output.get("matched_blueprints", output.get("blueprints", output.get("matches", [])))
if not isinstance(matches, list):
    errors.append("no matched_blueprints found")

for m in matches[:5]:
    if not isinstance(m, dict):
        errors.append(f"match entry not dict")
        continue
    for f in ["blueprint_name", "score"]:
        if f not in m:
            errors.append(f"missing {f} in match")
    score_val = m.get("score", -1)
    if not isinstance(score_val, (int, float)) or score_val < 0 or score_val > 1:
        errors.append(f"invalid score: {score_val}")

    # Should have matched tables
    if "matched_tables" not in m and "tables" not in m:
        errors.append(f"missing matched_tables in {m.get('blueprint_name','?')}")

score = max(0, 1.0 - len(errors) * 0.25)
report(score)
