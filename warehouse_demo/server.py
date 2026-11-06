"""Local-only demo API; serves built Vue assets without live dependencies."""
from __future__ import annotations
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import urlparse
import uuid
from .pipeline import run

ROOT=Path(__file__).resolve().parent.parent

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(ROOT/'dashboard'/'dist'),**kwargs)
    def send_json(self,value,status=200):
        data=json.dumps(value,ensure_ascii=False).encode()
        self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/health':return self.send_json({'status':'ok','mode':'offline-demo','summary':'SQLite 离线演示'})
        if path=='/api/demo/info':return self.send_json({'mode':'offline-demo','synthetic':True,'planner':'deterministic','live_engine':False})
        if path.startswith('/api/') or path.startswith('/ws'):
            return self.send_json({'detail':'This endpoint requires the live server; offline demo supports /api/demo/* only.'},409)
        if not (ROOT/'dashboard'/'dist'/'index.html').exists():
            return self.send_json({'detail':'Build dashboard with npm ci && npm run build, or use CLI: python -m warehouse_demo'},503)
        if '.' not in path.rsplit('/',1)[-1]:self.path='/index.html'
        super().do_GET()
    def do_POST(self):
        if urlparse(self.path).path!='/api/demo/run':return self.send_json({'detail':'Unknown route'},404)
        try:
            # Reject cross-site browser requests to the local service.
            origin=self.headers.get('Origin')
            if origin and urlparse(origin).hostname not in {'localhost','127.0.0.1'}:return self.send_json({'detail':'Local origins only'},403)
            size=int(self.headers.get('Content-Length','0'))
            if not 0 <= size <= 4096:raise ValueError('Invalid request size')
            payload=json.loads(self.rfile.read(size) or '{}')
            if not isinstance(payload,dict):raise ValueError('Request body must be a JSON object')
            if set(payload)-{'orders','seed'}:raise ValueError('Only orders and seed fields are accepted')
            orders=payload.get('orders',120);seed=payload.get('seed',7)
            # bool is an int subclass; require actual JSON integers without coercion.
            if type(orders) is not int or type(seed) is not int:raise ValueError('orders and seed must be JSON integers')
            if not 1 <= orders <= 10000:raise ValueError('orders must be between 1 and 10000')
            output=ROOT/'.demo-data'/(datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])
            report=run(output,orders,seed)
            report['output']=str(output.relative_to(ROOT))
            return self.send_json(report,200 if report['status']=='passed' else 422)
        except (ValueError,TypeError) as exc:return self.send_json({'detail':str(exc)},400)
        except Exception:return self.send_json({'detail':'Demo execution failed; no successful report was returned'},500)

def serve(port=8000):
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    print(f'Offline demo: http://127.0.0.1:{server.server_port}/demo',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
