"""Grade design-ads: verify ADS has metrics with expressions + Chinese comments."""
import sys; sys.path.insert(0, __file__.rsplit("/", 2)[0]+"/..")
from common import load_params, report, chinese_comment_errors

output, inp = load_params()
errors = []

ads = output.get(
    "designs",
    output.get("ads_designs", output.get("applications", output.get("ads", []))),
)
for d in ads[:5]:
    for f in ["application_name", "ads_table", "metrics"]:
        if f not in d:
            errors.append(f"missing {f} in ADS design")
    metrics = d.get("metrics", [])
    if not d.get("dimensions"):
        errors.append(f"{d.get('ads_table','?')}: missing physical query dimensions")
    if not str(d.get("load_sql", "")).lstrip().upper().startswith("INSERT INTO"):
        errors.append(f"{d.get('ads_table','?')}: missing executable INSERT load_sql")
    if not isinstance(metrics, list) or len(metrics) == 0:
        errors.append(f"empty metrics in {d.get('ads_table','?')}")
    for m in metrics[:3]:
        if isinstance(m, dict):
            if "name" not in m:
                errors.append("metric missing name")
            if "expression" not in m:
                errors.append("metric missing expression")
            if "field_name" not in m:
                errors.append("metric missing physical field_name")
            metric_type = m.get("metric_type")
            if metric_type not in ("atomic", "derived"):
                errors.append("metric missing valid metric_type")
            if metric_type == "derived" and not m.get("parent_metrics"):
                errors.append("derived metric missing parent_metrics")
    field_names = {
        str(field.get("name", "")).lower()
        for field in d.get("fields", []) if isinstance(field, dict)
    }
    for dimension in d.get("dimensions", []):
        if str(dimension).lower() not in field_names:
            errors.append(f"{d.get('ads_table','?')}: dimension {dimension} missing from fields")
    coverage = d.get("requirement_coverage", [])
    if not coverage:
        errors.append(f"{d.get('ads_table','?')}: missing requirement_coverage")
    for item in coverage:
        if item.get("status") != "supported":
            errors.append(
                f"{d.get('ads_table','?')}: requirement is not fully supported: "
                f"{item.get('requirement','?')}"
            )
        if not item.get("evidence"):
            errors.append(f"{d.get('ads_table','?')}: supported requirement has no evidence")
    for e in chinese_comment_errors(d.get("ddl", "")):
        errors.append(f"{d.get('ads_table','?')}: {e}")

required = inp.get("required_requirements", [])
coverage_text = " ".join(
    str(item.get("requirement", ""))
    for design in ads
    for item in design.get("requirement_coverage", [])
).lower()
for requirement in required:
    if str(requirement).lower() not in coverage_text:
        errors.append(f"requested requirement missing from coverage: {requirement}")

score = max(0, 1.0 - len(errors) * 0.25)
report(score)
