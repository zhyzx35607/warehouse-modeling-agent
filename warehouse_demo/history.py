"""Read-only access to local runs, bounded data previews and comparable evidence."""
from __future__ import annotations

from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import zipfile

from .pipeline import quote, validate
from .sources import COLUMNS, sample_sources

ARTIFACTS = ('report.json', 'report.html', 'schema.json', 'model.json', 'warehouse.sql',
             'lineage.json', 'lineage.mmd', 'warehouse.sqlite')


def fingerprint(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def run_directory(root: Path, run_id: str) -> Path:
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}', run_id):
        raise ValueError('Invalid run ID')
    parent = (root / '.demo-data').resolve()
    candidate = parent / run_id
    if candidate.is_symlink() or getattr(candidate, 'is_junction', lambda: False)():
        raise ValueError('Linked run directories are not accessible')
    if not candidate.is_dir() or candidate.resolve().parent != parent:
        raise FileNotFoundError('Run not found')
    return candidate


def artifact_path(root: Path, run_id: str, name: str) -> Path:
    if name not in ARTIFACTS:
        raise ValueError('Artifact is not available')
    directory = run_directory(root, run_id)
    path = directory / name
    if path.is_symlink() or not path.is_file() or path.resolve().parent != directory.resolve():
        raise FileNotFoundError('Artifact not found')
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('Artifact exceeds download limit')
    return path


def get_run(root: Path, run_id: str) -> dict:
    path = artifact_path(root, run_id, 'report.json')
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('Report exceeds read limit')
    result = json.loads(path.read_text(encoding='utf-8'))
    result.update(run_id=run_id, output=f'.demo-data/{run_id}')
    return result


def list_runs(root: Path) -> list[dict]:
    parent = root / '.demo-data'
    if not parent.exists():
        return []
    result = []
    candidates = [p for p in parent.iterdir() if p.is_dir() and not p.is_symlink()]
    for candidate in sorted(candidates, key=lambda p: p.stat().st_mtime_ns, reverse=True):
        try:
            report = get_run(root, candidate.name)
            result.append({key: report.get(key) for key in ('run_id', 'created_at', 'status', 'source_mode', 'source_orders', 'seed')}
                          | {'passed': sum(c['passed'] for c in report['checks']), 'checks': len(report['checks'])})
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if len(result) == 100:
            break
    return result


def readonly_database(root: Path, run_id: str):
    path = artifact_path(root, run_id, 'warehouse.sqlite')
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    conn.execute('PRAGMA query_only=ON')
    return conn


def preview_table(root: Path, run_id: str, table: str, limit: int = 25, offset: int = 0) -> dict:
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 1_000_000:
        raise ValueError('Preview limit must be 1..100 and offset 0..1000000')
    report = get_run(root, run_id)
    if table not in set(COLUMNS) | {m['name'] for m in report['models']}:
        raise ValueError('Unknown source or materialized table')
    with closing(readonly_database(root, run_id)) as db:
        cursor = db.execute(f'SELECT * FROM {quote(table)} ORDER BY rowid LIMIT ? OFFSET ?', (limit, offset))
        return {'table': table, 'columns': [column[0] for column in cursor.description], 'rows': cursor.fetchall(),
                'total': db.execute(f'SELECT COUNT(*) FROM {quote(table)}').fetchone()[0], 'limit': limit, 'offset': offset}


def diagnose(db: sqlite3.Connection, report: dict) -> dict:
    """Produce bounded, observed failure evidence, not an LLM root-cause claim."""
    checks = validate(db, report['models'])
    cursor = db.execute("""SELECT order_id,status,gross_cents,paid_cents,paid_cents-gross_cents AS delta_cents
                           FROM dwd_order WHERE status='paid' AND gross_cents<>paid_cents ORDER BY order_id LIMIT 20""")
    mismatch = [dict(zip([c[0] for c in cursor.description], row)) for row in cursor.fetchall()]
    total_mismatches = db.execute("SELECT COUNT(*) FROM dwd_order WHERE status='paid' AND gross_cents<>paid_cents").fetchone()[0]
    failures = [c for c in checks if not c['passed']]
    affected = {'dwd_order'} if total_mismatches else set()
    for failure in failures:
        candidate = failure['name'].split(':', 1)[0]
        if candidate in {m['name'] for m in report['models']}:
            affected.add(candidate)
        if 'reconciles' in failure['name']:
            affected.add('ads_daily_sales')
    for _ in report['models']:
        affected.update(e['target'] for e in report['lineage'] if e['source'] in affected)
    return {'status': 'passed' if not failures else 'failed', 'checks': checks, 'failures': failures,
            'order_mismatches': mismatch, 'order_mismatch_count': total_mismatches, 'affected_models': sorted(affected),
            'method': 'deterministic SQL checks and lineage traversal; no LLM inference',
            'guidance': '核对订单明细与已结算支付、再检查分层聚合；证据不自动解释退款、优惠或漏单原因。修正源 CSV 后创建新运行。' if failures else '当前数据通过已定义的检查；这不证明所有业务规则均已覆盖。'}


def revalidate(root: Path, run_id: str) -> dict:
    report = get_run(root, run_id)
    with closing(readonly_database(root, run_id)) as db:
        result = diagnose(db, report)
    current = fingerprint(artifact_path(root, run_id, 'warehouse.sqlite'))
    result.update(run_id=run_id, database_sha256=current,
                  snapshot_unchanged=current == report.get('database_sha256') if report.get('database_sha256') else None)
    return result


def compare_runs(root: Path, left_id: str, right_id: str) -> dict:
    if left_id == right_id:
        raise ValueError('Choose two different runs')
    left, right = get_run(root, left_id), get_run(root, right_id)
    rows = [{'table': name, 'left': left['row_counts'].get(name, 0), 'right': right['row_counts'].get(name, 0),
             'delta': right['row_counts'].get(name, 0) - left['row_counts'].get(name, 0)}
            for name in sorted(set(left['row_counts']) | set(right['row_counts']))]
    metrics = []
    for name in ('order_count', 'gross_cents', 'paid_cents'):
        first, second = [sum(row[name] for row in report['daily_sales']) for report in (left, right)]
        metrics.append({'metric': name, 'left': first, 'right': second, 'delta': second-first})
    lc, rc = [{c['name']: c for c in report['checks']} for report in (left, right)]
    changed = [{'name': name, 'left': lc.get(name), 'right': rc.get(name)} for name in sorted(set(lc) | set(rc)) if lc.get(name) != rc.get(name)]
    manifests = [{item['table']: item['sha256'] for item in report.get('source_manifest', [])} for report in (left, right)]
    source_changes = [{'table': table, 'changed': manifests[0][table] != manifests[1][table] if table in manifests[0] and table in manifests[1] else None} for table in sorted(set(manifests[0]) | set(manifests[1]))]
    return {'left': left_id, 'right': right_id, 'row_counts': rows, 'metrics': metrics, 'changed_checks': changed,
            'source_changes': source_changes, 'limitation': '比较已保存运行快照，差值为右减左；不同数据范围或随机种子的差异不代表系统效果提升。'}


def zip_artifacts(root: Path, run_id: str) -> bytes:
    paths = [(name, artifact_path(root, run_id, name)) for name in ARTIFACTS]
    if sum(path.stat().st_size for _, path in paths) > 96 * 1024 * 1024:
        raise ValueError('Run exceeds export size limit')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, path in paths:
            archive.write(path, arcname=name)
    return buffer.getvalue()


def zip_template() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, text in sample_sources().items():
            archive.writestr(name + '.csv', text)
    return buffer.getvalue()
