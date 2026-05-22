"""
Local LLM extraction layer (Ollama).

Covers:
  - Vendor exclusion config loading
  - Ollama chat client (text + vision)
  - Customer and line-item extraction prompts
  - JSON response cleaning
  - Payload mapping from raw dicts to dataclasses
"""

import re
import json
import requests
from pathlib import Path

from core.invoices.extractor import (
    FieldValue, CustomerInfo, LineItem, InvoiceTotals, InvoicePayload,
    PageData, RasterisedPDF,
)

# v3/ root (this file lives at v3/core/invoices/)
_V3_ROOT = Path(__file__).resolve().parent.parent.parent

# Load vendor exclusion config (private file is gitignored; example is a safe fallback)
_CONFIG_PATH = _V3_ROOT / "vendor_config.json"
_CONFIG_EXAMPLE_PATH = _V3_ROOT / "vendor_config.json.example"

def _load_config() -> dict:
    path = _CONFIG_PATH if _CONFIG_PATH.exists() else _CONFIG_EXAMPLE_PATH
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Failed to load config from {path.name}: {e}")
    return {}

_CONFIG = _load_config()
VENDOR_EXCLUSIONS = _CONFIG.get("vendor_exclusions", [])
VENDOR_ABNS       = _CONFIG.get("vendor_abns", [])
VENDOR_EMAILS     = _CONFIG.get("vendor_emails", [])
VENDOR_PHONES     = _CONFIG.get("vendor_phones", [])

# ==============================================================================
# OLLAMA CLIENT
# ==============================================================================

OLLAMA_BASE  = "http://localhost:11434"
TEXT_MODEL   = "qwen2.5:3b"
VISION_MODEL = "qwen2-vl:2b"

def get_available_models() -> list[str]:
    """Return list of pulled model names from the local Ollama instance."""
    try:
        resp = requests.get(f"{OLLAMA_BASE}/api/tags", timeout=3)
        if resp.status_code == 200:
            return [m["name"] for m in resp.json().get("models", [])]
    except Exception:
        pass
    return []

# Dynamic model selection — runs once at import time
available_models = get_available_models()
if available_models:
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

    best_vision = None
    for model in available_models:
        if "vl" in model:
            best_vision = model
            break
    if best_vision:
        VISION_MODEL = best_vision

def ollama_chat(model: str, messages: list[dict], timeout: int = 120) -> str:
    """Send a chat request to the local Ollama API and return the reply string."""
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.0},
    }
    # Disable MoE thinking steps for qwen 3.6 MoE variants
    if "3.6" in model:
        payload["options"]["think"] = False
    resp = requests.post(f"{OLLAMA_BASE}/api/chat", json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()["message"]["content"]

_SYSTEM_PROMPT = (
    "You are an invoice data extraction engine. You receive invoice content "
    "and return ONLY a valid JSON object. No explanation. No markdown. No "
    "code fences. If a field is not present, use null. For confidence, use "
    "\"high\" if you can clearly see the value, \"medium\" if inferred, \"low\" "
    "if uncertain or guessed."
)

# ==============================================================================
# EXTRACTION FUNCTIONS
# ==============================================================================

def _build_exclusions_str() -> str:
    """Build vendor exclusion instructions string for injection into prompts."""
    s = ""
    if VENDOR_EXCLUSIONS:
        s += f"- Vendor Name Exclusions: {', '.join(repr(x) for x in VENDOR_EXCLUSIONS)}\n"
    if VENDOR_ABNS:
        s += f"- Vendor ABN Exclusions: {', '.join(repr(x) for x in VENDOR_ABNS)}\n"
    if VENDOR_EMAILS:
        s += f"- Vendor Email Exclusions: {', '.join(repr(x) for x in VENDOR_EMAILS)}\n"
    if VENDOR_PHONES:
        s += f"- Vendor Phone Exclusions: {', '.join(repr(x) for x in VENDOR_PHONES)}\n"
    return s

_CUSTOMER_JSON_SCHEMA = (
    "Return this exact JSON:\n"
    "{\n"
    "  \"name\":    {\"value\": null, \"confidence\": \"low\"},\n"
    "  \"email\":   {\"value\": null, \"confidence\": \"low\"},\n"
    "  \"phone\":   {\"value\": null, \"confidence\": \"low\"},\n"
    "  \"abn\":     {\"value\": null, \"confidence\": \"low\"},\n"
    "  \"address\": {\"value\": null, \"confidence\": \"low\"}\n"
    "}"
)

_CUSTOMER_INSTRUCTIONS = (
    "Extract customer (buyer) details from this invoice.\n"
    "IMPORTANT:\n"
    "1. Differentiate between the Supplier/Vendor and the Buyer/Customer:\n"
    "   - The Supplier/Vendor issues the invoice (provides goods/services).\n"
    "   - The Customer/Buyer receives and is billed for the invoice.\n"
    "   - Do NOT extract the Supplier/Vendor's details as the customer!\n"
    "2. We have configured the following Supplier exclusions:\n"
    "{exclusions}"
    "   - If any names, ABNs, emails, or phones above appear, they belong to the Vendor — ignore them for customer details.\n"
    "3. Look for Customer/Buyer details labeled as 'Ship To', 'Deliver To', 'Bill To', or listed next to 'Supplier'.\n"
    "   - e.g. 'Supplier: Vendor  Ship To: Customer' → Customer Name is the second party.\n"
    "   - In Bavarian Bier Cafe documents, Bavarian Bier Cafe is the customer; Top Cut is the vendor.\n"
    "   - In The Star Gold Coast documents, The Star Gold Coast is the customer; Top Cut is the vendor.\n\n"
)

def extract_customer(page: PageData, email_context: str | None = None) -> dict:
    """Extract billing/customer details from invoice page 1 using Ollama."""
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    exclusions_str = _build_exclusions_str()
    instructions = _CUSTOMER_INSTRUCTIONS.format(exclusions=exclusions_str)

    use_vision = (page.page_type == "image") and (VISION_MODEL in available_models)

    if not use_vision:
        text_source = page.text_content if page.page_type == "text" else page.paddle_text
        prompt = instructions + f"INVOICE TEXT:\n{text_source}\n\n"
        if email_context:
            prompt += f"EMAIL CONTEXT (use to fill missing fields):\n{email_context}\n\n"
        prompt += _CUSTOMER_JSON_SCHEMA
        messages.append({"role": "user", "content": prompt})
        model, timeout = TEXT_MODEL, 120
    else:
        prompt = instructions.replace("this invoice", "this invoice image")
        prompt += f"OCR pre-scan detected this text:\n{page.paddle_text[:1000]}\n\n"
        if email_context:
            prompt += f"EMAIL CONTEXT:\n{email_context}\n\n"
        prompt += _CUSTOMER_JSON_SCHEMA
        messages.append({"role": "user", "content": prompt, "images": [page.image_b64]})
        model, timeout = VISION_MODEL, 240

    return parse_json_response(ollama_chat(model, messages, timeout=timeout))

def is_boilerplate_page(text: str) -> bool:
    """Return True for legal T&C pages that contain no invoice line data."""
    if not text:
        return False
    text_lower = text.lower()
    legal_keywords = [
        "terms and conditions", "general terms", "governing law",
        "entire agreement", "severability", "intellectual property",
        "confidentiality and privacy", "warranties",
    ]
    matches = sum(1 for kw in legal_keywords if kw in text_lower)
    if matches >= 2:
        invoice_markers = [
            "qty", "quantity", "unit price", "unit_price",
            "line total", "subtotal", "total due", "total inclusive",
        ]
        if not any(m in text_lower for m in invoice_markers):
            return True
    return False

_LINE_ITEMS_INSTRUCTIONS = (
    "Extract ALL line items and totals from this document.\n"
    "INSTRUCTIONS:\n"
    "1. Extract the product code/SKU in `sku` (e.g. '15335', '15329', '85255', '48245', 'JGKITHB 1007001414', '15196').\n"
    "   - In Picking Slips, look for 5-digit product codes under 'Code' or 'Product' column.\n"
    "   - In Purchase Orders, extract the code under 'Item' or 'Supplier Item' as the SKU.\n"
    "2. Extract the item text description in `description`. Remove leading/trailing product codes.\n"
    "3. Extract numerical quantity in `quantity`.\n"
    "4. Extract the Unit of Measure in `uom` (e.g. 'Kg', 'EA', 'ctn', 'box', 'bag').\n"
    "5. Extract unit price in `unit_price` and total row cost in `line_total`.\n"
    "   - ALWAYS validate: quantity * unit_price = line_total.\n"
    "   - The smaller number is usually unit_price and the larger is line_total.\n"
    "6. IMPORTANT FOR PICKING SLIPS / NON-PRICED DOCUMENTS:\n"
    "   - If there are NO prices on the document, set `unit_price` and `line_total` to null.\n"
    "   - Do NOT put product numbers in `unit_price` or calculate fake totals!\n\n"
)

_LINE_ITEMS_JSON_SCHEMA = (
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

def extract_line_items(pages: list[PageData]) -> dict:
    """Extract line items and totals from all (non-boilerplate) document pages."""
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]

    active_pages = [p for p in pages if not is_boilerplate_page(p.text_content or p.paddle_text)]
    if not active_pages:
        active_pages = pages  # fallback if all pages were filtered

    use_vision = any(p.page_type == "image" for p in active_pages) and (VISION_MODEL in available_models)

    if not use_vision:
        concat_text = ""
        for p in active_pages:
            text_source = p.text_content if p.page_type == "text" else p.paddle_text
            concat_text += f"--- PAGE {p.page_number} ---\n{text_source}\n\n"
        prompt = _LINE_ITEMS_INSTRUCTIONS + f"INVOICE TEXT:\n{concat_text}\n\n" + _LINE_ITEMS_JSON_SCHEMA
        messages.append({"role": "user", "content": prompt})
        model, timeout = TEXT_MODEL, 180
    else:
        ocr_hint = "".join(
            f"--- PAGE {p.page_number} OCR HINT ---\n{p.paddle_text or p.text_content}\n\n"
            for p in active_pages
        )
        prompt = (
            _LINE_ITEMS_INSTRUCTIONS.replace("this document", "this document image")
            + f"OCR pre-scan hints:\n{ocr_hint[:2000]}\n\n"
            + _LINE_ITEMS_JSON_SCHEMA
        )
        messages.append({
            "role": "user",
            "content": prompt,
            "images": [p.image_b64 for p in active_pages],
        })
        model, timeout = VISION_MODEL, 300

    return parse_json_response(ollama_chat(model, messages, timeout=timeout))

# ==============================================================================
# UTILITIES
# ==============================================================================

def parse_json_response(raw: str) -> dict:
    """Strip markdown fences and extract the first JSON object from an LLM reply."""
    cleaned = re.sub(r"```(?:json)?|```", "", raw).strip()
    match = re.search(r'\{.*\}', cleaned, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in response: {raw[:200]}")
    return json.loads(match.group())

def map_to_payload(customer_dict: dict, items_dict: dict) -> InvoicePayload:
    """Map raw extraction dicts to a typed InvoicePayload dataclass."""
    customer_info = CustomerInfo()
    for field_name in ["name", "email", "phone", "abn", "address"]:
        f_dict = customer_dict.get(field_name, {}) or {}
        val = f_dict.get("value")
        if val is not None:
            val = str(val).strip()
        setattr(customer_info, field_name, FieldValue(
            value=val,
            confidence=f_dict.get("confidence", "low"),
            source=f_dict.get("source", "llm"),
        ))

    def safe_float(v):
        if v is None:
            return None
        return float(str(v).replace("$", "").replace(",", "").strip())

    line_items = []
    for i, item in enumerate(items_dict.get("line_items", []) or []):
        try:
            line_items.append(LineItem(
                line_number=int(item.get("line_number") or (i + 1)),
                sku=str(item.get("sku")).strip() if item.get("sku") is not None else None,
                description=str(item.get("description", "") or "").strip(),
                quantity=safe_float(item.get("quantity")),
                uom=str(item.get("uom")).strip() if item.get("uom") is not None else None,
                unit_price=safe_float(item.get("unit_price")),
                line_total=safe_float(item.get("line_total")),
                confidence=item.get("confidence", "high"),
            ))
        except Exception:
            line_items.append(LineItem(
                line_number=i + 1,
                description=str(item.get("description", "Mapping error")),
                confidence="low",
            ))

    totals_dict = items_dict.get("totals", {}) or {}

    def clean_total(v):
        if v is None:
            return None
        try:
            return float(str(v).replace("$", "").replace(",", "").strip())
        except ValueError:
            return None

    return InvoicePayload(
        customer=customer_info,
        line_items=line_items,
        totals=InvoiceTotals(
            subtotal=clean_total(totals_dict.get("subtotal")),
            tax=clean_total(totals_dict.get("tax")),
            total=clean_total(totals_dict.get("total")),
        ),
    )

def extract_from_pdf(rasterised: RasterisedPDF, email_context: str | None = None) -> InvoicePayload:
    """Orchestrate customer + line-item extraction for a rasterised PDF."""
    if not rasterised.pages:
        raise ValueError("No pages rasterised")
    customer_dict = extract_customer(rasterised.pages[0], email_context)
    items_dict = extract_line_items(rasterised.pages)
    return map_to_payload(customer_dict, items_dict)
