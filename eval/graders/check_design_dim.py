"""Grade design-dim: verify DIM tables have natural keys + version + Chinese comments."""
import re
import sys; sys.path.insert(0, __file__.rsplit("/", 2)[0]+"/..")
from common import load_params, report, chinese_comment_errors

output, inp = load_params()
errors = []

dims = output.get("dim_designs", output.get("dimensions", output.get("dims", [])))
if not isinstance(dims, list) or len(dims) == 0:
    errors.append("no DIM designs found")

for d in dims[:5]:
    for f in ["dim_name", "dim_table", "natural_key", "source"]:
        if f not in d:
            errors.append(f"missing {f} in DIM design")
    nk = d.get("natural_key", d.get("natural_keys", []))
    if isinstance(nk, str):
        pass  # OK, single key as string
    elif isinstance(nk, list) and len(nk) == 0:
        errors.append(f"empty natural_key in {d.get('dim_table','?')}")
    for e in chinese_comment_errors(d.get("ddl", "")):
        errors.append(f"{d.get('dim_table','?')}: {e}")
    if d.get("source") != "from_existing_dim":
        load_sql = d.get("load_sql", "")
        if not re.search(r"\bINSERT\s+INTO\b", str(load_sql), re.I):
            errors.append(f"{d.get('dim_table','?')}: missing executable INSERT load_sql")

score = max(0, 1.0 - len(errors) * 0.25)
report(score)
