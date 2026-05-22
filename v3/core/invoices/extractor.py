"""
Shared invoice extraction layer.

Covers:
  - Data contracts (dataclasses)
  - SQLite template memory store
  - PDF rasterizer (pdfplumber + PyMuPDF + PaddleOCR fallback)
  - Merge / validator / staging (validate_and_stage)
"""

import os
import io
import json
import base64
import sqlite3
import difflib
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

# Suppress oneDNN dynamic compilation warnings from PaddleOCR's CPU backend
os.environ["FLAGS_use_onednn"] = "0"

# Resolved path to the v3/ root directory (this file lives at v3/core/invoices/)
_V3_ROOT = Path(__file__).resolve().parent.parent.parent

# ==============================================================================
# SECTION 1: SHARED DATA CONTRACT (DATACLASSES)
# ==============================================================================

@dataclass
class FieldValue:
    value:      Optional[str] = None
    confidence: str = "low"    # "high" | "medium" | "low"
    source:     str = "llm"    # "llm" | "email" | "memory" | "human"

@dataclass
class CustomerInfo:
    name:    FieldValue = field(default_factory=FieldValue)
    email:   FieldValue = field(default_factory=FieldValue)
    phone:   FieldValue = field(default_factory=FieldValue)
    abn:     FieldValue = field(default_factory=FieldValue)
    address: FieldValue = field(default_factory=FieldValue)

@dataclass
class LineItem:
    line_number: int             = 0
    sku:         Optional[str]   = None
    description: str             = ""
    quantity:    Optional[float] = None
    uom:         Optional[str]   = None
    unit_price:  Optional[float] = None
    line_total:  Optional[float] = None
    confidence:  str             = "high"

@dataclass
class InvoiceTotals:
    subtotal: Optional[float] = None
    tax:      Optional[float] = None
    total:    Optional[float] = None

@dataclass
class InvoicePayload:
    source_file:      str          = ""
    source_type:      str          = ""   # "pdf_text" | "pdf_scanned" | "pdf_mixed"
    customer:         CustomerInfo = field(default_factory=CustomerInfo)
    line_items:       list[LineItem]  = field(default_factory=list)
    totals:           InvoiceTotals   = field(default_factory=InvoiceTotals)
    parse_confidence: str          = "low"
    warnings:         list[str]    = field(default_factory=list)
    needs_review:     bool         = False
    review_fields:    list[str]    = field(default_factory=list)
    staging_path:     str          = ""

@dataclass
class PageData:
    page_number:  int
    page_type:    str   # "text" | "image"
    text_content: str   # pdfplumber text (may be empty for scanned pages)
    paddle_text:  str   # PaddleOCR text (empty for native-text pages)
    image_b64:    str   # base64-encoded JPEG

@dataclass
class RasterisedPDF:
    source_file: str
    source_type: str   # "pdf_text" | "pdf_scanned" | "pdf_mixed"
    page_count:  int
    pages:       list[PageData]

# ==============================================================================
# SECTION 2: SQLITE MEMORY STORE
# ==============================================================================

DB_PATH = _V3_ROOT / "invoice_memory.db"

def get_db_connection():
    """Return a SQLite connection with dict-like row access."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Create the customer_templates table if it does not already exist."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS customer_templates (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            lookup_key       TEXT NOT NULL UNIQUE,
            customer_name    TEXT,
            customer_email   TEXT,
            customer_phone   TEXT,
            customer_abn     TEXT,
            customer_address TEXT,
            confirmed_count  INTEGER DEFAULT 0,
            created_at       TEXT DEFAULT (datetime('now')),
            updated_at       TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    conn.close()

# Initialize on module import (idempotent)
init_db()

def get_template(lookup_key: str) -> dict | None:
    """Return stored customer dict for the normalised lookup_key, or None."""
    if not lookup_key:
        return None
    normalized_key = lookup_key.lower().strip()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM customer_templates WHERE lookup_key = ?",
        (normalized_key,)
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def save_template(lookup_key: str, customer: CustomerInfo) -> None:
    """Upsert customer template. Increments confirmed_count on each save."""
    if not lookup_key:
        return
    normalized_key = lookup_key.lower().strip()
    updates = {}
    if customer.name.value is not None:
        updates["customer_name"] = customer.name.value
    if customer.email.value is not None:
        updates["customer_email"] = customer.email.value
    if customer.phone.value is not None:
        updates["customer_phone"] = customer.phone.value
    if customer.abn.value is not None:
        updates["customer_abn"] = customer.abn.value
    if customer.address.value is not None:
        updates["customer_address"] = customer.address.value

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM customer_templates WHERE lookup_key = ?", (normalized_key,))
        row = cursor.fetchone()
        if row:
            set_clauses = ["confirmed_count = confirmed_count + 1", "updated_at = datetime('now')"]
            params = []
            for col, val in updates.items():
                set_clauses.append(f"{col} = ?")
                params.append(val)
            params.append(normalized_key)
            cursor.execute(
                f"UPDATE customer_templates SET {', '.join(set_clauses)} WHERE lookup_key = ?",
                params
            )
        else:
            columns = ["lookup_key", "confirmed_count"]
            placeholders = ["?", "?"]
            params = [normalized_key, 1]
            for col, val in updates.items():
                columns.append(col)
                placeholders.append("?")
                params.append(val)
            cursor.execute(
                f"INSERT INTO customer_templates ({', '.join(columns)}) VALUES ({', '.join(placeholders)})",
                params
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def list_templates() -> list[dict]:
    """Return all customer_templates rows as dicts (for CLI/debugging)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM customer_templates")
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

# ==============================================================================
# SECTION 3: PDF RASTERIZER
# ==============================================================================

def rasterise(filepath: str) -> RasterisedPDF:
    """Open a PDF, classify each page, rasterize at appropriate DPI,
    run PaddleOCR on scanned pages, and return structured page data.
    """
    # Deferred imports to keep startup fast when only the DB layer is needed
    import pdfplumber
    import fitz        # PyMuPDF — rasterizes without poppler binaries
    import numpy as np
    from PIL import Image

    if not os.path.exists(filepath):
        raise FileNotFoundError(f"File not found: {filepath}")

    pages_data = []
    has_text = False
    has_image = False
    ocr_model = None

    print(f"Opening PDF: {filepath}")
    with pdfplumber.open(filepath) as pdf:
        page_count = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            page_num = i + 1
            text = page.extract_text() or ""
            clean_text = "".join(text.split())

            # Pages with >80 non-whitespace chars are classified as native-text
            if len(clean_text) > 80:
                page_type = "text"
                has_text = True
                dpi = 150
            else:
                page_type = "image"
                has_image = True
                dpi = 300

            # Rasterize with fitz (no poppler dependency)
            try:
                doc = fitz.open(filepath)
                fitz_page = doc.load_page(i)
                pix = fitz_page.get_pixmap(dpi=dpi)
                pillow_image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                doc.close()
            except Exception as e:
                raise RuntimeError(f"Failed to rasterize page {page_num}: {e}")

            # PaddleOCR pre-scan for scanned pages
            paddle_text = ""
            if page_type == "image":
                print(f"Page {page_num} classified as scanned IMAGE. Running PaddleOCR...")
                try:
                    from paddleocr import PaddleOCR
                    if ocr_model is None:
                        ocr_model = PaddleOCR(lang='en')
                    numpy_image = np.array(pillow_image.convert("RGB"))
                    ocr_result = ocr_model.ocr(numpy_image)
                    lines = []
                    if ocr_result and isinstance(ocr_result, list):
                        for block in ocr_result:
                            if block:
                                for line in block:
                                    if line and len(line) > 1 and len(line[1]) > 0:
                                        lines.append(str(line[1][0]))
                    paddle_text = "\n".join(lines)
                except Exception as e:
                    print(f"Warning: PaddleOCR pre-scan failed on page {page_num}: {e}")
                    paddle_text = ""

            # Base64 JPEG encode for vision model / storage
            buffered = io.BytesIO()
            pillow_image.convert("RGB").save(buffered, format="JPEG")
            image_b64 = base64.b64encode(buffered.getvalue()).decode("utf-8")

            pages_data.append(PageData(
                page_number=page_num,
                page_type=page_type,
                text_content=text,
                paddle_text=paddle_text,
                image_b64=image_b64
            ))

    if has_text and has_image:
        source_type = "pdf_mixed"
    elif has_text:
        source_type = "pdf_text"
    else:
        source_type = "pdf_scanned"

    return RasterisedPDF(
        source_file=filepath,
        source_type=source_type,
        page_count=page_count,
        pages=pages_data
    )

# ==============================================================================
# SECTION 5: MERGER & VALIDATOR
# ==============================================================================

FIELD_MAP = {
    "name":    "customer_name",
    "email":   "customer_email",
    "phone":   "customer_phone",
    "abn":     "customer_abn",
    "address": "customer_address",
}

def validate_and_stage(payload: InvoicePayload) -> InvoicePayload:
    """Enrich from template memory, compute confidence, run math checks,
    and write a JSON staging file to v3/invoice_staging/pending or approved/.
    """
    if payload.review_fields is None:
        payload.review_fields = []
    if payload.warnings is None:
        payload.warnings = []

    # Identify the lookup key (prefer email over name)
    lookup_key = ""
    if payload.customer.email.value:
        lookup_key = payload.customer.email.value
    elif payload.customer.name.value:
        lookup_key = payload.customer.name.value

    template = None
    if lookup_key:
        normalized_key = lookup_key.lower().strip()
        template = get_template(normalized_key)

        # Apply trusted template overrides when confirmed >= 2 times
        if template and template.get("confirmed_count", 0) >= 2:
            print(f"Memory Match Found: Using trusted template '{normalized_key}'")
            for payload_field, db_field in FIELD_MAP.items():
                field_obj = getattr(payload.customer, payload_field)
                mem_val = template.get(db_field)
                if mem_val is not None:
                    mem_val = str(mem_val).strip()

                if field_obj.confidence == "low" and mem_val:
                    field_obj.value = mem_val
                    field_obj.source = "memory"
                    field_obj.confidence = "high"
                elif field_obj.value and mem_val:
                    ratio = difflib.SequenceMatcher(
                        None,
                        str(field_obj.value).lower().strip(),
                        mem_val.lower().strip()
                    ).ratio()
                    if ratio < 0.8:
                        payload.warnings.append(
                            f"Field '{payload_field}' differs from memory — flagged for review"
                        )
                        if payload_field not in payload.review_fields:
                            payload.review_fields.append(payload_field)

    # Confidence scoring
    confidence = "high"
    for f_name in FIELD_MAP.keys():
        f_obj = getattr(payload.customer, f_name)
        if f_obj.confidence == "low" and f_obj.source != "memory":
            confidence = "medium"
    for item in payload.line_items:
        if item.confidence == "low":
            confidence = "medium"
    if payload.source_type == "pdf_scanned" and not template:
        if confidence == "high":
            confidence = "medium"
    if len(payload.line_items) == 0:
        confidence = "low"
    if payload.customer.name.value is None:
        confidence = "low"
    payload.parse_confidence = confidence

    # Math validation
    if payload.totals.total is not None:
        line_sum = sum(
            item.line_total for item in payload.line_items if item.line_total is not None
        )
        expected_subtotal = payload.totals.total
        if payload.totals.subtotal is not None:
            expected_subtotal = payload.totals.subtotal
        elif payload.totals.tax is not None:
            expected_subtotal = payload.totals.total - payload.totals.tax
        if abs(line_sum - expected_subtotal) > 0.10:
            payload.warnings.append(
                f"Line total mismatch: sum={line_sum:.2f} vs expected subtotal={expected_subtotal:.2f} "
                f"(invoice total={payload.totals.total:.2f})"
            )

    for item in payload.line_items:
        if item.quantity is not None and item.quantity < 0:
            payload.warnings.append(f"Line item {item.line_number} has negative quantity: {item.quantity}")
            if "line_items" not in payload.review_fields:
                payload.review_fields.append("line_items")
        if item.unit_price is not None and item.unit_price < 0:
            payload.warnings.append(f"Line item {item.line_number} has negative unit price: {item.unit_price}")
            if "line_items" not in payload.review_fields:
                payload.review_fields.append("line_items")
        if not item.description or not item.description.strip():
            payload.warnings.append(f"Line item {item.line_number} has empty description")
            if "line_items" not in payload.review_fields:
                payload.review_fields.append("line_items")

    payload.needs_review = bool(
        payload.parse_confidence == "low"
        or payload.review_fields
        or payload.warnings
        or payload.customer.name.value is None
    )

    # Write staging JSON
    base_staging = _V3_ROOT / "invoice_staging"
    pending_dir = base_staging / "pending"
    approved_dir = base_staging / "approved"
    pending_dir.mkdir(parents=True, exist_ok=True)
    approved_dir.mkdir(parents=True, exist_ok=True)

    file_stem = Path(payload.source_file).stem if payload.source_file else "unknown_invoice"
    json_filename = f"{file_stem}.json"
    target_path = pending_dir / json_filename if payload.needs_review else approved_dir / json_filename
    target_path.write_text(json.dumps(asdict(payload), indent=2), encoding="utf-8")
    payload.staging_path = str(target_path.resolve())

    return payload
