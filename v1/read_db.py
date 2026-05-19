"""
Quick views into pipeline.db.
Run: python read_db.py
"""
import sqlite3
import pandas as pd
from config.settings import DB_PATH

conn = sqlite3.connect(DB_PATH)

df = pd.read_sql("SELECT * FROM processed_documents ORDER BY processed_at DESC", conn)
conn.close()

pd.set_option("display.max_columns", None)
pd.set_option("display.width", 120)

print(f"\n=== All records ({len(df)}) ===")
print(df[["processed_at", "customer_name", "attachment_filename", "confidence", "needs_review", "error"]].to_string(index=False))

if df["needs_review"].any():
    print(f"\n=== Needs review ({df['needs_review'].sum()}) ===")
    print(df[df["needs_review"] == 1][["processed_at", "customer_name", "attachment_filename", "confidence"]].to_string(index=False))

errors = df[df["error"].notna()]
if not errors.empty:
    print(f"\n=== Errors ({len(errors)}) ===")
    print(errors[["processed_at", "sender_email", "attachment_filename", "error"]].to_string(index=False))
