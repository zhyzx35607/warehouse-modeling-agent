"""Works with Python -S: the offline demo has no third-party dependency."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from warehouse_demo.__main__ import main
if __name__=='__main__':main()
