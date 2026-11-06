"""Grade explore-schema: verify output contains tables with columns + types."""
import sys; sys.path.insert(0, __file__.rsplit("/", 2)[0]+"/..")
from common import load_params, report, check_field

output, inp = load_params()
errors = []

# Must have a tables key or be a list
if "tables" in output:
    tbls = output["tables"]
elif isinstance(output, list):
    tbls = output
else:
    errors.append("missing 'tables' field")

if isinstance(tbls, list) and len(tbls) > 0:
    for t in tbls:
        if not isinstance(t, dict):
            errors.append(f"table is not dict: {t}")
            continue
        for fld in ["name", "fields"]:
            ok, msg = check_field(t, fld)
            if not ok:
                errors.append(msg)
        # Check fields have data_type
        for f in t.get("fields", []):
            if isinstance(f, dict) and "data_type" not in f:
                errors.append(f"field {f.get('name','?')} missing data_type")
else:
    errors.append("no tables found")

score = max(0, 1.0 - len(errors) * 0.25)
report(score)
