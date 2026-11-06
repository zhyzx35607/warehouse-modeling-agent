"""Offline demo checks actual data movement and catches incorrect output."""
import json
from contextlib import closing
import sqlite3
from pathlib import Path
import subprocess
import sys
import pytest
from warehouse_demo.pipeline import run, validate, plan_model, quote


def test_pipeline_materializes_and_reconciles_split_payments(tmp_path):
    result = run(tmp_path, orders=37, seed=3)
    assert result['status'] == 'passed'
    assert result['row_counts']['dwd_order'] == 37
    assert sum(row['order_count'] for row in result['daily_sales']) == 34
    assert {model['layer'] for model in result['models']} == {'ODS','DIM','DWD','DWS','ADS'}
    assert (tmp_path/'warehouse.sql').exists()
    assert json.loads((tmp_path/'report.json').read_text(encoding='utf-8'))['synthetic'] is True
    with closing(sqlite3.connect(tmp_path/'warehouse.sqlite')) as db, db:
        source = db.execute("SELECT SUM(amount_cents) FROM payments WHERE status='settled'").fetchone()[0]
        target = db.execute('SELECT SUM(paid_cents) FROM ads_daily_sales').fetchone()[0]
        assert target == source and source > 0


def test_quality_fails_after_real_record_tampering(tmp_path):
    result=run(tmp_path, orders=20)
    with closing(sqlite3.connect(tmp_path/'warehouse.sqlite')) as db, db:
        db.execute('UPDATE ads_daily_sales SET paid_cents=paid_cents+1')
        checks=validate(db,result['models'])
    assert not next(c for c in checks if c['name']=='payment_total_reconciles')['passed']


def test_schema_contract_rejects_missing_sources():
    with pytest.raises(ValueError,match='Missing source columns'):plan_model([])


def test_demo_never_overwrites_existing_database(tmp_path):
    run(tmp_path,orders=1)
    with pytest.raises(FileExistsError):run(tmp_path,orders=2)


def test_identifier_validation():
    with pytest.raises(ValueError):quote('orders; DROP TABLE orders')


def test_demo_runs_with_no_site_packages(tmp_path):
    script=Path(__file__).resolve().parents[1]/'scripts'/'run_demo.py'
    result=subprocess.run([sys.executable,'-S',str(script),'--output',str(tmp_path),'--orders','12'],capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
    assert 'PASSED' in result.stdout
