from contextlib import closing
import io
import json
import sqlite3
import zipfile
import pytest

from warehouse_demo.pipeline import run
from warehouse_demo.sources import parse_sources, read_directory, sample_sources
from warehouse_demo import history
from warehouse_demo.evaluate import evaluate
from warehouse_demo.serialization import browser_safe


def make_run(root, name='first', tables=None):
    return run(root/'.demo-data'/name, source_tables=tables if tables is not None else sample_sources())


def test_csv_import_executes_real_values_and_keeps_trace(tmp_path):
    report = make_run(tmp_path)
    assert report['synthetic'] is False and report['seed'] is None
    assert report['source_orders'] == 2 and report['status'] == 'passed'
    assert report['daily_sales'][0]['paid_cents'] == 10000
    assert len(report['source_manifest']) == 5
    assert [t['stage'] for t in report['trace']] == ['source','explore','plan','materialize','validate','diagnose']
    assert all(t['elapsed_ms'] >= 0 for t in report['trace'])
    assert history.revalidate(tmp_path, 'first')['snapshot_unchanged'] is True


@pytest.mark.parametrize('kind', ['missing_table','extra_table','headers','duplicate','orphan','date','status','integer','large','empty','malformed'])
def test_invalid_csv_rejected_before_output(tmp_path, kind):
    tables = sample_sources()
    if kind == 'missing_table': del tables['products']
    elif kind == 'extra_table': tables['../../evil'] = 'x'
    elif kind == 'headers': tables['orders'] = tables['orders'].replace('order_id,', 'id,')
    elif kind == 'duplicate': tables['customers'] += '1,Other\n'
    elif kind == 'orphan': tables['orders'] = tables['orders'].replace('1,1,2026', '1,900,2026')
    elif kind == 'date': tables['orders'] = tables['orders'].replace('2026-01-01', '2026-02-30')
    elif kind == 'status': tables['orders'] = tables['orders'].replace('paid', 'unknown')
    elif kind == 'integer': tables['payments'] = tables['payments'].replace('6000', '6e3')
    elif kind == 'large': tables['customers'] = 'x' * (4*1024*1024+1)
    elif kind == 'empty': tables['orders'] = tables['orders'].splitlines()[0]+'\n'
    elif kind == 'malformed': tables['customers'] += '3,"unfinished'
    with pytest.raises(ValueError): run(tmp_path/'output', source_tables=tables)
    assert not (tmp_path/'output').exists()


def test_failed_quality_is_preserved_with_order_evidence(tmp_path):
    tables = sample_sources()
    tables['payments'] = tables['payments'].replace('6000', '5900')
    report = make_run(tmp_path, tables=tables)
    assert report['status'] == 'failed'
    assert report['diagnostics']['order_mismatches'][0]['delta_cents'] == -100
    assert report['diagnostics']['affected_models'] == ['ads_daily_sales','dwd_order','dws_daily_region']
    assert history.list_runs(tmp_path)[0]['status'] == 'failed'


def test_preview_is_paginated_readonly_and_rejects_sql(tmp_path):
    make_run(tmp_path)
    value = history.preview_table(tmp_path, 'first', 'orders', 1, 1)
    assert value['total'] == 2 and value['rows'][0][0] == 2
    with closing(history.readonly_database(tmp_path, 'first')) as db:
        with pytest.raises(sqlite3.OperationalError): db.execute('DELETE FROM orders')
    for table in ['sqlite_master', 'orders; DELETE FROM orders', '../warehouse.sqlite']:
        with pytest.raises(ValueError): history.preview_table(tmp_path, 'first', table)
    with pytest.raises(ValueError): history.preview_table(tmp_path, 'first', 'orders', limit=101)


def test_revalidation_detects_mutation_without_overwriting_report(tmp_path):
    make_run(tmp_path)
    saved = (tmp_path/'.demo-data/first/report.json').read_bytes()
    with closing(sqlite3.connect(tmp_path/'.demo-data/first/warehouse.sqlite')) as db, db:
        db.execute('UPDATE ads_daily_sales SET paid_cents=paid_cents+1')
    result = history.revalidate(tmp_path, 'first')
    assert result['status'] == 'failed' and result['snapshot_unchanged'] is False
    assert (tmp_path/'.demo-data/first/report.json').read_bytes() == saved


def test_compare_uses_actual_totals_and_changed_source_hashes(tmp_path):
    make_run(tmp_path, 'before')
    tables = sample_sources()
    tables['payments'] = tables['payments'].replace('4000', '4100')
    make_run(tmp_path, 'after', tables)
    compared = history.compare_runs(tmp_path, 'before', 'after')
    assert next(m for m in compared['metrics'] if m['metric']=='paid_cents')['delta'] == 100
    assert [s['table'] for s in compared['source_changes'] if s['changed']] == ['payments']
    assert any(c['name']=='paid_order_balance' for c in compared['changed_checks'])


def test_missing_source_fingerprint_is_not_claimed_as_a_change(tmp_path):
    run(tmp_path/'.demo-data/synthetic', orders=2)
    make_run(tmp_path, 'csv')
    compared = history.compare_runs(tmp_path, 'synthetic', 'csv')
    assert all(item['changed'] is None for item in compared['source_changes'])


def test_exports_allowlist_and_path_traversal(tmp_path):
    make_run(tmp_path)
    with zipfile.ZipFile(io.BytesIO(history.zip_artifacts(tmp_path, 'first'))) as archive:
        assert set(archive.namelist()) == set(history.ARTIFACTS)
        assert json.loads(archive.read('report.json'))['source_mode'] == 'csv'
    with zipfile.ZipFile(io.BytesIO(history.zip_template())) as archive:
        assert len(archive.namelist()) == 5
        parse_sources({name.removesuffix('.csv'):archive.read(name).decode() for name in archive.namelist()})
    for run_id in ['..', '../first', '%2e%2e', 'first/report.json']:
        with pytest.raises(ValueError): history.get_run(tmp_path, run_id)
    with pytest.raises(ValueError): history.artifact_path(tmp_path, 'first', '../../.env')


def test_input_directory_and_empty_payment_table(tmp_path):
    tables = sample_sources()
    tables['payments'] = tables['payments'].splitlines()[0]+'\n'
    for name, content in tables.items(): (tmp_path/(name+'.csv')).write_text(content, encoding='utf-8-sig')
    parsed, _ = parse_sources(read_directory(tmp_path))
    assert parsed['payments'] == []


def test_quality_evaluation_all_authored_cases_detected():
    result = evaluate()
    assert len(result['cases']) == 12
    assert result['case_pass_rate'] == result['fault_detection_rate'] == 1
    assert result['control_false_positive_rate'] == 0


def test_large_integer_evidence_survives_browser_json_boundary(tmp_path):
    tables = sample_sources()
    tables['order_items'] = 'item_id,order_id,product_id,quantity,unit_price_cents\n' + ''.join(
        f'{i},1,1,10000,1000000000\n' for i in range(1, 1001)) + '1001,1,1,1,1\n'
    report = make_run(tmp_path, tables=tables)
    assert report['daily_sales'][0]['gross_cents'] == 10000000000000001
    wire = json.loads(json.dumps(browser_safe(report)))
    assert wire['daily_sales'][0]['gross_cents'] == '10000000000000001'
    assert wire['synthetic'] is False and wire['source_orders'] == 2
    assert browser_safe(-10000000000000001) == '-10000000000000001'
