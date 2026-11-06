"""Grade schema-drift: verify breaking vs additive classification."""
import sys; sys.path.insert(0, __file__.rsplit("/", 2)[0]+"/..")
from common import load_params, report

output, inp = load_params()
errors = []

drifts = output.get("schema_drifts", output.get("drifts", output.get("changes", [])))
if not isinstance(drifts, list) or len(drifts) == 0:
    errors.append("no schema drifts found")

for d in drifts[:5]:
    if not isinstance(d, dict):
        continue
    for f in ["table_name", "drift_type"]:
        if f not in d:
            errors.append(f"missing {f} in drift result")
    drift = d.get("drift_type", d.get("type", ""))
    if drift:
        valid = ["column_removed", "column_added", "column_type_changed",
                 "column_renamed", "table_removed", "table_renamed", "no_change"]
        if drift not in valid:
            errors.append(f"invalid drift_type: {drift}")
    # is_breaking should be present for type-based classification
    if "is_breaking" not in d and "breaking" not in d:
        errors.append(f"missing is_breaking in drift for {d.get('table_name','?')}")

score = max(0, 1.0 - len(errors) * 0.2)
report(score)
