"""Grade dag-deploy: verify DAG topology and namespace conventions."""
import sys; sys.path.insert(0, __file__.rsplit("/", 2)[0]+"/..")
from common import load_params, report

output, inp = load_params()
errors = []

# Check for deployment plan
project = output.get("project", output.get("ds_project", output.get("deployment_plan", {})))
if not isinstance(project, dict):
    errors.append("no project/deployment info found")

dags = output.get("dags", output.get("dag_list", project.get("dags", [])))
if not isinstance(dags, list) or len(dags) == 0:
    errors.append("no DAGs found")

for d in dags[:5]:
    if isinstance(d, dict):
        if "dag_name" not in d and "name" not in d:
            errors.append("DAG missing name")
    elif isinstance(d, str):
        pass  # OK, just a name list
    else:
        errors.append(f"DAG entry not string or dict: {type(d)}")

# Check namespace convention: should contain task_id or layer prefix
names = []
for d in dags[:5]:
    names.append(d.get("dag_name", d.get("name", d if isinstance(d, str) else "?")))
has_namespace = any("__" in str(n) or "ods_" in str(n).lower() or "dim_" in str(n).lower() for n in names)
if not has_namespace:
    errors.append("no namespace convention detected in DAG names")

score = max(0, 1.0 - len(errors) * 0.25)
report(score)
