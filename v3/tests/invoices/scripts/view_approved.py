"""List and summarise all invoices in invoice_staging/approved/ under the test sandbox."""

import json
import sys
from pathlib import Path

_V3_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if "--live" in sys.argv:
    APPROVED_DIR = _V3_ROOT / "data" / "invoice_staging" / "approved"
    print("Mode: LIVE")
else:
    APPROVED_DIR = _V3_ROOT / "tests" / "data" / "invoice_staging" / "approved"
    print("Mode: TEST (use --live for live data)")

if not APPROVED_DIR.exists():
    print(f"Approved directory not found: {APPROVED_DIR}")
    sys.exit(0)

files = sorted(APPROVED_DIR.glob("*.json"))

print(f"\n{'='*70}")
print(f"  APPROVED TEST INVOICES  ({len(files)} files)  →  {APPROVED_DIR}")
print(f"{'='*70}")

if not files:
    print("  No approved test invoices.")
else:
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            cust = d.get("customer", {})
            name  = (cust.get("name")  or {}).get("value") or "N/A"
            email = (cust.get("email") or {}).get("value") or "N/A"
            totals = d.get("totals") or {}
            total = totals.get("total")
            tax   = totals.get("tax")
            total_str = f"${total:.2f}" if total is not None else "N/A"
            tax_str   = f"${tax:.2f}"   if tax   is not None else "N/A"
            items = len(d.get("line_items", []))
            print(f"\n  {f.name}")
            print(f"    Customer   : {name}  ({email})")
            print(f"    Total      : {total_str}  |  Tax: {tax_str}  |  Line items: {items}")
        except Exception as e:
            print(f"\n  {f.name}  [ERROR: {e}]")

print(f"\n{'='*70}\n")
