"""List and summarise all invoices in invoice_staging/pending/."""

import json
import sys
from pathlib import Path

PENDING_DIR = Path(__file__).resolve().parent.parent.parent / "invoice_staging" / "pending"

if not PENDING_DIR.exists():
    print(f"Pending directory not found: {PENDING_DIR}")
    sys.exit(0)

files = sorted(PENDING_DIR.glob("*.json"))

print(f"\n{'='*70}")
print(f"  PENDING INVOICES  ({len(files)} files)  →  {PENDING_DIR}")
print(f"{'='*70}")

if not files:
    print("  No pending invoices.")
else:
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            cust = d.get("customer", {})
            name  = (cust.get("name")  or {}).get("value") or "N/A"
            email = (cust.get("email") or {}).get("value") or "N/A"
            total = (d.get("totals") or {}).get("total")
            total_str = f"${total:.2f}" if total is not None else "N/A"
            conf  = d.get("parse_confidence", "?").upper()
            warns = len(d.get("warnings", []))
            print(f"\n  {f.name}")
            print(f"    Customer   : {name}  ({email})")
            print(f"    Total      : {total_str}  |  Confidence: {conf}  |  Warnings: {warns}")
            if d.get("review_fields"):
                print(f"    Review     : {', '.join(d['review_fields'])}")
        except Exception as e:
            print(f"\n  {f.name}  [ERROR: {e}]")

print(f"\n{'='*70}\n")
