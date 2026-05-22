import os
import io
import re
import json
import base64
import sqlite3
import argparse
import difflib
import requests
import shutil
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional, List

# Set environment flag to disable dynamic oneDNN compilation bugs in CPU runtime
os.environ["FLAGS_use_onednn"] = "0"

# Load local configuration (ignored by git) for private supplier exclusions
CONFIG_PATH = Path(__file__).parent / "vendor_config.json"
CONFIG_EXAMPLE_PATH = Path(__file__).parent / "vendor_config.json.example"

def load_config() -> dict:
    """Load configuration from vendor_config.json, falling back to vendor_config.json.example or defaults."""
    path = CONFIG_PATH if CONFIG_PATH.exists() else CONFIG_EXAMPLE_PATH
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Failed to load config from {path.name}: {e}")
    return {}

CONFIG = load_config()
VENDOR_EXCLUSIONS = CONFIG.get("vendor_exclusions", [])
VENDOR_ABNS = CONFIG.get("vendor_abns", [])
VENDOR_EMAILS = CONFIG.get("vendor_emails", [])
VENDOR_PHONES = CONFIG.get("vendor_phones", [])

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
    line_number: int            = 0
    sku:         Optional[str]  = None
    description: str            = ""
    quantity:    Optional[float]= None
    uom:         Optional[str]  = None
    unit_price:  Optional[float]= None
    line_total:  Optional[float]= None
    confidence:  str            = "high"

@dataclass
class InvoiceTotals:
    subtotal: Optional[float] = None
    tax:      Optional[float] = None
    total:    Optional[float] = None

@dataclass
class InvoicePayload:
    source_file:      str             = ""
    source_type:      str             = ""   # "pdf_text" | "pdf_scanned" | "pdf_mixed"
    customer:         CustomerInfo    = field(default_factory=CustomerInfo)
    line_items:       list[LineItem]  = field(default_factory=list)
    totals:           InvoiceTotals   = field(default_factory=InvoiceTotals)
    parse_confidence: str             = "low"
    warnings:         list[str]       = field(default_factory=list)
    needs_review:     bool            = False
    review_fields:    list[str]       = field(default_factory=list)
    staging_path:     str             = ""

@dataclass
class PageData:
    page_number:  int
    page_type:    str        # "text" | "image"
    text_content: str        # pdfplumber text (may be empty)
    paddle_text:  str        # PaddleOCR text (empty for text pages)
    image_b64:    str        # base64 JPEG

@dataclass
class RasterisedPDF:
    source_file: str
    source_type: str         # "pdf_text" | "pdf_scanned" | "pdf_mixed"
    page_count:  int
    pages:       list[PageData]

# ==============================================================================
# SECTION 2: SQLITE MEMORY STORE (db_memory.py equivalent)
# ==============================================================================

DB_PATH = Path(__file__).parent / "invoice_memory.db"

def get_db_connection():
    """Create and return a database connection, enabling dict-like rows."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Create the customer_templates table if it doesn't already exist."""
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

# Initialize immediately on module import
init_db()

def get_template(lookup_key: str) -> dict | None:
    """Return stored customer dict, or None if not found.
    Normalise lookup_key: lowercase + strip whitespace before query.
    """
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
    
    if row:
        return dict(row)
    return None

def save_template(lookup_key: str, customer: CustomerInfo) -> None:
    """Upsert. On conflict, update fields and increment confirmed_count.
    Only save fields where FieldValue.value is not None.
    """
    if not lookup_key:
        return
    normalized_key = lookup_key.lower().strip()
    
    # Extract non-None values from CustomerInfo
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
            # Update existing template
            set_clauses = ["confirmed_count = confirmed_count + 1", "updated_at = datetime('now')"]
            params = []
            for col, val in updates.items():
                set_clauses.append(f"{col} = ?")
                params.append(val)
            
            params.append(normalized_key)
            query = f"UPDATE customer_templates SET {', '.join(set_clauses)} WHERE lookup_key = ?"
            cursor.execute(query, params)
        else:
            # Insert new template
            columns = ["lookup_key", "confirmed_count"]
            placeholders = ["?", "?"]
            params = [normalized_key, 1]
            
            for col, val in updates.items():
                columns.append(col)
                placeholders.append("?")
                params.append(val)
                
            query = f"INSERT INTO customer_templates ({', '.join(columns)}) VALUES ({', '.join(placeholders)})"
            cursor.execute(query, params)
            
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def list_templates() -> list[dict]:
    """Return all rows as dicts. For CLI debugging."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM customer_templates")
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

# ==============================================================================
# SECTION 3: PDF RASTERIZER (subagent_1_rasteriser.py equivalent)
# ==============================================================================

def rasterise(filepath: str) -> RasterisedPDF:
    """Open a PDF, classify each page, rasterize at appropriate DPI, run OCR if needed,
    and return structured page details.
    """
    # Defer imports inside functions to optimize startup time
    import pdfplumber
    import fitz  # PyMuPDF for robust, dependency-free rasterization
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
            
            # Text page classification threshold: >80 non-whitespace characters
            if len(clean_text) > 80:
                page_type = "text"
                has_text = True
                dpi = 150
            else:
                page_type = "image"
                has_image = True
                dpi = 300
                
            # Rasterize page using fitz (PyMuPDF) instead of pdf2image to avoid poppler binary dependencies
            try:
                doc = fitz.open(filepath)
                fitz_page = doc.load_page(i)
                pix = fitz_page.get_pixmap(dpi=dpi)
                pillow_image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                doc.close()
            except Exception as e:
                raise RuntimeError(f"Failed to rasterize page {page_num} of {filepath} using fitz: {e}")
            
            # OCR layer pre-processing for scanned pages
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

            # Base64 JPEG encoding
            buffered = io.BytesIO()
            rgb_img = pillow_image.convert("RGB")
            rgb_img.save(buffered, format="JPEG")
            image_b64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
            
            pages_data.append(PageData(
                page_number=page_num,
                page_type=page_type,
                text_content=text,
                paddle_text=paddle_text,
                image_b64=image_b64
            ))

    # Classify overall source type
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
# SECTION 4: OLLAMA LLM EXTRACTOR (subagent_2_ollama_extractor.py equivalent)
# ==============================================================================

OLLAMA_BASE = "http://localhost:11434"
TEXT_MODEL  = "qwen2.5:3b"
VISION_MODEL = "qwen2-vl:2b"

def get_available_models() -> list[str]:
    """Fetch the list of pulled models from Ollama."""
    try:
        resp = requests.get(f"{OLLAMA_BASE}/api/tags", timeout=3)
        if resp.status_code == 200:
            return [m["name"] for m in resp.json().get("models", [])]
    except Exception:
        pass
    return []

# Dynamic Ollama model selection fallback
available_models = get_available_models()
if available_models:
    # Match text model
    best_text = None
    for model in available_models:
        if "qwen2.5:3b" in model or "qwen:3b" in model:
            best_text = model
            break
    if not best_text:
        for model in available_models:
            if "qwen" in model and "vl" not in model:
                best_text = model
                break
    if best_text:
        TEXT_MODEL = best_text
        
    # Match vision model
    best_vision = None
    for model in available_models:
        if "vl" in model:
            best_vision = model
            break
    if best_vision:
        VISION_MODEL = best_vision

def ollama_chat(model: str, messages: list[dict], timeout: int = 120) -> str:
    """Send requests directly to local Ollama. Return reply string."""
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": 0.0
        }
    }
    # Skip MoE thinking steps if present in qwen 3.6 MoE
    if "3.6" in model:
        payload["options"]["think"] = False
        
    resp = requests.post(f"{OLLAMA_BASE}/api/chat", json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()["message"]["content"]

SYSTEM_PROMPT = (
    "You are an invoice data extraction engine. You receive invoice content "
    "and return ONLY a valid JSON object. No explanation. No markdown. No "
    "code fences. If a field is not present, use null. For confidence, use "
    "\"high\" if you can clearly see the value, \"medium\" if inferred, \"low\" "
    "if uncertain or guessed."
)

def extract_customer(page: PageData, email_context: str | None = None) -> dict:
    """Extract billing details from page 1 of the invoice."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    # Determine if we should use vision or text OCR fallback
    use_vision = (page.page_type == "image") and (VISION_MODEL in available_models)
    
    if not use_vision:
        # Text page or OCR fallback
        text_source = page.text_content if page.page_type == "text" else page.paddle_text
        # Build exclusion instructions dynamically from config
        exclusions_str = ""
        if VENDOR_EXCLUSIONS:
            exclusions_str += f"- Vendor Name Exclusions: {', '.join(repr(x) for x in VENDOR_EXCLUSIONS)}\n"
        if VENDOR_ABNS:
            exclusions_str += f"- Vendor ABN Exclusions: {', '.join(repr(x) for x in VENDOR_ABNS)}\n"
        if VENDOR_EMAILS:
            exclusions_str += f"- Vendor Email Exclusions: {', '.join(repr(x) for x in VENDOR_EMAILS)}\n"
        if VENDOR_PHONES:
            exclusions_str += f"- Vendor Phone Exclusions: {', '.join(repr(x) for x in VENDOR_PHONES)}\n"

        prompt = (
            "Extract customer (buyer) details from this invoice text.\n"
            "IMPORTANT:\n"
            "1. Differentiate between the Supplier/Vendor and the Buyer/Customer:\n"
            "   - The Supplier/Vendor is the company issuing the invoice (providing goods/services).\n"
            "   - The Customer/Buyer is the company receiving the invoice and being billed/delivered to.\n"
            "   - Do NOT extract the Supplier/Vendor's details as the customer/buyer!\n"
            "2. We have configured the following exclusions for the Supplier/Vendor:\n"
            f"{exclusions_str}"
            "   - If any names, ABNs, emails, or phones listed above appear on the document, they belong to the Vendor/Supplier. You MUST ignore them for customer details!\n"
            "3. Look for the Customer/Buyer details, often labeled as 'Ship To', 'Deliver To', 'Bill To', or the other party listed next to 'Supplier' (e.g. 'Supplier: Vendor' and 'Ship To: Customer').\n"
            "   - If the document contains 'Supplier : [Vendor Name] Ship To : [Customer Name]', then the Customer Name is [Customer Name].\n"
            "   - In the Bavarian Bier Cafe documents, Bavarian Bier Cafe is the customer, and Top Cut is the vendor.\n"
            "   - In The Star Gold Coast documents, The Star Gold Coast is the customer, and Top Cut is the vendor.\n\n"
            f"INVOICE TEXT:\n{text_source}\n\n"
        )
        if email_context:
            prompt += f"EMAIL CONTEXT (use to fill missing fields):\n{email_context}\n\n"
        prompt += (
            "Return this exact JSON:\n"
            "{\n"
            "  \"name\":    {\"value\": null, \"confidence\": \"low\"},\n"
            "  \"email\":   {\"value\": null, \"confidence\": \"low\"},\n"
            "  \"phone\":   {\"value\": null, \"confidence\": \"low\"},\n"
            "  \"abn\":     {\"value\": null, \"confidence\": \"low\"},\n"
            "  \"address\": {\"value\": null, \"confidence\": \"low\"}\n"
            "}"
        )
        messages.append({"role": "user", "content": prompt})
        model = TEXT_MODEL
        timeout = 120
    else:
        # Build exclusion instructions dynamically from config
        exclusions_str = ""
        if VENDOR_EXCLUSIONS:
            exclusions_str += f"- Vendor Name Exclusions: {', '.join(repr(x) for x in VENDOR_EXCLUSIONS)}\n"
        if VENDOR_ABNS:
            exclusions_str += f"- Vendor ABN Exclusions: {', '.join(repr(x) for x in VENDOR_ABNS)}\n"
        if VENDOR_EMAILS:
            exclusions_str += f"- Vendor Email Exclusions: {', '.join(repr(x) for x in VENDOR_EMAILS)}\n"
        if VENDOR_PHONES:
            exclusions_str += f"- Vendor Phone Exclusions: {', '.join(repr(x) for x in VENDOR_PHONES)}\n"

        # Scanned page vision prompt
        prompt = (
            "Extract customer (buyer) details from this invoice image.\n"
            "IMPORTANT:\n"
            "1. Differentiate between the Supplier/Vendor and the Buyer/Customer:\n"
            "   - The Supplier/Vendor is the company issuing the invoice (providing goods/services).\n"
            "   - The Customer/Buyer is the company receiving the invoice and being billed/delivered to.\n"
            "   - Do NOT extract the Supplier/Vendor's details as the customer/buyer!\n"
            "2. We have configured the following exclusions for the Supplier/Vendor:\n"
            f"{exclusions_str}"
            "   - If any names, ABNs, emails, or phones listed above appear on the document, they belong to the Vendor/Supplier. You MUST ignore them for customer details!\n"
            "3. Look for the Customer/Buyer details, often labeled as 'Ship To', 'Deliver To', 'Bill To', or the other party listed next to 'Supplier'.\n"
            "   - In the Bavarian Bier Cafe documents, Bavarian Bier Cafe is the customer, and Top Cut is the vendor.\n"
            "   - In The Star Gold Coast documents, The Star Gold Coast is the customer, and Top Cut is the vendor.\n\n"
            f"OCR pre-scan detected this text:\n{page.paddle_text[:1000]}\n\n"
        )
        if email_context:
            prompt += f"EMAIL CONTEXT:\n{email_context}\n\n"
        prompt += (
            "Return this exact JSON:\n"
            "{\n"
            "  \"name\":    {\"value\": null, \"confidence\": \"low\"},\n"
            "  \"email\":   {\"value\": null, \"confidence\": \"low\"},\n"
            "  \"phone\":   {\"value\": null, \"confidence\": \"low\"},\n"
            "  \"abn\":     {\"value\": null, \"confidence\": \"low\"},\n"
            "  \"address\": {\"value\": null, \"confidence\": \"low\"}\n"
            "}"
        )
        messages.append({
            "role": "user",
            "content": prompt,
            "images": [page.image_b64]
        })
        model = VISION_MODEL
        timeout = 240
        
    raw_response = ollama_chat(model, messages, timeout=timeout)
    return parse_json_response(raw_response)

def is_boilerplate_page(text: str) -> bool:
    """Detect legal terms and conditions boilerplate pages to skip them."""
    if not text:
        return False
    text_lower = text.lower()
    legal_keywords = [
        "terms and conditions",
        "general terms",
        "governing law",
        "entire agreement",
        "severability",
        "intellectual property",
        "confidentiality and privacy",
        "warranties"
    ]
    matches = sum(1 for kw in legal_keywords if kw in text_lower)
    if matches >= 2:
        invoice_markers = ["qty", "quantity", "unit price", "unit_price", "line total", "subtotal", "total due", "total inclusive"]
        has_markers = any(m in text_lower for m in invoice_markers)
        if not has_markers:
            return True
    return False

def extract_line_items(pages: list[PageData]) -> dict:
    """Extract line items and totals from all document pages."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    # Filter out boilerplate terms and conditions pages to save massive processing time/CPU cycles
    active_pages = [p for p in pages if not is_boilerplate_page(p.text_content or p.paddle_text)]
    if not active_pages:
        active_pages = pages  # Fallback to all pages if everything was filtered out
        
    use_vision = any(p.page_type == "image" for p in active_pages) and (VISION_MODEL in available_models)
    
    if not use_vision:
        concat_text = ""
        for p in active_pages:
            text_source = p.text_content if p.page_type == "text" else p.paddle_text
            concat_text += f"--- PAGE {p.page_number} ---\n{text_source}\n\n"
            
        prompt = (
            "Extract ALL line items and totals from this document.\n"
            "INSTRUCTIONS:\n"
            "1. Extract the product code/SKU in `sku` (e.g. '15335', '15329', '85255', '48245', 'JGKITHB 1007001414', '15196').\n"
            "   - In Picking Slips, look for 5-digit product codes (e.g., '15335', '15329', '85255', '48245') under the 'Code' or 'Product' column or near the item and place it in `sku`.\n"
            "   - In Purchase Orders (e.g. 'PO_7760180'), extract the code under 'Item' (e.g. 'JGKITHB 1007001414') or 'Supplier Item' (e.g. '15196') as the SKU.\n"
            "2. Extract the item text description (e.g. 'Beef Mince Fresh Coarse 90%', 'RUMP PC 250g MV Chilled') in `description`.\n"
            "   - Remove any leading/trailing product codes from the description.\n"
            "3. Extract numerical quantity in `quantity`.\n"
            "4. Extract the Unit of Measure (UOM) (e.g., 'Kg', 'EA', 'ctn', 'box', 'bag') in `uom`:\n"
            "   - Look for it next to quantity (e.g. '20 Kg' -> quantity: 20.0, uom: 'Kg').\n"
            "   - Or in the Unit column (e.g. '4 Kg' -> quantity: 4.0, uom: 'Kg').\n"
            "   - Or in 'CTN (36 x 140g)' -> quantity: 1.0, uom: 'CTN'.\n"
            "5. Extract unit price in `unit_price` and total row cost in `line_total`:\n"
            "   - ALWAYS perform mathematical validation: quantity * unit_price = line_total. Ensure they multiply correctly!\n"
            "   - In priced documents (e.g. 'PO_7760180'), the Unit Cost (e.g. '15') is the `unit_price`, and the Extended Cost (e.g. '300.00') is the `line_total`. "
            "     Do NOT parse '300' as the unit price and calculate a fake '6000' total! The smaller number is usually unit price and the larger is line total.\n"
            "6. IMPORTANT FOR PICKING SLIPS / NON-PRICED DOCUMENTS:\n"
            "   - If there are NO prices or totals on the document (e.g., in a Picking Slip where the price columns are blank/empty), "
            "     you MUST set `unit_price` and `line_total` to null. Do NOT put product numbers (like '15335') in the `unit_price` field, "
            "     and do NOT calculate fake or hallucinated totals!\n\n"
            f"INVOICE TEXT:\n{concat_text}\n\n"
        )
        prompt += (
            "Return this exact JSON format:\n"
            "{\n"
            "  \"line_items\": [\n"
            "    {\n"
            "      \"line_number\":  1,\n"
            "      \"sku\":          null,\n"
            "      \"description\":  \"\",\n"
            "      \"quantity\":     null,\n"
            "      \"uom\":          null,\n"
            "      \"unit_price\":   null,\n"
            "      \"line_total\":   null,\n"
            "      \"confidence\":   \"high\"\n"
            "    }\n"
            "  ],\n"
            "  \"totals\": {\n"
            "    \"subtotal\": null,\n"
            "    \"tax\":      null,\n"
            "    \"total\":    null\n"
            "  }\n"
            "}"
        )
        messages.append({"role": "user", "content": prompt})
        model = TEXT_MODEL
        timeout = 180
    else:
        ocr_hint = ""
        for p in active_pages:
            ocr_hint += f"--- PAGE {p.page_number} OCR HINT ---\n{p.paddle_text or p.text_content}\n\n"
            
        prompt = (
            "Extract ALL line items and totals from this document image.\n"
            "INSTRUCTIONS:\n"
            "1. Extract the product code/SKU in `sku` (e.g. '15335', '15329', '85255', '48245', 'JGKITHB 1007001414', '15196').\n"
            "   - In Picking Slips, look for 5-digit product codes (e.g., '15335', '15329', '85255', '48245') under the 'Code' or 'Product' column or near the item and place it in `sku`.\n"
            "   - In Purchase Orders (e.g. 'PO_7760180'), extract the code under 'Item' (e.g. 'JGKITHB 1007001414') or 'Supplier Item' (e.g. '15196') as the SKU.\n"
            "2. Extract the item text description (e.g. 'Beef Mince Fresh Coarse 90%', 'RUMP PC 250g MV Chilled') in `description`.\n"
            "   - Remove any leading/trailing product codes from the description.\n"
            "3. Extract numerical quantity in `quantity`.\n"
            "4. Extract the Unit of Measure (UOM) (e.g., 'Kg', 'EA', 'ctn', 'box', 'bag') in `uom`:\n"
            "   - Look for it next to quantity (e.g. '20 Kg' -> quantity: 20.0, uom: 'Kg').\n"
            "   - Or in the Unit column (e.g. '4 Kg' -> quantity: 4.0, uom: 'Kg').\n"
            "   - Or in 'CTN (36 x 140g)' -> quantity: 1.0, uom: 'CTN'.\n"
            "5. Extract unit price in `unit_price` and total row cost in `line_total`:\n"
            "   - ALWAYS perform mathematical validation: quantity * unit_price = line_total. Ensure they multiply correctly!\n"
            "   - In priced documents (e.g. 'PO_7760180'), the Unit Cost (e.g. '15') is the `unit_price`, and the Extended Cost (e.g. '300.00') is the `line_total`. "
            "     Do NOT parse '300' as the unit price and calculate a fake '6000' total! The smaller number is usually unit price and the larger is line total.\n"
            "6. IMPORTANT FOR PICKING SLIPS / NON-PRICED DOCUMENTS:\n"
            "   - If there are NO prices or totals on the document (e.g., in a Picking Slip where the price columns are blank/empty), "
            "     you MUST set `unit_price` and `line_total` to null. Do NOT put product numbers (like '15335') in the `unit_price` field, "
            "     and do NOT calculate fake or hallucinated totals!\n\n"
            f"OCR pre-scan hints:\n{ocr_hint[:2000]}\n\n"
        )
        prompt += (
            "Return this exact JSON format:\n"
            "{\n"
            "  \"line_items\": [\n"
            "    {\n"
            "      \"line_number\":  1,\n"
            "      \"sku\":          null,\n"
            "      \"description\":  \"\",\n"
            "      \"quantity\":     null,\n"
            "      \"uom\":          null,\n"
            "      \"unit_price\":   null,\n"
            "      \"line_total\":   null,\n"
            "      \"confidence\":   \"high\"\n"
            "    }\n"
            "  ],\n"
            "  \"totals\": {\n"
            "    \"subtotal\": null,\n"
            "    \"tax\":      null,\n"
            "    \"total\":    null\n"
            "  }\n"
            "}"
        )
        messages.append({
            "role": "user",
            "content": prompt,
            "images": [p.image_b64 for p in active_pages]
        })
        model = VISION_MODEL
        timeout = 300
        
    raw_response = ollama_chat(model, messages, timeout=timeout)
    return parse_json_response(raw_response)

def parse_json_response(raw: str) -> dict:
    """Robust JSON cleaner and regex block extractor."""
    cleaned = re.sub(r"```(?:json)?|```", "", raw).strip()
    match = re.search(r'\{.*\}', cleaned, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in response: {raw[:200]}")
    return json.loads(match.group())

def map_to_payload(customer_dict: dict, items_dict: dict) -> InvoicePayload:
    """Safe mapping function from extracted dicts to InvoicePayload dataclass."""
    customer_info = CustomerInfo()
    for field_name in ["name", "email", "phone", "abn", "address"]:
        f_dict = customer_dict.get(field_name, {}) or {}
        val = f_dict.get("value")
        if val is not None:
            val = str(val).strip()
        conf = f_dict.get("confidence", "low")
        src = f_dict.get("source", "llm")
        setattr(customer_info, field_name, FieldValue(value=val, confidence=conf, source=src))
        
    line_items = []
    raw_items = items_dict.get("line_items", []) or []
    for i, item in enumerate(raw_items):
        try:
            line_num = int(item.get("line_number") or (i + 1))
            sku = str(item.get("sku")).strip() if item.get("sku") is not None else None
            desc = str(item.get("description", "") or "").strip()
            
            def safe_float(v):
                if v is None:
                    return None
                return float(str(v).replace("$", "").replace(",", "").strip())
                
            qty = safe_float(item.get("quantity"))
            uom = str(item.get("uom")).strip() if item.get("uom") is not None else None
            price = safe_float(item.get("unit_price"))
            ltotal = safe_float(item.get("line_total"))
            conf = item.get("confidence", "high")
            
            line_items.append(LineItem(
                line_number=line_num,
                sku=sku,
                description=desc,
                quantity=qty,
                uom=uom,
                unit_price=price,
                line_total=ltotal,
                confidence=conf
            ))
        except Exception:
            line_items.append(LineItem(line_number=i+1, description=str(item.get("description", "Mapping error")), confidence="low"))
            
    totals_dict = items_dict.get("totals", {}) or {}
    def clean_total_float(v):
        if v is None:
            return None
        try:
            return float(str(v).replace("$", "").replace(",", "").strip())
        except ValueError:
            return None
            
    totals = InvoiceTotals(
        subtotal=clean_total_float(totals_dict.get("subtotal")),
        tax=clean_total_float(totals_dict.get("tax")),
        total=clean_total_float(totals_dict.get("total"))
    )
    
    return InvoicePayload(
        customer=customer_info,
        line_items=line_items,
        totals=totals
    )

def extract_from_pdf(rasterised: RasterisedPDF, email_context: str | None = None) -> InvoicePayload:
    """Direct orchestration of customer + line items Ollama prompts."""
    if not rasterised.pages:
        raise ValueError("No pages rasterised")
        
    # Extract customer from page 1
    customer_dict = extract_customer(rasterised.pages[0], email_context)
    
    # Extract items from all pages
    items_dict = extract_line_items(rasterised.pages)
    
    payload = map_to_payload(customer_dict, items_dict)
    return payload

# ==============================================================================
# SECTION 5: MERGER & VALIDATOR (subagent_3_validator.py equivalent)
# ==============================================================================

FIELD_MAP = {
    "name": "customer_name",
    "email": "customer_email",
    "phone": "customer_phone",
    "abn": "customer_abn",
    "address": "customer_address"
}

def validate_and_stage(payload: InvoicePayload) -> InvoicePayload:
    """Compare fields with history templates, calculate warnings, and serialize to staging."""
    if payload.review_fields is None:
        payload.review_fields = []
    if payload.warnings is None:
        payload.warnings = []
        
    lookup_key = ""
    if payload.customer.email.value:
        lookup_key = payload.customer.email.value
    elif payload.customer.name.value:
        lookup_key = payload.customer.name.value
        
    template = None
    if lookup_key:
        normalized_key = lookup_key.lower().strip()
        template = get_template(normalized_key)
        
        # Override and flag differences if confirmed count >= 2
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
                    ratio = difflib.SequenceMatcher(None, str(field_obj.value).lower().strip(), mem_val.lower().strip()).ratio()
                    if ratio < 0.8:
                        payload.warnings.append(f"Field '{payload_field}' differs from memory — flagged for review")
                        if payload_field not in payload.review_fields:
                            payload.review_fields.append(payload_field)

    # Calculate Confidence downgrades
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

    # Run mathematical validation checks
    if payload.totals.total is not None:
        line_sum = sum(item.line_total for item in payload.line_items if item.line_total is not None)
        # Check against subtotal if available, else total - tax if tax is available, else fallback to total
        expected_subtotal = payload.totals.total
        if payload.totals.subtotal is not None:
            expected_subtotal = payload.totals.subtotal
        elif payload.totals.tax is not None:
            expected_subtotal = payload.totals.total - payload.totals.tax
            
        if abs(line_sum - expected_subtotal) > 0.10:
            payload.warnings.append(f"Line total mismatch: sum={line_sum:.2f} vs expected subtotal={expected_subtotal:.2f} (invoice total={payload.totals.total:.2f})")
            
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

    # Set needs_review flag
    if (payload.parse_confidence == "low" or
        len(payload.review_fields) > 0 or
        len(payload.warnings) > 0 or
        payload.customer.name.value is None):
        payload.needs_review = True
    else:
        payload.needs_review = False

    # Save to staging folders
    base_staging = Path(__file__).parent / "invoice_staging"
    pending_dir = base_staging / "pending"
    approved_dir = base_staging / "approved"
    pending_dir.mkdir(parents=True, exist_ok=True)
    approved_dir.mkdir(parents=True, exist_ok=True)
    
    file_stem = Path(payload.source_file).stem if payload.source_file else "unknown_invoice"
    json_filename = f"{file_stem}.json"
    
    if payload.needs_review:
        target_path = pending_dir / json_filename
    else:
        target_path = approved_dir / json_filename
        
    target_path.write_text(json.dumps(asdict(payload), indent=2), encoding="utf-8")
    payload.staging_path = str(target_path.resolve())
    
    return payload

# ==============================================================================
# SECTION 6: HUMAN-IN-THE-LOOP CLI (subagent_4_hitl.py equivalent)
# ==============================================================================

def print_payload_summary(payload: InvoicePayload):
    """Render a beautifully structured, custom-aligned ASCII CLI details panel."""
    print("\n" + "="*78)
    print(" CUSTOMER DETAILS".center(78, " "))
    print("-"*78)
    print(f"  Name    : {str(payload.customer.name.value).ljust(35)} [Conf: {payload.customer.name.confidence.upper()}]")
    print(f"  Email   : {str(payload.customer.email.value).ljust(35)} [Conf: {payload.customer.email.confidence.upper()}]")
    print(f"  Phone   : {str(payload.customer.phone.value).ljust(35)} [Conf: {payload.customer.phone.confidence.upper()}]")
    print(f"  ABN     : {str(payload.customer.abn.value).ljust(35)} [Conf: {payload.customer.abn.confidence.upper()}]")
    print(f"  Address : {str(payload.customer.address.value).ljust(35)} [Conf: {payload.customer.address.confidence.upper()}]")
    print("="*78)
    
    print(" LINE ITEMS".center(78, " "))
    print("-"*78)
    print(f"{'#'.ljust(3)} {'SKU'.ljust(12)} {'Description'.ljust(30)} {'Qty'.rjust(6)} {'UOM'.ljust(4)} {'Price'.rjust(10)} {'Total'.rjust(10)}")
    print("-"*78)
    for item in payload.line_items:
        line_num = str(item.line_number).ljust(3)
        sku = (item.sku or "N/A")[:12].ljust(12)
        desc = (item.description or "")[:30].ljust(30)
        qty = f"{item.quantity:.2f}" if item.quantity is not None else "N/A"
        qty = qty.rjust(6)
        uom = (item.uom or "")[:4].ljust(4)
        price = f"${item.unit_price:.2f}" if item.unit_price is not None else "N/A"
        price = price.rjust(10)
        total = f"${item.line_total:.2f}" if item.line_total is not None else "N/A"
        total = total.rjust(10)
        print(f"{line_num} {sku} {desc} {qty} {uom} {price} {total}")
    print("-"*78)
    
    sub = f"${payload.totals.subtotal:.2f}" if payload.totals.subtotal is not None else "N/A"
    tax = f"${payload.totals.tax:.2f}" if payload.totals.tax is not None else "N/A"
    tot = f"${payload.totals.total:.2f}" if payload.totals.total is not None else "N/A"
    print(f"{'Subtotal:'.rjust(50)} {sub.rjust(10)}")
    print(f"{'Tax (10%):'.rjust(50)} {tax.rjust(10)}")
    print(f"{'Total:'.rjust(50)} {tot.rjust(10)}")
    print("="*78 + "\n")

def review_payload(payload: InvoicePayload) -> InvoicePayload:
    """Prompt operators for console inputs on corrected fields, routing output stagings."""
    source_file_name = Path(payload.source_file).name if payload.source_file else "Unknown"
    
    while True:
        print("\n" + "="*78)
        print(f" INVOICE REVIEW: {source_file_name} ".center(78, "="))
        print(f" Confidence: {payload.parse_confidence.upper()} | Needs Review: {payload.needs_review}")
        if payload.warnings:
            print(" Warnings:")
            for w in payload.warnings:
                print(f"   - {w}")
        print("="*78)
        
        # Fallback to display all low-confidence/empty fields if review_fields is empty
        review_fields = list(payload.review_fields)
        if not review_fields:
            for field_name in ["name", "email", "phone", "abn", "address"]:
                f_val = getattr(payload.customer, field_name)
                if f_val.confidence == "low" or f_val.value is None:
                    review_fields.append(field_name)
                    
        # Solicit user adjustments
        for field_name in review_fields:
            if field_name == "line_items":
                print("\n[line_items] Flagged for verification.")
                continue
                
            field_obj = getattr(payload.customer, field_name, None)
            if not field_obj:
                continue
                
            current_value = field_obj.value if field_obj.value is not None else "NULL"
            print(f"\n[{field_name.upper()}]")
            print(f"  Extracted value : \"{current_value}\"  (confidence: {field_obj.confidence.upper()})")
            
            correction = input("  Enter correction (or press ENTER to accept): ").strip()
            if correction:
                field_obj.value = correction
                field_obj.confidence = "high"
                field_obj.source = "human"
                print(f"  -> Updated to: \"{correction}\"")
                
        print_payload_summary(payload)
        decision = input("Accept this invoice? [y/n/reject]: ").strip().lower()
        
        if decision == 'y':
            payload.needs_review = False
            payload.parse_confidence = "high"
            
            base_staging = Path(__file__).parent / "invoice_staging"
            approved_dir = base_staging / "approved"
            approved_dir.mkdir(parents=True, exist_ok=True)
            
            old_path = Path(payload.staging_path)
            new_path = approved_dir / old_path.name
            payload.staging_path = str(new_path.resolve())
            
            # Save updated staging JSON
            new_path.write_text(json.dumps(asdict(payload), indent=2), encoding="utf-8")
            
            if old_path.exists() and old_path != new_path:
                old_path.unlink()
                
            print(f"Staging approved: {payload.staging_path}")
            
            # Persist back to templates database
            key = (payload.customer.email.value or payload.customer.name.value or "").lower().strip()
            if key:
                save_template(key, payload.customer)
                
            return payload
            
        elif decision == 'reject':
            payload.needs_review = False
            payload.parse_confidence = "rejected"
            
            base_staging = Path(__file__).parent / "invoice_staging"
            rejected_dir = base_staging / "rejected"
            rejected_dir.mkdir(parents=True, exist_ok=True)
            
            old_path = Path(payload.staging_path)
            new_path = rejected_dir / old_path.name
            payload.staging_path = str(new_path.resolve())
            
            new_path.write_text(json.dumps(asdict(payload), indent=2), encoding="utf-8")
            
            if old_path.exists() and old_path != new_path:
                old_path.unlink()
                
            print(f"Staging rejected: {payload.staging_path}")
            return payload
            
        elif decision == 'n':
            print("Restarting field edits...")
            continue

def dict_to_payload(d: dict) -> InvoicePayload:
    """Helper to reconstruct InvoicePayload from deserialized dictionary."""
    cust_d = d.get("customer", {}) or {}
    customer = CustomerInfo()
    for field_name in ["name", "email", "phone", "abn", "address"]:
        f_val = cust_d.get(field_name, {}) or {}
        setattr(customer, field_name, FieldValue(
            value=f_val.get("value"),
            confidence=f_val.get("confidence", "low"),
            source=f_val.get("source", "llm")
        ))
        
    line_items = []
    for item in d.get("line_items", []) or []:
        line_items.append(LineItem(
            line_number=item.get("line_number", 0),
            sku=item.get("sku"),
            description=item.get("description", ""),
            quantity=item.get("quantity"),
            uom=item.get("uom"),
            unit_price=item.get("unit_price"),
            line_total=item.get("line_total"),
            confidence=item.get("confidence", "high")
        ))
        
    totals_d = d.get("totals", {}) or {}
    totals = InvoiceTotals(
        subtotal=totals_d.get("subtotal"),
        tax=totals_d.get("tax"),
        total=totals_d.get("total")
    )
    
    return InvoicePayload(
        source_file=d.get("source_file", ""),
        source_type=d.get("source_type", ""),
        customer=customer,
        line_items=line_items,
        totals=totals,
        parse_confidence=d.get("parse_confidence", "low"),
        warnings=d.get("warnings", []) or [],
        needs_review=d.get("needs_review", False),
        review_fields=d.get("review_fields", []) or [],
        staging_path=d.get("staging_path", "")
    )

def review_all_pending(staging_base: str = "invoice_staging") -> None:
    """Batch loader for pending/ directory invoices."""
    base_staging = Path(__file__).parent / staging_base
    pending_dir = base_staging / "pending"
    
    if not pending_dir.exists():
        print(f"No pending reviews directory found at: {pending_dir}")
        return
        
    pending_files = list(pending_dir.glob("*.json"))
    if not pending_files:
        print("No pending invoices found requiring manual review.")
        return
        
    print(f"Found {len(pending_files)} pending invoice(s) for review.")
    for file in pending_files:
        print(f"\nLoading invoice from pending queue: {file.name}")
        try:
            raw_data = json.loads(file.read_text(encoding="utf-8"))
            payload = dict_to_payload(raw_data)
            payload.staging_path = str(file.resolve())
            review_payload(payload)
        except Exception as e:
            print(f"Error loading pending review file {file.name}: {e}")

# ==============================================================================
# SECTION 7: PIPELINE ORCHESTRATOR & MAIN ENGINE ENTRY POINT
# ==============================================================================

def run_pipeline(
    pdf_path: str,
    email_context: str | None = None,
    auto_review: bool = False,
) -> InvoicePayload:
    """Wire all stages: initialize dirs, rasterise, extract, validate, and HITL review."""
    # Initialize the staging directories relative to current parent path
    base_staging = Path(__file__).parent / "invoice_staging"
    (base_staging / "pending").mkdir(parents=True, exist_ok=True)
    (base_staging / "approved").mkdir(parents=True, exist_ok=True)
    (base_staging / "rejected").mkdir(parents=True, exist_ok=True)
    (base_staging / "dummy").mkdir(parents=True, exist_ok=True)

    # 1. Rasterisation & Classification
    rasterised = rasterise(pdf_path)

    # 2. Ollama JSON Extraction
    payload = extract_from_pdf(rasterised, email_context=email_context)
    payload.source_file = pdf_path
    payload.source_type = rasterised.source_type

    # 3. Database Template Enrichment, Scoring, and JSON Staging
    payload = validate_and_stage(payload)

    # 4. Interactive Operator Review Loop (if flag triggers and auto is disabled)
    if payload.needs_review and not auto_review:
        payload = review_payload(payload)
        if payload.parse_confidence != "rejected":
            key = (payload.customer.email.value or payload.customer.name.value or "")
            if key:
                save_template(key.lower().strip(), payload.customer)

    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Invoice Parser v3 Pipeline")
    # PDF positional argument is optional (nargs="?") for batch pending reviews
    parser.add_argument("pdf",         nargs="?", help="Path to invoice PDF")
    parser.add_argument("--email",     help="Path to email body .txt file for context")
    parser.add_argument("--auto",      action="store_true", help="Skip CLI human review prompts (batch/automated)")
    parser.add_argument("--review-pending", action="store_true", help="Launch interactive operator review for all pending/ stagings")
    args = parser.parse_args()

    if args.review_pending:
        review_all_pending()
    else:
        if not args.pdf:
            parser.error("the following arguments are required: pdf (unless --review-pending is set)")
            
        email_text = None
        if args.email:
            email_text = Path(args.email).read_text(encoding="utf-8")

        result = run_pipeline(
            args.pdf,
            email_context=email_text,
            auto_review=args.auto,
        )

        print(f"\nResult: {result.parse_confidence.upper()}")
        print(f"Staged path: {result.staging_path}")
        if result.warnings:
            print("Warnings list:")
            for w in result.warnings:
                print(f"  - {w}")
