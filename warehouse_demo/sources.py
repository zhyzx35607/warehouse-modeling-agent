"""Bounded CSV ingestion for the documented ecommerce source contract."""
from __future__ import annotations

import csv
from datetime import date
import hashlib
import io
import re
import sqlite3
from pathlib import Path

MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_TABLE_ROWS = 50000
COLUMNS = {
    'customers': ['customer_id', 'region'],
    'products': ['product_id', 'category', 'price_cents'],
    'orders': ['order_id', 'customer_id', 'order_date', 'status'],
    'order_items': ['item_id', 'order_id', 'product_id', 'quantity', 'unit_price_cents'],
    'payments': ['payment_id', 'order_id', 'amount_cents', 'status'],
}
DDL = """
CREATE TABLE customers(customer_id INTEGER PRIMARY KEY, region TEXT NOT NULL);
CREATE TABLE products(product_id INTEGER PRIMARY KEY, category TEXT NOT NULL, price_cents INTEGER NOT NULL CHECK(price_cents>0));
CREATE TABLE orders(order_id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers, order_date TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE order_items(item_id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders, product_id INTEGER NOT NULL REFERENCES products, quantity INTEGER NOT NULL CHECK(quantity>0), unit_price_cents INTEGER NOT NULL);
CREATE TABLE payments(payment_id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders, amount_cents INTEGER NOT NULL, status TEXT NOT NULL);
"""


def parse_sources(tables: dict) -> tuple[dict, list[dict]]:
    if not isinstance(tables, dict) or set(tables) != set(COLUMNS):
        raise ValueError('CSV requires exactly customers, products, orders, order_items and payments')
    if any(not isinstance(value, str) for value in tables.values()):
        raise ValueError('Each source must be UTF-8 CSV text')
    if sum(len(value.encode('utf-8')) for value in tables.values()) > MAX_SOURCE_BYTES:
        raise ValueError('CSV sources exceed the 4 MiB total limit')
    parsed, manifest = {}, []
    for table, columns in COLUMNS.items():
        text = tables[table].removeprefix('\ufeff')
        reader = csv.reader(io.StringIO(text, newline=''), strict=True)
        try:
            if next(reader, None) != columns:
                raise ValueError(f'{table}: headers must be {", ".join(columns)} in that order')
            rows, seen = [], set()
            for number, raw in enumerate(reader, 2):
                if not raw:
                    continue
                if len(raw) != len(columns):
                    raise ValueError(f'{table}: row {number} has the wrong column count')
                row = []
                for name, value in zip(columns, raw):
                    value = value.strip()
                    if not value or len(value) > 256 or '\x00' in value:
                        raise ValueError(f'{table}: row {number}, {name} is empty or too long')
                    if name.endswith('_id') or name.endswith('_cents') or name == 'quantity':
                        minimum = 0 if name in {'amount_cents', 'unit_price_cents'} else 1
                        maximum = 10000 if name == 'quantity' else 1_000_000_000
                        if not re.fullmatch(r'[0-9]{1,10}', value) or not minimum <= int(value) <= maximum:
                            raise ValueError(f'{table}: row {number}, {name} is outside its integer range')
                        value = int(value)
                    elif name == 'order_date':
                        try:
                            if date.fromisoformat(value).isoformat() != value:
                                raise ValueError()
                        except ValueError:
                            raise ValueError(f'{table}: row {number}, order_date must be YYYY-MM-DD') from None
                    elif name == 'status':
                        allowed = {'paid', 'cancelled'} if table == 'orders' else {'settled', 'failed'}
                        if value not in allowed:
                            raise ValueError(f'{table}: row {number}, unsupported status')
                    row.append(value)
                if row[0] in seen:
                    raise ValueError(f'{table}: row {number} repeats its primary key')
                seen.add(row[0]); rows.append(tuple(row))
                if len(rows) > (10000 if table == 'orders' else MAX_TABLE_ROWS):
                    raise ValueError(f'{table}: too many rows')
        except csv.Error:
            raise ValueError(f'{table}: malformed CSV') from None
        if table in {'customers', 'products', 'orders'} and not rows:
            raise ValueError(f'{table}: at least one data row is required')
        parsed[table] = rows
        manifest.append({'table': table, 'rows': len(rows), 'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()})
    keys = {table: {row[0] for row in rows} for table, rows in parsed.items()}
    for table, column, target in [('orders', 'customer_id', 'customers'), ('order_items', 'order_id', 'orders'),
                                  ('order_items', 'product_id', 'products'), ('payments', 'order_id', 'orders')]:
        index = COLUMNS[table].index(column)
        if any(row[index] not in keys[target] for row in parsed[table]):
            raise ValueError(f'{table}: {column} references a missing {target} row')
    return parsed, manifest


def import_sources(db: sqlite3.Connection, parsed: dict) -> None:
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript(DDL)
    for table, columns in COLUMNS.items():
        placeholders = ','.join('?' for _ in columns)
        db.executemany(f'INSERT INTO {table} VALUES ({placeholders})', parsed[table])


def read_directory(directory: Path) -> dict[str, str]:
    directory = Path(directory)
    files = [directory / (name + '.csv') for name in COLUMNS]
    if any(path.is_symlink() or not path.is_file() for path in files):
        raise ValueError('Input directory must contain five regular CSV files matching the template')
    if sum(path.stat().st_size for path in files) > MAX_SOURCE_BYTES:
        raise ValueError('CSV sources exceed the 4 MiB total limit')
    try:
        return {path.stem: path.read_text(encoding='utf-8-sig') for path in files}
    except UnicodeError:
        raise ValueError('CSV files must use UTF-8 encoding') from None


def sample_sources() -> dict[str, str]:
    return {
        'customers': 'customer_id,region\n1,East\n2,West\n',
        'products': 'product_id,category,price_cents\n1,Books,5000\n2,Home,3000\n',
        'orders': 'order_id,customer_id,order_date,status\n1,1,2026-01-01,paid\n2,2,2026-01-02,cancelled\n',
        'order_items': 'item_id,order_id,product_id,quantity,unit_price_cents\n1,1,1,2,5000\n2,2,2,1,3000\n',
        'payments': 'payment_id,order_id,amount_cents,status\n1,1,6000,settled\n2,1,4000,settled\n',
    }
