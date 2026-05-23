import os
from pathlib import Path

V3_ROOT = Path(__file__).resolve().parent.parent

if os.environ.get("INVOICE_TEST") == "1":
    DATA_DIR = V3_ROOT / "tests" / "data"
else:
    DATA_DIR = V3_ROOT / "data"
