"""Local warehouse lab: CSV import, immutable runs, previews, diagnostics and exports."""
from __future__ import annotations
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import uuid
from .pipeline import run
from . import history
from .sources import COLUMNS, MAX_SOURCE_BYTES, parse_sources
from .serialization import browser_safe

ROOT = Path(__file__).resolve().parent.parent


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT/'dashboard'/'dist'), **kwargs)

    def send_bytes(self, data, content_type, status=200, filename=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, value, status=200):
        return self.send_bytes(json.dumps(browser_safe(value), ensure_ascii=False).encode(), 'application/json; charset=utf-8', status)

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        try:
            if path == '/health':
                return self.send_json({'status': 'ok', 'mode': 'offline-demo', 'version': '0.2.0', 'summary': 'SQLite 本地数据实验室'})
            if path == '/api/demo/info':
                return self.send_json({'mode': 'offline-demo', 'planner': 'deterministic', 'live_engine': False, 'csv_schema': COLUMNS})
            if path == '/api/demo/template.zip':
                return self.send_bytes(history.zip_template(), 'application/zip', filename='warehouse-csv-template.zip')
            if path == '/api/demo/runs':
                return self.send_json(history.list_runs(ROOT))
            if path == '/api/demo/compare':
                return self.send_json(history.compare_runs(ROOT, query.get('left', [''])[0], query.get('right', [''])[0]))
            if path.startswith('/api/demo/runs/'):
                parts = path[len('/api/demo/runs/'):].split('/')
                run_id = parts[0]
                if len(parts) == 1:
                    return self.send_json(history.get_run(ROOT, run_id))
                if len(parts) == 2 and parts[1] == 'export.zip':
                    return self.send_bytes(history.zip_artifacts(ROOT, run_id), 'application/zip', filename=f'{run_id}.zip')
                if len(parts) == 3 and parts[1] == 'artifacts':
                    artifact = history.artifact_path(ROOT, run_id, parts[2])
                    return self.send_bytes(artifact.read_bytes(), 'application/octet-stream', filename=parts[2])
                if len(parts) == 3 and parts[1] == 'tables':
                    return self.send_json(history.preview_table(ROOT, run_id, parts[2], int(query.get('limit', ['25'])[0]), int(query.get('offset', ['0'])[0])))
                return self.send_json({'detail': 'Unknown route'}, 404)
            if path.startswith('/api/') or path.startswith('/ws'):
                return self.send_json({'detail': 'This endpoint requires the live server; offline lab supports /api/demo/* only.'}, 409)
            if not (ROOT/'dashboard'/'dist'/'index.html').exists():
                return self.send_json({'detail': 'Build dashboard with npm ci && npm run build, or use CLI: python -m warehouse_demo'}, 503)
            if '.' not in path.rsplit('/', 1)[-1]:
                self.path = '/index.html'
            return super().do_GET()
        except FileNotFoundError:
            return self.send_json({'detail': 'Run or artifact not found'}, 404)
        except (ValueError, TypeError):
            return self.send_json({'detail': 'Invalid run, artifact or query parameters'}, 400)
        except Exception:
            return self.send_json({'detail': 'Could not read the local run'}, 500)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            origin = self.headers.get('Origin')
            if origin and urlparse(origin).netloc != self.headers.get('Host'):
                return self.send_json({'detail': 'Same-origin requests only'}, 403)
            size = int(self.headers.get('Content-Length', '0'))
            maximum = MAX_SOURCE_BYTES * 2 if path == '/api/demo/import' else 4096
            if not 0 <= size <= maximum:
                raise ValueError('Invalid request size')
            payload = json.loads(self.rfile.read(size) or '{}')
            if not isinstance(payload, dict):
                raise ValueError('Request body must be a JSON object')
            parts = path.split('/')
            if len(parts) == 6 and parts[1:4] == ['api', 'demo', 'runs'] and parts[5] == 'diagnose':
                if payload:
                    raise ValueError('Diagnosis takes no parameters')
                return self.send_json(history.revalidate(ROOT, parts[4]))
            if path not in {'/api/demo/run', '/api/demo/import'}:
                return self.send_json({'detail': 'Unknown route'}, 404)
            tables = None
            if path == '/api/demo/import':
                if set(payload) != {'tables'}:
                    raise ValueError('CSV import accepts only tables')
                tables = payload['tables']
                parse_sources(tables)
                orders, seed = 120, 7
            else:
                if set(payload) - {'orders', 'seed'}:
                    raise ValueError('Only orders and seed fields are accepted')
                orders, seed = payload.get('orders', 120), payload.get('seed', 7)
                if type(orders) is not int or type(seed) is not int:
                    raise ValueError('orders and seed must be JSON integers')
                if not 1 <= orders <= 10000:
                    raise ValueError('orders must be between 1 and 10000')
            output = ROOT/'.demo-data'/(datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])
            report = run(output, orders, seed, **({'source_tables': tables} if tables is not None else {}))
            report.update(output=str(output.relative_to(ROOT)), run_id=output.name)
            return self.send_json(report, 200 if report['status'] == 'passed' else 422)
        except FileNotFoundError:
            return self.send_json({'detail': 'Run not found'}, 404)
        except (ValueError, TypeError) as exc:
            return self.send_json({'detail': str(exc)}, 400)
        except Exception:
            return self.send_json({'detail': 'Demo execution failed; no successful report was returned'}, 500)


def serve(port=8000):
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'Warehouse Lab: http://127.0.0.1:{server.server_port}/demo', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
