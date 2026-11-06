"""Reproducible failure-injection evaluation of deterministic quality checks."""
from __future__ import annotations
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
from time import perf_counter
from .pipeline import run
from .history import diagnose


def evaluate() -> dict:
    cases = json.loads((Path(__file__).resolve().parent.parent/'eval/quality_cases.json').read_text(encoding='utf-8'))
    results = []
    with tempfile.TemporaryDirectory(prefix='warehouse-quality-eval-') as temp:
        base = Path(temp)/'baseline'
        report = run(base, orders=30, seed=7)
        for case in cases:
            database = Path(temp)/(case['id']+'.sqlite')
            shutil.copyfile(base/'warehouse.sqlite', database)
            start = perf_counter()
            with closing(sqlite3.connect(database)) as db, db:
                if case['sql']:
                    db.executescript(case['sql'])
                observation = diagnose(db, report)
            actual = [failure['name'] for failure in observation['failures']]
            expected = case['expected_failures']
            passed = set(expected) <= set(actual) if expected else not actual
            results.append({'case': case['id'], 'expected_failures': expected, 'observed_failures': actual,
                            'passed': passed, 'elapsed_ms': round((perf_counter()-start)*1000, 3)})
    faults = [r for r in results if r['expected_failures']]
    controls = [r for r in results if not r['expected_failures']]
    return {'evaluation': 'deterministic-quality-fault-injection', 'created_at': datetime.now(timezone.utc).isoformat(),
            'dataset': '12 authored synthetic regression cases, 30 orders, seed 7',
            'case_pass_rate': sum(r['passed'] for r in results)/len(results),
            'fault_detection_rate': sum(bool(r['observed_failures']) for r in faults)/len(faults),
            'control_false_positive_rate': sum(bool(r['observed_failures']) for r in controls)/len(controls),
            'cases': results, 'limitation': 'Small deterministic regression suite, not LLM accuracy, production incident success rate, or an independent held-out benchmark.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = evaluate()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as stream:
            stream.write(text+'\n')
    raise SystemExit(0 if all(case['passed'] for case in result['cases']) else 1)


if __name__ == '__main__':
    main()
