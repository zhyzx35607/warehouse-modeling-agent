"""Grade grill-expert: verify ClarificationItems have question, context, candidates, recommended."""
import sys; sys.path.insert(0, __file__.rsplit("/", 2)[0]+"/..")
from common import load_params, report, check_field

output, inp = load_params()
errors = []

items = output.get("clarifications", output.get("questions", []))
if not isinstance(items, list) or len(items) == 0:
    errors.append("no clarifications/questions found")

for item in items[:5]:
    for f, t in [("question", str), ("context", str)]:
        ok, msg = check_field(item, f, t)
        if not ok: errors.append(msg)
    # candidates should be a list
    if "candidates" not in item:
        errors.append(f"missing candidates in: {item.get('question','?')[:40]}")
    elif not isinstance(item["candidates"], list):
        errors.append(f"candidates not a list in: {item.get('question','?')[:40]}")
    # recommended should be present and, when candidates exist, one of them
    if not str(item.get("recommended", "") or "").strip():
        errors.append(f"missing recommended in: {item.get('question','?')[:40]}")
    elif isinstance(item.get("candidates"), list) and item["candidates"] \
            and item["recommended"] not in item["candidates"]:
        errors.append(f"recommended not in candidates: {item.get('question','?')[:40]}")

score = max(0, 1.0 - len(errors) * 0.2)
report(score)
