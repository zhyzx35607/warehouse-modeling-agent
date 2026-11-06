"""Grade harness-validate: verify 3-level validation results."""
import sys; sys.path.insert(0, __file__.rsplit("/", 2)[0]+"/..")
from common import load_params, report

output, inp = load_params()
errors = []

results = output.get("validation_results", output.get("validations", output.get("results", [])))
if not isinstance(results, list) or len(results) == 0:
    errors.append("no validation results found")

levels_seen = set()
for r in results:
    if not isinstance(r, dict):
        continue
    level = r.get("level", r.get("type", ""))
    if level:
        levels_seen.add(level)
    for f in ["layer", "passed"]:
        if f not in r:
            errors.append(f"missing {f} in validation result")

if len(levels_seen) < 2:
    errors.append(f"expected 3 levels (structure/consistency/semantic), got {len(levels_seen)}: {levels_seen}")

score = max(0, 1.0 - len(errors) * 0.2)
report(score)
