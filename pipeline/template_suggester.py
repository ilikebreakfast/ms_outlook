"""
Auto-generates draft customer templates for unrecognised senders.

When no existing template matches an email, this module:
  1. Extracts whatever it can from the sender info and document text
     (display name, domain/email, ABN, dates, invoice numbers, amounts, keywords)
  2. Writes a draft JSON template to config/suggested_templates/
  3. Leaves regex fields pre-populated with best-guess patterns and
     inline comments explaining what to look for

The user then:
  - Reviews the suggested template
  - Adjusts any regex patterns that need tweaking
  - Copies the file to config/templates/ to activate it

Existing suggestions are updated (not overwritten) if new info is found.
"""
import json
import logging
import re
import string
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

from config.settings import SUGGESTED_TEMPLATES_DIR
from pipeline.customer_classifier import extract_abn

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stop words — filtered out of keyword suggestions
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Pattern sniffers — try to detect field values in raw text
# ---------------------------------------------------------------------------

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
    """Return the most distinctive non-stop words from the text."""
    words = re.findall(r"\b[a-zA-Z]{4,}\b", text.lower())
    counts = Counter(w for w in words if w not in STOP_WORDS)
    return [w for w, _ in counts.most_common(top_n)]


def _safe_filename(sender_email: str) -> str:
    """Turn an email address into a safe filename."""
    safe = re.sub(r"[^\w@.\-]", "_", sender_email.lower())
    return safe.replace("@", "_at_")


def _sniff_fields(text: str) -> dict:
    """
    Try to detect what field patterns look like in this specific document.
    Returns example values (not regexes) to help the user write patterns.
    """
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


def _build_template(
    sender_email: str,
    display_name: str,
    text: str,
) -> dict:
    """Build a draft template dict from available information."""
    is_personal = sender_email.split("@")[-1].lower() in {
        "gmail.com", "hotmail.com", "outlook.com", "yahoo.com",
        "live.com", "icloud.com", "bigpond.com", "optusnet.com.au",
    }

    abn = extract_abn(text)
    keywords = _extract_keywords(text)
    sniffed = _sniff_fields(text)

    # Build the best-guess customer name
    customer_name = display_name.strip() if display_name else sender_email.split("@")[0]

    template = {
        "_status": "SUGGESTED — review and copy to config/templates/ to activate",
        "_generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "_sender_seen": sender_email,
        "customer_name": customer_name,
        "sender_emails": [sender_email] if is_personal else [],
        "sender_domains": [] if is_personal else [sender_email.split("@")[-1]],
        "abns": [abn] if abn else [],
        "keywords": keywords,
        "required_fields": ["invoice_number", "order_date"],
        "fields": {
            "customer_name": [
                re.escape(customer_name) if customer_name else "FILL_IN_CUSTOMER_NAME"
            ],
            "abn": [
                "ABN[:\\s]+([\\d\\s]{11,14})"
            ],
            "address": [
                "(\\d+\\s+\\w+.*?(?:NSW|VIC|QLD|WA|SA|TAS|ACT|NT)\\s+\\d{4})"
            ],
            "order_date": [
                "(?:Invoice|Order|Bill|Issue)\\s*Date[:\\s]+(\\d{1,2}[\\s/\\-]\\w{2,9}[\\s/\\-]\\d{2,4})"
            ],
            "requested_delivery_date": [
                "(?:Due|Deliver(?:y)?\\s*(?:By|Date))[:\\s]+(\\d{1,2}[\\s/\\-]\\w{2,9}[\\s/\\-]\\d{2,4})"
            ],
            "invoice_number": [
                "(?:Invoice|INV|Order|PO|Ref)[\\s#:.]*(\\w{3,20})"
            ],
        },
        "line_items_pattern": (
            "(?P<qty>\\d+(?:\\.\\d+)?)\\s+"
            "(?P<description>[A-Za-z][\\w\\s,.-]{3,60})\\s+"
            "\\$?(?P<unit_price>[\\d,]+\\.\\d{2})\\s+"
            "\\$?(?P<total>[\\d,]+\\.\\d{2})"
        ),
    }

    # Attach sniffed examples as hints for the user
    if sniffed:
        template["_field_examples_found_in_document"] = sniffed

    return template


def suggest(
    sender_email: str,
    display_name: str,
    text: str,
) -> Optional[Path]:
    """
    Generate or update a suggested template for an unrecognised sender.
    Returns the path to the suggestion file, or None if generation failed.
    """
    SUGGESTED_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

    filename = _safe_filename(sender_email) + ".json"
    dest = SUGGESTED_TEMPLATES_DIR / filename

    # If a suggestion already exists, only update the generated_at and examples
    # so we don't overwrite any manual edits the user may have made.
    if dest.exists():
        try:
            existing = json.loads(dest.read_text(encoding="utf-8"))
            existing["_generated_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
            sniffed = _sniff_fields(text)
            if sniffed:
                existing["_field_examples_found_in_document"] = sniffed
            abn = extract_abn(text)
            if abn and abn not in existing.get("abns", []):
                existing.setdefault("abns", []).append(abn)
            dest.write_text(json.dumps(existing, indent=2), encoding="utf-8")
            log.info(f"Updated existing suggestion: {dest.name}")
            return dest
        except Exception as e:
            log.warning(f"Could not update existing suggestion {dest.name}: {e}")

    try:
        template = _build_template(sender_email, display_name, text)
        dest.write_text(json.dumps(template, indent=2), encoding="utf-8")
        log.info(f"Suggested template saved: {dest}")
        return dest
    except Exception as e:
        log.warning(f"Failed to generate suggestion for {sender_email}: {e}")
        return None
