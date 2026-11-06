"""Run CLI, HTTP, reconciliation and error-path checks without external services."""
from __future__ import annotations
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from warehouse_demo.pipeline import run, validate
import warehouse_demo.pipeline as pipeline_module
from warehouse_demo.server import Handler
from http.server import ThreadingHTTPServer

def verify_output_preservation(root: Path):
    filenames=['warehouse.sqlite','report.json','schema.json','model.json','lineage.json',
               'warehouse.sql','lineage.mmd','report.html','user-notes.txt']
    for index,filename in enumerate(filenames):
        output=root/f'occupied-{index}';output.mkdir()
        target=output/filename;target.write_bytes(b'existing-user-content')
        try:run(output,orders=1)
        except FileExistsError:pass
        else:raise AssertionError(f'Existing {filename} was accepted')
        assert target.read_bytes()==b'existing-user-content'
        assert list(output.iterdir())==[target],'Rejected run created additional files'
    empty=root/'empty';empty.mkdir()
    assert run(empty,orders=1)['status']=='passed','An existing empty directory should remain supported'
    simulated=root/'simulated-link'
    with patch.object(Path,'is_symlink',return_value=True),patch.object(pipeline_module,'seed_source') as seed:
        try:run(simulated,orders=1)
        except FileExistsError:pass
        else:raise AssertionError('Linked-directory rejection branch was not enforced')
        seed.assert_not_called()
    assert not simulated.exists(),'Linked-directory rejection created output'
    link_target=root/'link-target';link_target.mkdir()
    link=root/'linked-output'
    try:link.symlink_to(link_target,target_is_directory=True)
    except OSError as exc:
        if getattr(exc,'winerror',None)!=1314:raise
        print('SKIP: directory symlink test requires Windows symlink privileges')
    else:
        try:run(link,orders=1)
        except FileExistsError:pass
        else:raise AssertionError('Symlink output directory was accepted')
        assert list(link_target.iterdir())==[],'Symlink target was modified'
    # Introduce a report after the initial emptiness check to verify exclusive writes.
    concurrent=root/'concurrent-output'
    original_validate=pipeline_module.validate
    def introduce_report(db,plan):
        checks=original_validate(db,plan)
        (concurrent/'report.json').write_bytes(b'created-by-another-writer')
        return checks
    with patch.object(pipeline_module,'validate',side_effect=introduce_report):
        try:run(concurrent,orders=1)
        except FileExistsError:pass
        else:raise AssertionError('Late-created report was overwritten')
    assert (concurrent/'report.json').read_bytes()==b'created-by-another-writer'


def main():
    with tempfile.TemporaryDirectory(prefix='warehouse-demo-check-') as directory:
        verify_output_preservation(Path(directory))
        path=Path(directory)/'cli'
        subprocess.run([sys.executable,'-S',str(Path(__file__).with_name('run_demo.py')),'--output',str(path),'--orders','37'],check=True)
        result=json.loads((path/'report.json').read_text(encoding='utf-8'))
        assert result['status']=='passed'
        assert result['row_counts']['dwd_order']==37
        with closing(sqlite3.connect(path/'warehouse.sqlite')) as db, db:
            db.execute('UPDATE ads_daily_sales SET paid_cents=paid_cents+1')
            checks=validate(db,result['models'])
            assert not next(c for c in checks if c['name']=='payment_total_reconciles')['passed']
        # Point HTTP output into the temporary workspace for a clean smoke test.
        import warehouse_demo.server as server_module
        original_root=server_module.ROOT
        server_module.ROOT=Path(directory)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            base=f'http://127.0.0.1:{server.server_port}'
            with urlopen(base+'/health',timeout=5) as response:
                assert json.load(response)['mode']=='offline-demo'
            request=Request(base+'/api/demo/run',data=json.dumps({'orders':12,'seed':4}).encode(),headers={'Content-Type':'application/json'},method='POST')
            with urlopen(request,timeout=10) as response:
                report=json.load(response)
                assert report['status']=='passed' and report['source_orders']==12 and report['seed']==4
            outputs=set((Path(directory)/'.demo-data').iterdir())
            invalid_payloads=[[],None,True,1,'text',{'orders':0},{'orders':10001},
                              {'orders':1.5},{'orders':True},{'orders':'12'},
                              {'seed':1.5},{'seed':False},{'seed':'4'},
                              {'orders':12,'output':'../../outside'}]
            invalid_bodies=[json.dumps(payload).encode() for payload in invalid_payloads]+[b'{broken',b'\xff']
            for body in invalid_bodies:
                bad=Request(base+'/api/demo/run',data=body,headers={'Content-Type':'application/json'},method='POST')
                try:urlopen(bad,timeout=5)
                except HTTPError as exc:
                    assert exc.code==400,(body,exc.code)
                    assert json.load(exc).get('detail')
                else:raise AssertionError(f'Invalid request was accepted: {body!r}')
                assert set((Path(directory)/'.demo-data').iterdir())==outputs,'Invalid input created output'
            with patch.object(server_module,'run',side_effect=RuntimeError('private-path-or-credential-marker')):
                try:urlopen(request,timeout=5)
                except HTTPError as exc:
                    assert exc.code==500
                    message=json.load(exc)
                    assert 'private-path-or-credential-marker' not in json.dumps(message)
                else:raise AssertionError('Execution exception was reported as success')
            try:urlopen(base+'/api/v1/tasks',timeout=5)
            except HTTPError as exc:assert exc.code==409
            else:raise AssertionError('Offline server impersonated a live endpoint')
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)
            server_module.ROOT=original_root
    print('PASS: dependency-free CLI, real SQLite data, tamper detection, output preservation, HTTP execution and invalid-input handling')

if __name__=='__main__':main()
