"""Inspect SQLite metadata, compile a small semantic plan, execute and reconcile it.

This is an explicit rule-based demo planner, not a replacement for the live LLM
graph. Its inputs and all output records are synthetic and reproducible.
"""
from __future__ import annotations

import html
import json
from contextlib import closing
from pathlib import Path
import random
import re
import sqlite3
from typing import Any


def quote(name: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError(f"Unsupported identifier: {name!r}")
    return '"' + name + '"'


def seed_source(path: Path, orders: int = 120, seed: int = 7) -> None:
    """Generate customer/order/item/payment data, including cancelled orders."""
    if not 1 <= orders <= 10000:
        raise ValueError("orders must be between 1 and 10000")
    rng = random.Random(seed)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""
        CREATE TABLE customers(customer_id INTEGER PRIMARY KEY, region TEXT NOT NULL);
        CREATE TABLE products(product_id INTEGER PRIMARY KEY, category TEXT NOT NULL, price_cents INTEGER NOT NULL CHECK(price_cents>0));
        CREATE TABLE orders(order_id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers, order_date TEXT NOT NULL, status TEXT NOT NULL);
        CREATE TABLE order_items(item_id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders, product_id INTEGER NOT NULL REFERENCES products, quantity INTEGER NOT NULL CHECK(quantity>0), unit_price_cents INTEGER NOT NULL);
        CREATE TABLE payments(payment_id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders, amount_cents INTEGER NOT NULL, status TEXT NOT NULL);
        """)
        db.executemany("INSERT INTO customers VALUES (?,?)", [(i, ['North','South','East','West'][i % 4]) for i in range(1, 21)])
        products = [(i, ['Books','Home','Outdoors'][i % 3], rng.randrange(100, 10000)) for i in range(1, 13)]
        db.executemany("INSERT INTO products VALUES (?,?,?)", products)
        for oid in range(1, orders + 1):
            status = 'cancelled' if oid % 10 == 0 else 'paid'
            db.execute("INSERT INTO orders VALUES (?,?,?,?)", (oid, rng.randrange(1,21), f'2026-01-{1 + oid % 28:02d}', status))
            total = 0
            for _ in range(rng.randrange(1, 4)):
                product = rng.choice(products)
                qty = rng.randrange(1, 4)
                total += qty * product[2]
                db.execute("INSERT INTO order_items(order_id,product_id,quantity,unit_price_cents) VALUES (?,?,?,?)", (oid, product[0], qty, product[2]))
            # Split payments exercise the payment/item fan-out trap.
            if status == 'paid':
                first = total // 2
                db.executemany("INSERT INTO payments(order_id,amount_cents,status) VALUES (?,?,'settled')", [(oid, first), (oid, total-first)])


def explore(db: sqlite3.Connection) -> list[dict[str, Any]]:
    tables = []
    names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for name in names:
        columns = [dict(zip(['cid','name','type','notnull','default','pk'], r)) for r in db.execute(f'PRAGMA table_info({quote(name)})')]
        foreign_keys = [dict(zip(['id','seq','table','from','to','on_update','on_delete','match'], r)) for r in db.execute(f'PRAGMA foreign_key_list({quote(name)})')]
        tables.append({'name': name, 'columns': columns, 'foreign_keys': foreign_keys, 'rows': db.execute(f'SELECT COUNT(*) FROM {quote(name)}').fetchone()[0]})
    return tables


def plan_model(schema: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fail if metadata does not satisfy the documented ecommerce contract."""
    required = {'customers': {'customer_id','region'}, 'products': {'product_id','category','price_cents'}, 'orders': {'order_id','customer_id','order_date','status'}, 'order_items': {'item_id','order_id','product_id','quantity','unit_price_cents'}, 'payments': {'payment_id','order_id','amount_cents','status'}}
    actual = {t['name']: {c['name'] for c in t['columns']} for t in schema}
    for table, columns in required.items():
        missing = columns - actual.get(table, set())
        if missing:
            raise ValueError(f'Missing source columns: {table}: {sorted(missing)}')
    plan = [{'name': f'ods_{name}', 'layer': 'ODS', 'grain': [next(c['name'] for c in table['columns'] if c['pk'])], 'sources': [name], 'select': f'SELECT * FROM {quote(name)}'} for table in schema if (name := table['name']) in required]
    plan.extend([
        {'name':'dim_customer','layer':'DIM','grain':['customer_id'],'sources':['ods_customers'],'select':'SELECT customer_id, region FROM ods_customers'},
        {'name':'dim_product','layer':'DIM','grain':['product_id'],'sources':['ods_products'],'select':'SELECT product_id, category, price_cents FROM ods_products'},
        {'name':'dwd_order','layer':'DWD','grain':['order_id'],'sources':['ods_orders','ods_order_items','ods_payments','dim_customer'], 'select':"""SELECT o.order_id,o.order_date,o.status,o.customer_id,c.region,
            COALESCE(i.item_count,0) AS item_count, COALESCE(i.gross_cents,0) AS gross_cents,
            COALESCE(p.paid_cents,0) AS paid_cents
            FROM ods_orders o JOIN dim_customer c ON c.customer_id=o.customer_id
            LEFT JOIN (SELECT order_id,SUM(quantity) AS item_count,SUM(quantity*unit_price_cents) AS gross_cents FROM ods_order_items GROUP BY order_id) i ON i.order_id=o.order_id
            LEFT JOIN (SELECT order_id,SUM(amount_cents) AS paid_cents FROM ods_payments WHERE status='settled' GROUP BY order_id) p ON p.order_id=o.order_id"""},
        {'name':'dws_daily_region','layer':'DWS','grain':['order_date','region'],'sources':['dwd_order'],'select':"SELECT order_date,region,COUNT(*) AS order_count,SUM(item_count) AS item_count,SUM(gross_cents) AS gross_cents,SUM(paid_cents) AS paid_cents FROM dwd_order WHERE status='paid' GROUP BY order_date,region"},
        {'name':'ads_daily_sales','layer':'ADS','grain':['order_date'],'sources':['dws_daily_region'],'select':'SELECT order_date,SUM(order_count) AS order_count,SUM(gross_cents) AS gross_cents,SUM(paid_cents) AS paid_cents,ROUND(1.0*SUM(paid_cents)/NULLIF(SUM(order_count),0),2) AS average_order_cents FROM dws_daily_region GROUP BY order_date'},
    ])
    return plan


def materialize(db: sqlite3.Connection, plan: list[dict[str, Any]]) -> list[str]:
    sql = []
    for model in plan:
        statement = f"CREATE TABLE {quote(model['name'])} AS {model['select']};"
        db.execute(statement)
        grain = ','.join(quote(c) for c in model['grain'])
        index = f"CREATE UNIQUE INDEX {quote('uq_' + model['name'])} ON {quote(model['name'])} ({grain});"
        db.execute(index)
        sql.extend([statement,index])
    return sql


def validate(db: sqlite3.Connection, plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compute checks from persisted records; no canned pass/fail values."""
    checks = []
    def check(name: str, actual: Any, expected: Any):
        checks.append({'name': name, 'passed': actual == expected, 'actual':actual, 'expected':expected})
    scalar = lambda sql: db.execute(sql).fetchone()[0]
    check('source_foreign_keys', len(db.execute('PRAGMA foreign_key_check').fetchall()), 0)
    for model in plan:
        name = quote(model['name']); grain = ','.join(quote(c) for c in model['grain'])
        check(model['name'] + ':unique_grain', scalar(f'SELECT COUNT(*) FROM (SELECT {grain} FROM {name} GROUP BY {grain} HAVING COUNT(*)>1)'), 0)
        check(model['name'] + ':non_null_grain', scalar(f'SELECT COUNT(*) FROM {name} WHERE ' + ' OR '.join(quote(c)+' IS NULL' for c in model['grain'])), 0)
    check('order_rows_preserved',scalar('SELECT COUNT(*) FROM dwd_order'),scalar('SELECT COUNT(*) FROM orders'))
    check('paid_order_count',scalar('SELECT COALESCE(SUM(order_count),0) FROM ads_daily_sales'),scalar("SELECT COUNT(*) FROM orders WHERE status='paid'"))
    check('payment_total_reconciles',scalar('SELECT COALESCE(SUM(paid_cents),0) FROM ads_daily_sales'),scalar("SELECT COALESCE(SUM(p.amount_cents),0) FROM payments p JOIN orders o USING(order_id) WHERE p.status='settled' AND o.status='paid'"))
    check('items_total_reconciles',scalar('SELECT COALESCE(SUM(gross_cents),0) FROM ads_daily_sales'),scalar("SELECT COALESCE(SUM(i.quantity*i.unit_price_cents),0) FROM order_items i JOIN orders o USING(order_id) WHERE o.status='paid'"))
    check('paid_order_balance',scalar("SELECT COUNT(*) FROM dwd_order WHERE status='paid' AND gross_cents<>paid_cents"),0)
    return checks


def run(output: Path, orders: int = 120, seed: int = 7) -> dict[str, Any]:
    # Keep the final path component intact until link checks have completed.
    output = Path(output).absolute()
    if output.is_symlink() or getattr(output, 'is_junction', lambda: False)():
        raise FileExistsError(f'Refusing linked output directory {output}; choose a new output directory')
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise FileExistsError(f'Refusing non-empty output directory {output}; choose a new output directory')
    dbpath = output / 'warehouse.sqlite'
    # Exclusive creation also prevents concurrent runs from sharing a database.
    with dbpath.open('xb'):
        pass
    seed_source(dbpath, orders, seed)
    with closing(sqlite3.connect(dbpath)) as db, db:
        schema = explore(db)
        plan = plan_model(schema)
        sql = materialize(db, plan)
        checks = validate(db, plan)
        db.row_factory = sqlite3.Row
        daily = [dict(row) for row in db.execute('SELECT * FROM ads_daily_sales ORDER BY order_date')]
        counts = {m['name']: db.execute(f"SELECT COUNT(*) FROM {quote(m['name'])}").fetchone()[0] for m in plan}
    lineage = [{'source':source,'target':model['name']} for model in plan for source in model['sources']]
    report = {'mode':'offline-demo','planner':'deterministic ecommerce rules; no LLM call','dialect':'sqlite','synthetic':True,'seed':seed,'source_orders':orders,'status':'passed' if all(c['passed'] for c in checks) else 'failed','checks':checks,'row_counts':counts,'daily_sales':daily,'lineage':lineage,'models':plan,'schema':schema}
    def write_artifact(name: str, content: str) -> None:
        # Never replace a file or follow a symlink created after the initial check.
        with (output / name).open('x', encoding='utf-8') as artifact:
            artifact.write(content)
    for name, value in [('report',report),('schema',schema),('model',plan),('lineage',lineage)]:
        write_artifact(name+'.json',json.dumps(value,ensure_ascii=False,indent=2))
    write_artifact('warehouse.sql','\n\n'.join(sql))
    mermaid = 'flowchart LR\n'+'\n'.join(f"    {e['source']} --> {e['target']}" for e in lineage)
    write_artifact('lineage.mmd',mermaid+'\n')
    rows = ''.join(f"<tr><td>{html.escape(c['name'])}</td><td>{'PASS' if c['passed'] else 'FAIL'}</td><td>{c['actual']}</td><td>{c['expected']}</td></tr>" for c in checks)
    write_artifact('report.html','<!doctype html><meta charset="utf-8"><title>Warehouse demo</title><style>body{font:16px system-ui;max-width:1000px;margin:40px auto}td,th{padding:8px;border-bottom:1px solid #ddd;text-align:left}pre{white-space:pre-wrap}</style><h1>Warehouse Modeling · 离线演示</h1><p>SQLite 实际执行；合成电商数据；确定性规则规划；无模型调用。</p><h2>'+report['status']+'</h2><table><tr><th>检查</th><th>结果</th><th>实际</th><th>期望</th></tr>'+rows+'</table><h2>模型与血缘</h2><pre>'+html.escape(mermaid)+'</pre>')
    return report
