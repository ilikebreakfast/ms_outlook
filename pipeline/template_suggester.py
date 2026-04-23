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
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from config.settings import SUGGESTED_TEMPLATES_DIR
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

_PERSONAL_DOMAINS = {
    "gmail.com", "hotmail.com", "outlook.com", "yahoo.com",
    "live.com", "icloud.com", "bigpond.com", "optusnet.com.au",
}

_DATE_RE = re.compile(
    r"(?:invoice|order|bill|issue|date|issued)[^\n]{0,20}?"
    r"(\d{1,2}[\s/\-]\w{2,9}[\s/\-]\d{2,4}|\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})",
    re.IGNORECASE,
)
_DELIVERY_RE = re.compile(
    r"(?:due|deliver|required|by)[^\n]{0,20}?"
    r"(\d{1,2}[\s/\-]\w{2,9}[\s/\-]\d{2,4}|\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})",
    re.IGNORECASE,
)
_INVOICE_RE = re.compile(
    r"(?:invoice|inv|order|po|ref|reference)[^\n]{0,10}?[#:\s]([A-Z0-9\-]{3,20})",
    re.IGNORECASE,
)
_AMOUNT_RE = re.compile(r"\$\s*([\d,]+\.\d{2})")
_ADDRESS_RE = re.compile(
    r"(\d{1,5}\s+\w[\w\s,\.]{5,60}(?:NSW|VIC|QLD|WA|SA|TAS|ACT|NT)\s+\d{4})",
    re.IGNORECASE,
)


def _extract_keywords(text: str, top_n: int = 8) -> list[str]:
    words = re.findall(r"\b[a-zA-Z]{4,}\b", text.lower())
    counts = Counter(w for w in words if w not in STOP_WORDS)
    return [w for w, _ in counts.most_common(top_n)]


def _safe_filename(sender_email: str) -> str:
    safe = re.sub(r"[^\w@.\-]", "_", sender_email.lower())
    return safe.replace("@", "_at_")


def _sniff_fields(text: str) -> dict:
    """Detect example field values in the document to help the user write patterns."""
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
    return found


def _build_template(sender_email: str, display_name: str, text: str) -> dict:
    is_personal = sender_email.split("@")[-1].lower() in _PERSONAL_DOMAINS
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

    template: dict = {
        "_status": "SUGGESTED — review patterns, then copy to config/templates/ to activate",
        "_generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "_address_book_entry": address_book_entry,
        "customer_name": customer_name,
        "required_fields": ["invoice_number", "order_date"],
        "fields": {
            "customer_name": [re.escape(customer_name) if customer_name else "FILL_IN_CUSTOMER_NAME"],
            "abn": [r"ABN[:\s]+([\d\s]{11,14})"],
            "address": [r"(\d+\s+\w+.*?(?:NSW|VIC|QLD|WA|SA|TAS|ACT|NT)\s+\d{4})"],
            "order_date": [r"(?:Invoice|Order|Bill|Issue)\s*Date[:\s]+(\d{1,2}[\s/\-]\w{2,9}[\s/\-]\d{2,4})"],
            "requested_delivery_date": [r"(?:Due|Deliver(?:y)?\s*(?:By|Date))[:\s]+(\d{1,2}[\s/\-]\w{2,9}[\s/\-]\d{2,4})"],
            "invoice_number": [r"(?:Invoice|INV|Order|PO|Ref)[\s#:.]*(\w{3,20})"],
        },
        "line_items_pattern": (
            r"(?P<qty>\d+(?:\.\d+)?)\s+"
            r"(?P<description>[A-Za-z][\w\s,.-]{3,60})\s+"
            r"\$?(?P<unit_price>[\d,]+\.\d{2})\s+"
            r"\$?(?P<total>[\d,]+\.\d{2})"
        ),
    }

    if sniffed:
        template["_field_examples_found_in_document"] = sniffed

    return template


def suggest(sender_email: str, display_name: str, text: str) -> Optional[Path]:
    """
    Generate or update a suggested YAML template for an unrecognised sender.
    Returns the path to the suggestion file, or None if generation failed.
    """
    SUGGESTED_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

    filename = _safe_filename(sender_email) + ".yaml"
    dest = SUGGESTED_TEMPLATES_DIR / filename

    if dest.exists():
        try:
            existing = yaml.safe_load(dest.read_text(encoding="utf-8"))
            existing["_generated_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
            sniffed = _sniff_fields(text)
            if sniffed:
                existing["_field_examples_found_in_document"] = sniffed
            abn = extract_abn(text)
            if abn:
                entry = existing.setdefault("_address_book_entry", {})
                abns = entry.setdefault("abns", [])
                if abn not in abns:
                    abns.append(abn)
            dest.write_text(yaml.dump(existing, default_flow_style=False, allow_unicode=True), encoding="utf-8")
            log.info(f"Updated existing suggestion: {dest.name}")
            return dest
        except Exception as e:
            log.warning(f"Could not update existing suggestion {dest.name}: {e}")

    try:
        template = _build_template(sender_email, display_name, text)
        dest.write_text(yaml.dump(template, default_flow_style=False, allow_unicode=True), encoding="utf-8")
        log.info(f"Suggested template saved: {dest}")
        return dest
    except Exception as e:
        log.warning(f"Failed to generate suggestion for {sender_email}: {e}")
        return None
