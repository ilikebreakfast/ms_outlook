import json
import os
import sys
from pathlib import Path
from typing import Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import pandas as pd

# Ensure v2 root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from database import db
from core.parser import DeterministicParser, generate_layout_hash

app = FastAPI(title="Invoice Pipeline v2 — Operator Dashboard")

# Serve dashboard static assets
DASHBOARD_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=DASHBOARD_DIR), name="static")


class TestParseRequest(BaseModel):
    raw_text: str
    rules: Dict[str, Any]


class ApproveRequest(BaseModel):
    supplier_name: str
    document_type: str
    rules: Dict[str, Any]


@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    """Render the master visual dashboard."""
    index_path = DASHBOARD_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return index_path.read_text(encoding="utf-8")


@app.get("/api/metrics")
async def get_metrics():
    """Retrieve operational database metrics for high-level summaries."""
    conn = db.get_db_connection()
    try:
        # Total processed
        total_row = conn.execute("SELECT COUNT(*) as count FROM extraction_history;").fetchone()
        total = total_row["count"] if total_row else 0
        
        # Success rate
        success_row = conn.execute("SELECT COUNT(*) as count FROM extraction_history WHERE status = 'success';").fetchone()
        success = success_row["count"] if success_row else 0
        success_rate = round((success / total * 100), 1) if total > 0 else 100.0
        
        # Pending reviews
        pending_row = conn.execute("SELECT COUNT(*) as count FROM layout_rules WHERE status = 'pending_approval';").fetchone()
        pending = pending_row["count"] if pending_row else 0
        
        # Active rules
        active_row = conn.execute("SELECT COUNT(*) as count FROM layout_rules WHERE status = 'active';").fetchone()
        active = active_row["count"] if active_row else 0
        
        return {
            "total_processed": total,
            "success_rate": f"{success_rate}%",
            "pending_reviews": pending,
            "active_rules": active
        }
    finally:
        conn.close()


@app.get("/api/pending")
async def get_pending_templates():
    """List all templates awaiting operator visual review and approval."""
    conn = db.get_db_connection()
    try:
        rows = conn.execute("""
            SELECT layout_hash, supplier_name, document_type, created_at, extraction_rules
            FROM layout_rules
            WHERE status = 'pending_approval'
            ORDER BY created_at DESC;
        """).fetchall()
        
        pending_list = []
        for r in rows:
            # Try to grab a sample document text matching this hash from history if available
            sample_text = ""
            hist_row = conn.execute(
                "SELECT parsed_data FROM extraction_history WHERE layout_hash = ? ORDER BY processed_at DESC LIMIT 1;",
                (r["layout_hash"],)
            ).fetchone()
            
            # Simple fallback sample text if none found in logs
            if hist_row and hist_row["parsed_data"]:
                try:
                    sample_text = json.dumps(json.loads(hist_row["parsed_data"]), indent=2)
                except Exception:
                    pass
            if not sample_text:
                sample_text = "Sample raw document data has been cached. Click inspect to load."

            pending_list.append({
                "layout_hash": r["layout_hash"],
                "supplier_name": r["supplier_name"],
                "document_type": r["document_type"],
                "created_at": r["created_at"],
                "extraction_rules": json.loads(r["extraction_rules"]),
                "sample_text": sample_text
            })
        return pending_list
    finally:
        conn.close()


@app.get("/api/layout/{layout_hash}")
async def get_layout(layout_hash: str):
    """Retrieve full details and cached text sample of a single layout template."""
    details = db.get_rule_details(layout_hash)
    if not details:
        raise HTTPException(status_code=404, detail="Layout rule not found")
        
    # Get a sample text from history matching this layout hash
    conn = db.get_db_connection()
    sample_text = ""
    try:
        hist_row = conn.execute(
            "SELECT filename, parsed_data FROM extraction_history WHERE layout_hash = ? ORDER BY processed_at DESC LIMIT 1;",
            (layout_hash,)
        ).fetchone()
        
        # If no history logs yet, provide clean placeholder text
        if hist_row:
            sample_text = f"[SOURCE FILE: {hist_row['filename']}]\n\n"
            # Try getting cached raw file if we can read it, or default
            raw_cache = Path(__file__).parent.parent / "data" / "raw_text"
            # Scan matching text file
            for txt_file in raw_cache.glob("**/*.txt"):
                if txt_file.name.lower().startswith(Path(hist_row['filename']).stem.lower()):
                    try:
                        sample_text += txt_file.read_text(encoding="utf-8")
                        break
                    except Exception:
                        pass
            if len(sample_text) < 100:
                sample_text += "TAX INVOICE\nAcme Energy\nABN: 11222333444\nInvoice No: INV-1002\nTotal Amount Paid: $550.00"
        else:
            sample_text = "TAX INVOICE\nAcme Energy\nABN: 11222333444\nInvoice No: INV-1002\nTotal Amount Paid: $550.00"
    finally:
        conn.close()

    return {
        "layout_hash": details["layout_hash"],
        "supplier_name": details["supplier_name"],
        "document_type": details["document_type"],
        "status": details["status"],
        "extraction_rules": json.loads(details["extraction_rules"]),
        "sample_text": sample_text
    }


@app.post("/api/test-extraction")
async def test_extraction(req: TestParseRequest):
    """Perform live visual test execution of draft rules on sample document text."""
    try:
        p = DeterministicParser(req.raw_text, req.rules)
        extracted, confidence = p.extract_fields()
        return {
            "success": True,
            "extracted": extracted,
            "confidence": confidence
        }
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": f"Rule execution failed: {e}"}
        )


@app.post("/api/approve/{layout_hash}")
async def approve_layout(layout_hash: str, req: ApproveRequest):
    """Approve, lock, and activate a draft template layout configuration."""
    try:
        db.save_layout_rule(
            layout_hash=layout_hash,
            supplier_name=req.supplier_name,
            document_type=req.document_type,
            status="active",
            extraction_rules=req.rules
        )
        return {"success": True, "message": f"Layout template for '{req.supplier_name}' successfully locked and activated."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save layout rule: {e}")


@app.get("/api/document/file/{layout_hash}")
async def get_raw_document_file(layout_hash: str):
    """Retrieve the actual binary PDF, Excel, or CSV file for interactive embedding."""
    
    # 1. Fallback for our seeded demo layout (Acme Energy)
    if layout_hash == "a1f998485a91d5":
        mock_acme_html = """<!DOCTYPE html>
<html>
<head>
<style>
    body { font-family: 'Segoe UI', -apple-system, sans-serif; background: #fff; color: #1e293b; padding: 40px; margin: 0; line-height: 1.5; }
    .header { display: flex; justify-content: space-between; border-bottom: 2px solid #6366f1; padding-bottom: 20px; }
    .company { font-size: 24px; font-weight: bold; color: #1e1b4b; }
    .details { font-size: 12px; color: #64748b; line-height: 1.5; }
    .invoice-title { font-size: 28px; color: #6366f1; font-weight: 700; text-align: right; margin: 0; }
    .info-table { width: 100%; margin-top: 40px; border-collapse: collapse; }
    .info-table td { padding: 8px 0; font-size: 13px; }
    .items-table { width: 100%; margin-top: 40px; border-collapse: collapse; }
    .items-table th { background: #f8fafc; padding: 12px; font-size: 12px; font-weight: 600; text-align: left; border-bottom: 1px solid #e2e8f0; }
    .items-table td { padding: 12px; font-size: 13px; border-bottom: 1px solid #f1f5f9; }
    .totals { width: 300px; margin-left: auto; margin-top: 30px; }
    .totals td { padding: 6px 0; font-size: 14px; }
</style>
</head>
<body>
    <div class="header">
        <div>
            <div class="company">Acme Energy Corporation</div>
            <div class="details" style="margin-top: 6px;">
                100 Powerhouse Road, Melbourne VIC 3000<br>
                ABN: 11 222 333 444<br>
                Email: billing@acmeenergy.com.au
            </div>
        </div>
        <div>
            <h1 class="invoice-title">TAX INVOICE</h1>
            <div class="details" style="text-align: right; margin-top: 6px;">
                <strong>Invoice Number:</strong> INV-8827931<br>
                <strong>Date:</strong> 2026-05-19<br>
                <strong>Due Date:</strong> 2026-06-02
            </div>
        </div>
    </div>
    
    <div style="margin-top: 40px; font-size: 13px;">
        <strong>Bill To:</strong><br>
        Joel Wood<br>
        123 Developer Lane, Sydney NSW 2000
    </div>
    
    <table class="items-table">
        <thead>
            <tr>
                <th>Description</th>
                <th>Quantity</th>
                <th>Rate</th>
                <th>Amount</th>
            </tr>
        </thead>
        <tbody>
            <tr>
                <td>Commercial Electricity Usage - Peak Q2</td>
                <td>1</td>
                <td>$450.00</td>
                <td>$450.00</td>
            </tr>
            <tr>
                <td>Carbon Offset Green Surcharge</td>
                <td>1</td>
                <td>$45.00</td>
                <td>$45.00</td>
            </tr>
        </tbody>
    </table>
    
    <table class="totals">
        <tr>
            <td>Subtotal:</td>
            <td style="text-align: right;">$495.00</td>
        </tr>
        <tr>
            <td>GST (10% Included):</td>
            <td style="text-align: right;">$45.00</td>
        </tr>
        <tr style="font-weight: bold; font-size: 16px; border-top: 2px solid #1e293b;">
            <td style="padding-top: 10px;">Total Due:</td>
            <td style="padding-top: 10px; text-align: right; color: #6366f1;">$495.00</td>
        </tr>
    </table>
</body>
</html>
"""
        return HTMLResponse(content=mock_acme_html, media_type="text/html")

    conn = db.get_db_connection()
    try:
        hist_row = conn.execute(
            "SELECT filename FROM extraction_history WHERE layout_hash = ? ORDER BY processed_at DESC LIMIT 1;",
            (layout_hash,)
        ).fetchone()
        
        filename = hist_row["filename"] if hist_row else None
        
        if not filename:
            missing_html = """<!DOCTYPE html>
<html>
<head>
<style>
    body { font-family: sans-serif; background: #0f172a; color: #94a3b8; display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100vh; margin: 0; text-align: center; padding: 20px; }
    .card { background: #1e293b; padding: 32px; border-radius: 12px; border: 1px solid #334155; max-width: 420px; box-shadow: 0 10px 25px rgba(0,0,0,0.3); }
    h2 { color: #f43f5e; margin-top: 0; }
    p { font-size: 14px; line-height: 1.5; }
    .btn { background: #6366f1; color: #fff; border: none; padding: 8px 16px; border-radius: 6px; font-weight: 600; cursor: pointer; text-decoration: none; display: inline-block; margin-top: 16px; }
</style>
</head>
<body>
    <div class="card">
        <h2>Physical File Not Found</h2>
        <p>This layout doesn't have a binary attachment registered on local disk.</p>
        <p style="color:#38bdf8;">💡 You can toggle the <strong>"Plain Text"</strong> tab above to view and auto-extract the text representation of this document!</p>
    </div>
</body>
</html>
"""
            return HTMLResponse(content=missing_html, media_type="text/html")
            
        attachments_dir = Path(__file__).parent.parent / "data" / "attachments"
        
        # Scan matching attachment file
        for f in attachments_dir.glob("**/*"):
            if f.name.lower() == filename.lower() and f.is_file():
                media_type = "application/octet-stream"
                if f.suffix.lower() == ".pdf":
                    media_type = "application/pdf"
                elif f.suffix.lower() == ".csv":
                    media_type = "text/csv"
                elif f.suffix.lower() in [".xlsx", ".xls"]:
                    media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                
                return FileResponse(path=f, media_type=media_type, content_disposition_type="inline")
                
        # File is in history database but missing physically from data/attachments directory
        missing_file_html = f"""<!DOCTYPE html>
<html>
<head>
<style>
    body {{ font-family: sans-serif; background: #0f172a; color: #94a3b8; display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100vh; margin: 0; text-align: center; padding: 20px; }}
    .card {{ background: #1e293b; padding: 32px; border-radius: 12px; border: 1px solid #334155; max-width: 420px; box-shadow: 0 10px 25px rgba(0,0,0,0.3); }}
    h2 {{ color: #e11d48; margin-top: 0; }}
    p {{ font-size: 14px; line-height: 1.5; }}
</style>
</head>
<body>
    <div class="card">
        <h2>Document Cleaned or Missing</h2>
        <p>The original binary file <strong>{filename}</strong> is not present in local attachments storage.</p>
        <p style="color:#0ea5e9;">💡 Toggle the <strong>"Plain Text"</strong> view above to view and auto-extract the cached text representation!</p>
    </div>
</body>
</html>
"""
        return HTMLResponse(content=missing_file_html, media_type="text/html")
    finally:
        conn.close()


@app.get("/api/document/grid/{layout_hash}")
async def get_document_grid(layout_hash: str):
    """Parse Excel or CSV into structured JSON grid data for responsive table rendering."""
    conn = db.get_db_connection()
    try:
        hist_row = conn.execute(
            "SELECT filename FROM extraction_history WHERE layout_hash = ? ORDER BY processed_at DESC LIMIT 1;",
            (layout_hash,)
        ).fetchone()
        
        filename = hist_row["filename"] if hist_row else None
        
        # Seeded demo mock data if file not found or if it's our default signature
        if (not filename and layout_hash == "a1f998485a91d5") or layout_hash == "seeded_demo":
            return {
                "columns": ["Invoice No", "Date", "Customer Name", "Total Amount", "Status"],
                "rows": [
                    ["INV-8827931", "2026-05-19", "Joel Wood", "$495.00", "Pending"],
                    ["INV-8827932", "2026-05-20", "Alice Smith", "$1,250.00", "Paid"],
                    ["INV-8827933", "2026-05-21", "Bob Vance", "$3,140.00", "Overdue"]
                ]
            }
            
        if not filename:
            raise HTTPException(status_code=404, detail="No source document found for this layout signature.")
            
        attachments_dir = Path(__file__).parent.parent / "data" / "attachments"
        
        for f in attachments_dir.glob("**/*"):
            if f.name.lower() == filename.lower() and f.is_file():
                ext = f.suffix.lower()
                try:
                    if ext == ".csv":
                        df = pd.read_csv(f)
                    elif ext in [".xlsx", ".xls"]:
                        df = pd.read_excel(f)
                    else:
                        raise HTTPException(status_code=400, detail="Grid views are only supported for Excel and CSV formats.")
                    
                    df = df.fillna("")
                    columns = list(df.columns)
                    rows = df.values.tolist()
                    
                    return {
                        "columns": columns,
                        "rows": rows
                    }
                except Exception as e:
                    raise HTTPException(status_code=500, detail=f"Failed to parse spreadsheet file: {e}")
                    
        # Seeded mock data fallback if file isn't on disk (for easier local developer testing)
        return {
            "columns": ["Invoice No", "Date", "Customer Name", "Total Amount", "Status"],
            "rows": [
                ["INV-8827931", "2026-05-19", "Joel Wood", "$495.00", "Pending"],
                ["INV-8827932", "2026-05-20", "Alice Smith", "$1,250.00", "Paid"],
                ["INV-8827933", "2026-05-21", "Bob Vance", "$3,140.00", "Overdue"]
            ]
        }
    finally:
        conn.close()


if __name__ == "__main__":
    import uvicorn
    # Seed DB with dummy data if empty so operator sees beautiful logs
    conn = db.get_db_connection()
    try:
        count = conn.execute("SELECT COUNT(*) as count FROM extraction_history;").fetchone()["count"]
        if count == 0:
            print("Seeding SQLite database with initial metrics...")
            db.save_layout_rule(
                layout_hash="a1f998485a91d5",
                supplier_name="Acme Energy Corporation",
                document_type="pdf",
                status="pending_approval",
                extraction_rules={
                    "required_fields": ["invoice_number", "total_amount"],
                    "fields": {
                        "invoice_number": {
                            "anchor_keyword": "invoice number:",
                            "search_direction": "relative_right",
                            "window_characters": 80,
                            "regex_pattern": "([A-Z0-9-]+)"
                        },
                        "total_amount": {
                            "anchor_keyword": "total due:",
                            "search_direction": "relative_right",
                            "window_characters": 60,
                            "regex_pattern": "([0-9.,$]+)"
                        }
                    }
                }
            )
            # Log some test history
            hist_id = db.log_extraction(
                message_id="msg_dummy_1",
                filename="invoice_acme.pdf",
                layout_hash="a1f998485a91d5",
                parsed_data={"invoice_number": "INV-8827931", "total_amount": 495.00},
                status="success"
            )
    finally:
        conn.close()

    print("Starting FastAPI Operator Dashboard on http://127.0.0.1:8000")
    uvicorn.run("dashboard.main:app", host="127.0.0.1", port=8000, reload=True)
