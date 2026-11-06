"""Grade repair-doris-dml: ranked candidates, no boolean-column dead-end."""
import sys; sys.path.insert(0, __file__.rsplit("/", 2)[0]+"/..")
from common import load_params, report

output, inp = load_params()
errors = []

cands = output.get("candidates", output.get("repairs", []))
if not isinstance(cands, list) or len(cands) < 2:
    errors.append("need at least 2 repair candidates")

blob = str(output).lower()
if "concat" not in blob:
    errors.append("candidates must include CONCAT (Doris || is OR)")
if cands and all(isinstance(c, dict) and "p_success" in c for c in cands):
    ps = [float(c["p_success"]) for c in cands]
    if ps != sorted(ps, reverse=True):
        errors.append("candidates must be sorted by p_success descending")

sql_in = str(inp.get("sql", ""))
if "||" in sql_in:
    token = output.get("token") or output.get("error_token")
    if token and "-Q" in sql_in and str(token).find("Q") < 0 and "-Q" not in str(token):
        errors.append("token should be the error literal -Q, not a column name")

score = max(0, 1.0 - len(errors) * 0.35)
report(score)
