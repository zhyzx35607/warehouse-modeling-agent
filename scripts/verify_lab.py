"""End-to-end HTTP checks for CSV, history, preview, diagnostics and export."""
from pathlib import Path
import io
import json
import sys
import tempfile
import threading
import zipfile
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from warehouse_demo import server as module
from warehouse_demo.sources import sample_sources


def main():
    original = module.ROOT
    with tempfile.TemporaryDirectory(prefix='warehouse-lab-http-') as temp:
        module.ROOT = Path(temp)
        server = ThreadingHTTPServer(('127.0.0.1', 0), module.Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f'http://127.0.0.1:{server.server_port}'
        def call(path, payload=None, expected=200, binary=False, headers=None):
            request = Request(base+path, data=json.dumps(payload).encode() if payload is not None else None,
                              headers={'Content-Type':'application/json'} | (headers or {}))
            try:
                response = urlopen(request, timeout=10)
            except HTTPError as error:
                response = error
            with response:
                assert response.code == expected, (path, response.code, response.read())
                return response.read() if binary else json.load(response)
        try:
            assert call('/api/demo/runs') == []
            tables = sample_sources()
            first = call('/api/demo/import', {'tables':tables})
            assert first['source_mode']=='csv' and first['daily_sales'][0]['paid_cents']==10000
            tables['payments']=tables['payments'].replace('6000','5900')
            second = call('/api/demo/import', {'tables':tables}, expected=422)
            assert second['diagnostics']['order_mismatches'][0]['delta_cents']==-100
            assert len(call('/api/demo/runs'))==2
            run_url='/api/demo/runs/'+second['run_id']
            assert call(run_url)['status']=='failed'
            preview=call(run_url+'/tables/orders?limit=1&offset=1')
            assert len(preview['rows'])==1 and preview['rows'][0][0]==2
            assert call(run_url+'/diagnose', {})['snapshot_unchanged'] is True
            comparison=call('/api/demo/compare?left='+first['run_id']+'&right='+second['run_id'])
            assert next(m for m in comparison['metrics'] if m['metric']=='paid_cents')['delta']==-100
            with zipfile.ZipFile(io.BytesIO(call(run_url+'/export.zip', binary=True))) as archive:
                assert 'warehouse.sqlite' in archive.namelist()
                assert json.loads(archive.read('report.json'))['status']=='failed'
            with zipfile.ZipFile(io.BytesIO(call('/api/demo/template.zip',binary=True))) as archive:
                assert len(archive.namelist())==5
            call('/api/demo/import', {'tables':{'../bad':'text'}}, expected=400)
            call('/api/demo/runs/%2e%2e', expected=400)
            call(run_url+'/tables/sqlite_master', expected=400)
            call(run_url+'/tables/orders?limit=1000', expected=400)
            call('/api/demo/import', {'tables':tables}, expected=403, headers={'Origin':'https://other.invalid'})
            assert len(call('/api/demo/runs'))==2
            large = sample_sources()
            large['order_items'] = 'item_id,order_id,product_id,quantity,unit_price_cents\n' + ''.join(
                f'{i},1,1,10000,1000000000\n' for i in range(1, 1001)) + '1001,1,1,1,1\n'
            exact = call('/api/demo/import', {'tables':large}, expected=422)
            assert exact['daily_sales'][0]['gross_cents']=='10000000000000001'
            restored = call('/api/demo/runs/'+exact['run_id'])
            assert restored['daily_sales'][0]['gross_cents']=='10000000000000001'
        finally:
            server.shutdown(); server.server_close(); worker.join(timeout=5)
            module.ROOT=original
    print('PASS: CSV import, failed-quality evidence, persistent history, data preview, revalidation, comparisons, exports and input bounds')


if __name__=='__main__': main()
