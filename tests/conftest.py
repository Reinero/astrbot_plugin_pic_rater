import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PICAPI = ROOT / "picapi"
for p in (ROOT, PICAPI):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
