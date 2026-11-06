"""Grade design-ods: verify ODS tables have sync method + columns + Chinese comments."""
import sys; sys.path.insert(0, __file__.rsplit("/", 2)[0]+"/..")
from common import load_params, report, chinese_comment_errors

output, inp = load_params()
errors = []

ods = output.get("ods_designs", output.get("ods_tables", output.get("tables", [])))
if not isinstance(ods, list) or len(ods) == 0:
    errors.append("no ODS designs found")

for d in ods[:5]:
    if not isinstance(d, dict):
        errors.append(f"ods entry not dict: {d}")
        continue
    for f in ["ods_table", "source_table", "sync_method"]:
        if f not in d:
            errors.append(f"missing {f} in ODS design")
    sync = d.get("sync_method", "")
    if sync not in ("datax", "flink_cdc", "full", "incremental", ""):
        errors.append(f"invalid sync_method: {sync}")
    for e in chinese_comment_errors(d.get("ddl", "")):
        errors.append(f"{d.get('ods_table','?')}: {e}")
    ddl = d.get("ddl") or ""
    if "_ods_load_time" in ddl and "DEFAULT CURRENT_TIMESTAMP" not in ddl.upper():
        errors.append(
            f"{d.get('ods_table','?')}: _ods_load_time missing DEFAULT CURRENT_TIMESTAMP"
        )

score = max(0, 1.0 - len(errors) * 0.25)
report(score)
