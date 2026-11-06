from __future__ import annotations
import argparse
from datetime import datetime
from pathlib import Path
from .pipeline import run

def main():
    parser = argparse.ArgumentParser(description='Execute a synthetic SQLite warehouse pipeline without network access.')
    parser.add_argument('--output',type=Path,default=None)
    parser.add_argument('--orders',type=int,default=120)
    parser.add_argument('--seed',type=int,default=7)
    parser.add_argument('--input-dir',type=Path,help='Import five UTF-8 ecommerce CSV files using the documented schema')
    parser.add_argument('--serve',action='store_true',help='Serve the Vue dashboard and demo API on localhost')
    parser.add_argument('--port',type=int,default=8000)
    args=parser.parse_args()
    if args.serve:
        from .server import serve
        serve(args.port)
        return
    output=args.output or Path('.demo-data')/datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    from .sources import read_directory
    tables=read_directory(args.input_dir) if args.input_dir else None
    report=run(output,args.orders,args.seed,source_tables=tables)
    print(f"{report['status'].upper()}: {len(report['models'])} models, {len(report['checks'])} checks; {output.resolve()}")
    raise SystemExit(0 if report['status']=='passed' else 1)

if __name__=='__main__':main()
