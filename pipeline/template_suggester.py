"""
Auto-generates draft YAML templates for unrecognised senders.

When no existing contact matches an email, this module:
  1. Extracts sender info, ABN, dates, invoice numbers, amounts, and keywords
  2. Writes a draft YAML template to config/suggested_templates/
  3. Includes an _address_book_entry block showing exactly what to add to
     config/address_book.json to allow the sender in future runs

The user then:
  - Adds the _address_book_entry to config/address_book.json
  - Reviews and adjusts the regex patterns in the suggested template
  - Copies the file to config/templates/ to activate field parsing

Existing suggestions are updated (not overwritten) to preserve manual edits.
"""
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import yaml

from config.settings import (
    SUGGESTED_TEMPLATES_DIR, PERSONAL_EMAIL_DOMAINS,
    AUTO_APPROVE_CONFIDENCE, ALERT_WEBHOOK_URL,
    TEMPLATES_DIR, ADDRESS_BOOK_PATH,
)
from pipeline.customer_classifier import extract_abn

log = logging.getLogger(__name__)

STOP_WORDS = {
    "the", "and", "for", "are", "but", "not", "you", "all", "any", "can",
    "her", "was", "one", "our", "out", "day", "get", "has", "him", "his",
    "how", "its", "may", "new", "now", "old", "see", "two", "who", "boy",
    "did", "let", "put", "say", "she", "too", "use", "this", "that", "with",
    "have", "from", "they", "will", "been", "more", "when", "your", "than",
    "then", "into", "some", "each", "also", "were", "which", "there", "their",
    "what", "would", "about", "could", "other", "after", "first", "well",
    "page", "date", "total", "amount", "please", "dear", "regards", "thank",
    "invoice", "bill", "payment", "account", "number", "due",
}

_DATE_RE = re.compile(
    r"(?:invoice|order|bill|issue|date|issued|created)[^\n]{0,20}?"
    r"(\d{1,2}[\s/\-.]\w{2,9}[\s/\-.]\d{2,4}|\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}"
    r"|\d{1,2}\.\d{2}\.\d{4}|\d{2}-[A-Z]{3}-\d{2})",
    re.IGNORECASE,
)
_DELIVERY_RE = re.compile(
    r"(?:due|deliver|required|by|dispatch)[^\n]{0,20}?"
    r"(\d{1,2}[\s/\-.]\w{2,9}[\s/\-.]\d{2,4}|\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}"
    r"|\d{1,2}\.\d{2}\.\d{4})",
    re.IGNORECASE,
)
_INVOICE_RE = re.compile(
    r"(?:invoice|inv|order|po|ref|reference)\s*(?:number|no\.?|#)?[:\s#]*([A-Z0-9][A-Z0-9\-]{2,24})",
    re.IGNORECASE,
)
_AMOUNT_RE = re.compile(r"(?:\$|AUD)\s*([\d,]+\.\d{2})")
_ADDRESS_RE = re.compile(
    r"(\d{1,5}\s+\w[\w\s,\.]{5,60}(?:NSW|VIC|QLD|WA|SA|TAS|ACT|NT)\s+\d{4})",
    re.IGNORECASE,
)
_ABN_LABEL_RE = re.compile(r"ABN[\s:\-]+([\d\s]{11,14})", re.IGNORECASE)
_SUBTOTAL_RE = re.compile(
    r"(?:sub[\-\s]?total|amount\s*\(net\))[:\s]+([\d,]+\.\d{2})", re.IGNORECASE
)
_TOTAL_RE = re.compile(
    r"(?:total\s+amount|amount\s*\(gross\)|grand\s+total|total\s+incl)[^\n]{0,30}?([\d,]+\.\d{2})",
    re.IGNORECASE,
)
# Table header keywords that indicate a line-items section
_TABLE_HEADER_RE = re.compile(
    r"\b(qty|quantity)\b.{0,40}\b(code|item|product)\b.{0,40}\b(price|cost|total)\b",
    re.IGNORECASE,
)
_PIPE_ROW_RE = re.compile(r"^[^|]+\|[^|]+\|", re.MULTILINE)


def _extract_keywords(text: str, top_n: int = 8) -> list[str]:
    words = re.findall(r"\b[a-zA-Z]{4,}\b", text.lower())
    counts = Counter(w for w in words if w not in STOP_WORDS)
    return [w for w, _ in counts.most_common(top_n)]


def _safe_filename(sender_email: str) -> str:
    safe = re.sub(r"[^\w@.\-]", "_", sender_email.lower())
    return safe.replace("@", "_at_")


def _sniff_line_item_format(text: str) -> tuple[str, list[str]]:
    """
    Detect the line-item table format and return (format_type, suggested_patterns).
    format_type: 'pipe', 'tabular', or 'unknown'
    """
    pipe_lines = [l for l in text.splitlines() if l.count("|") >= 2 and len(l) > 10]
    if len(pipe_lines) >= 2:
        return "pipe", [
            r"(?P<product_code>\d{4,})\s*\|\s*(?P<description>[^|]+?)"
            r"\s*\|\s*(?P<qty>[\d.]+)\s*\|\s*(?P<unit_price>[\d.]+)"
            r"\s*\|\s*(?P<total>[\d,]+\.\d{2})"
        ]

    if _TABLE_HEADER_RE.search(text):
        return "tabular", [
            r"^(?P<qty>[\d.]+)\s+(?P<uom>\w+)\s+(?P<product_code>\d{4,})"
            r"\s+(?P<description>.+?)\s+(?P<unit_price>[\d.]+)"
            r"\s+(?P<total>[\d,]+\.\d{2})$"
        ]

    return "unknown", [
        r"(?P<qty>\d+(?:\.\d+)?)\s+(?P<description>[A-Za-z][\w\s,.-]{3,60})"
        r"\s+\$?(?P<unit_price>[\d,]+\.\d{2})\s+\$?(?P<total>[\d,]+\.\d{2})"
    ]


def _sniff_fields(text: str) -> dict:
    """Detect example field values in the document to help write patterns."""
    found = {}
    m = _DATE_RE.search(text)
    if m:
        found["order_date_example"] = m.group(1).strip()
    m = _DELIVERY_RE.search(text)
    if m:
        found["delivery_date_example"] = m.group(1).strip()
    m = _INVOICE_RE.search(text)
    if m:
        found["invoice_number_example"] = m.group(1).strip()
    amounts = _AMOUNT_RE.findall(text)
    if amounts:
        found["amounts_found"] = amounts[:3]
    m = _ADDRESS_RE.search(text)
    if m:
        found["address_example"] = m.group(1).strip()
    m = _ABN_LABEL_RE.search(text)
    if m:
        found["abn_example"] = re.sub(r"\s", "", m.group(1)).strip()
    m = _SUBTOTAL_RE.search(text)
    if m:
        found["subtotal_example"] = m.group(1).strip()
    m = _TOTAL_RE.search(text)
    if m:
        found["total_example"] = m.group(1).strip()
    fmt, _ = _sniff_line_item_format(text)
    found["line_item_format_detected"] = fmt
    return found


def _build_template(sender_email: str, display_name: str, text: str) -> dict:
    is_personal = sender_email.split("@")[-1].lower() in PERSONAL_EMAIL_DOMAINS
    abn = extract_abn(text)
    keywords = _extract_keywords(text)
    sniffed = _sniff_fields(text)
    customer_name = display_name.strip() if display_name else sender_email.split("@")[0]
    template_stem = _safe_filename(sender_email)

    address_book_entry: dict = {"name": customer_name, "template": template_stem}
    if is_personal:
        address_book_entry["emails"] = [sender_email]
    else:
        address_book_entry["domains"] = [sender_email.split("@")[-1]]
    if abn:
        address_book_entry["abns"] = [abn]
    if keywords:
        address_book_entry["keywords"] = keywords

    _, line_item_patterns = _sniff_line_item_format(text)

    template: dict = {
        "_status": "SUGGESTED — review patterns, then copy to config/templates/ to activate",
        "_generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "_address_book_entry": address_book_entry,
        "customer_name": customer_name,
        "required_fields": ["po_number", "delivery_date"],
        "fields": {
            "customer_name": [re.escape(customer_name) if customer_name else "FILL_IN_CUSTOMER_NAME"],
            "company_abn": [r"ABN[\s:\-]+([\d\s]{11,14})"],
            "address": [r"(\d+\s+\w+.*?(?:NSW|VIC|QLD|WA|SA|TAS|ACT|NT)\s+\d{4})"],
            "po_number": [
                r"(?:PO\s*Number|Order\s*(?:Number|No\.?|#)|REF)[:\s#]*([A-Z0-9][A-Z0-9\-]{2,24})",
                r"Order\s*:\s*([A-Z]\d+)",
            ],
            "order_date": [
                r"(?:Order|Invoice|Bill|Issue|Created)\s*(?:Date|On|date)?[:\s]+(\d{1,2}[./\-]\w{2,9}[./\-]\d{2,4})",
                r"(?:Order|Invoice|Bill|Issue|Created)\s*(?:Date|On|date)?[:\s]+(\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4})",
            ],
            "delivery_date": [
                r"(?:Delivery\s+Date|Date\s+of\s+Delivery|Deliver\s+By)[:\s]+(\d{1,2}[./\-]\w{2,9}[./\-]\d{2,4})",
                r"(?:Delivery\s+Date|Date\s+of\s+Delivery|Deliver\s+By)[:\s]+(\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4})",
            ],
            "company_name": [
                r"Company\s+Name[:\s]+(.+?)(?:,\s*ABN|\n|$)",
                r"Customer[:\s]+(.+?)(?:\n|,)",
                r"Ship\s+To[:\s]+(.+?)(?:\n|,)",
            ],
            "subtotal": [
                r"(?:Sub[\-\s]?Total|Amount\s*\(net\))[:\s]+([\d,]+\.\d{2})",
            ],
            "tax_amount": [
                r"(?:^Tax|Total\s+GST|GST\s+Amount)[:\s]+([\d,]+\.\d{2})",
            ],
            "total_amount": [
                r"(?:Total\s+Amount|Amount\s*\(gross\)|Grand\s+Total|Total\s+Incl)[^\n]{0,30}?([\d,]+\.\d{2})",
            ],
        },
        "line_items_patterns": line_item_patterns,
    }

    if sniffed:
        template["_field_examples_found_in_document"] = sniffed

    return template


def _auto_approve(dest: Path, template: dict) -> bool:
    """
    Attempt to auto-approve a suggestion when AUTO_APPROVE_CONFIDENCE > 0.
    Copies the template to config/templates/ and appends the address_book_entry
    to config/address_book.json atomically.
    Returns True if approval succeeded.
    """
    if AUTO_APPROVE_CONFIDENCE <= 0:
        return False

    sniffed = template.get("_field_examples_found_in_document", {})
    required_fields = template.get("required_fields", [])
    matched = sum(
        1 for f in required_fields
        if sniffed.get(f"{f}_example") or sniffed.get(f)
    )
    confidence = matched / len(required_fields) if required_fields else 0.0

    if confidence < AUTO_APPROVE_CONFIDENCE:
        log.debug(
            f"Auto-approve skipped for {dest.stem}: "
            f"confidence {confidence:.0%} < threshold {AUTO_APPROVE_CONFIDENCE:.0%}"
        )
        return False

    try:
        # Copy template to active templates directory
        active_path = TEMPLATES_DIR / dest.name
        TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
        active_path.write_text(
            yaml.dump(template, default_flow_style=False, allow_unicode=True),
            encoding="utf-8",
        )

        # Append address_book_entry to address_book.json
        entry = template.get("_address_book_entry", {})
        if entry and ADDRESS_BOOK_PATH.exists():
            book = json.loads(ADDRESS_BOOK_PATH.read_text(encoding="utf-8"))
            # Avoid duplicates
            existing_names = {c.get("name") for c in book.get("contacts", [])}
            if entry.get("name") not in existing_names:
                book.setdefault("contacts", []).append(entry)
                ADDRESS_BOOK_PATH.write_text(
                    json.dumps(book, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                log.info(
                    f"Auto-approved template for {entry.get('name', dest.stem)}: "
                    f"confidence={confidence:.0%}. Added to address_book.json and config/templates/."
                )
        return True
    except Exception as exc:
        log.warning(f"Auto-approve failed for {dest.stem}: {exc}")
        return False


def _send_suggestion_alert(sender_email: str, dest: Path, auto_approved: bool) -> None:
    """Fire-and-forget webhook when a new suggestion is created."""
    if not ALERT_WEBHOOK_URL:
        return
    action = "auto-approved and activated" if auto_approved else "saved — awaiting manual review"
    msg = (
        f"[ms_outlook] New sender: {sender_email} — "
        f"suggested template {action}: {dest.name}"
    )
    try:
        import requests as _req
        _req.post(ALERT_WEBHOOK_URL, json={"text": msg}, timeout=5)
    except Exception:
        pass


def suggest(sender_email: str, display_name: str, text: str) -> Optional[Path]:
    """
    Generate or update a suggested YAML template for an unrecognised sender.
    If AUTO_APPROVE_CONFIDENCE > 0 and field confidence meets the threshold,
    the template is automatically promoted to config/templates/ and the sender
    is added to address_book.json.
    Returns the path to the suggestion file, or None if generation failed.
    """
    import json  # local import to avoid circular at module level
    SUGGESTED_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

    filename = _safe_filename(sender_email) + ".yaml"
    dest = SUGGESTED_TEMPLATES_DIR / filename
    is_new = not dest.exists()

    if not is_new:
        try:
            existing = yaml.safe_load(dest.read_text(encoding="utf-8"))
            existing["_generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            sniffed = _sniff_fields(text)
            if sniffed:
                existing["_field_examples_found_in_document"] = sniffed
            abn = extract_abn(text)
            if abn:
                entry = existing.setdefault("_address_book_entry", {})
                abns = entry.setdefault("abns", [])
                if abn not in abns:
                    abns.append(abn)
            dest.write_text(
                yaml.dump(existing, default_flow_style=False, allow_unicode=True),
                encoding="utf-8",
            )
            log.info(f"Updated existing suggestion: {dest.name}")
            return dest
        except Exception as e:
            log.warning(f"Could not update existing suggestion {dest.name}: {e}")

    try:
        template = _build_template(sender_email, display_name, text)
        dest.write_text(
            yaml.dump(template, default_flow_style=False, allow_unicode=True),
            encoding="utf-8",
        )
        log.info(f"Suggested template saved: {dest}")

        auto_approved = _auto_approve(dest, template)
        if is_new:
            _send_suggestion_alert(sender_email, dest, auto_approved)

        return dest
    except Exception as e:
        log.warning(f"Failed to generate suggestion for {sender_email}: {e}")
        return None
