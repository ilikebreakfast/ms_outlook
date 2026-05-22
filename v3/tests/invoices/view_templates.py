"""Display all customer templates stored in invoice_memory.db."""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "invoice_memory.db"

if not DB_PATH.exists():
    print(f"No database found at: {DB_PATH}")
    print("Run the pipeline on at least one invoice first.")
    sys.exit(0)

conn = sqlite3.connect(str(DB_PATH))
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT * FROM customer_templates ORDER BY updated_at DESC").fetchall()
conn.close()

print(f"\n{'='*70}")
print(f"  CUSTOMER TEMPLATES  ({len(rows)} records)")
print(f"{'='*70}")

if not rows:
    print("  No templates saved yet.")
else:
    for r in rows:
        print(f"\n  [{r['id']}]  {r['lookup_key']}")
        print(f"       Name    : {r['customer_name']}")
        print(f"       Email   : {r['customer_email']}")
        print(f"       Phone   : {r['customer_phone']}")
        print(f"       ABN     : {r['customer_abn']}")
        print(f"       Address : {r['customer_address']}")
        print(f"       Confirmed: {r['confirmed_count']} times  |  Updated: {r['updated_at']}")

print(f"\n{'='*70}\n")
