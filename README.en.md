# Warehouse Modeling Agent

A personal warehouse modeling workspace with the existing LangGraph workflow, semantic SQL compiler, database adapters, validation, evaluation and Vue dashboard preserved.

The explicit offline demo uses only the Python 3.11+ standard library:

```sh
python -m warehouse_demo --orders 120 --seed 7
```

It generates synthetic ecommerce data, introspects actual SQLite schemas, materializes ten ODS/DIM/DWD/DWS/ADS models, reconciles quantities and payment totals, and writes SQL, models, quality results, table lineage and an HTML report. The planner is deterministic; it does not claim to call an LLM. Existing databases are never overwritten.

Build the Vue dashboard with `cd dashboard && npm ci && npm run build`, then run `python -m warehouse_demo --serve` from the repository root. Open `http://127.0.0.1:8000/demo`.

Live mode is separate: install `vendor/cli-anything-dolphinscheduler`, then `pip install -e ".[live,dev]"`, configure your own `.env` from `.env.example`, and run `python run.py`. Live dependencies include PostgreSQL and user-configured model/database/scheduler services. `/live` opens the full dashboard. Missing model credentials fail startup; provider failures do not silently switch into the offline demo. Embedding configuration is independent of chat configuration.

Run `python scripts/verify_demo.py`, `python -m pytest tests -q`, and the dashboard build to verify. See the Chinese README for details and `NOTICE.md` for source provenance and license scope.

## v0.2 local data workspace

Import five UTF-8 ecommerce CSV files with `python -m warehouse_demo --input-dir examples/ecommerce-csv`, or use the browser. The workspace supports persistent run history, bounded read-only table previews, source fingerprints, run comparison, SQL-backed quality diagnostics, stage timing, snapshot revalidation and artifact ZIP exports. The fixed ecommerce contract is explicit; this is not arbitrary-schema LLM modeling. Run `python -S scripts/verify_lab.py` and `python -S -m warehouse_demo.evaluate` for the HTTP workflow and 12 authored fault-injection cases.
