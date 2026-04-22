"""
Identifies which customer a document belongs to using four strategies (in order):

  1. Exact sender email address -> match against template sender_emails list
  2. Sender email domain        -> match against template sender_domains list
  3. ABN extracted from text    -> match against template abns list
  4. Keyword matching           -> score each template, pick highest

Strategy 1 exists specifically for personal email senders (hotmail.com, gmail.com etc.)
where the domain is shared by millions of people and useless for identification.

Returns (template_name, confidence_score).
"""
import json
import logging
import re
from pathlib import Path
from typing import Optional, Tuple

from config.settings import TEMPLATES_DIR

log = logging.getLogger(__name__)

ABN_PATTERN = re.compile(r"\b(\d{2}\s?\d{3}\s?\d{3}\s?\d{3})\b")


def _load_templates() -> list[dict]:
    templates = []
    for f in TEMPLATES_DIR.glob("*.json"):
        try:
            templates.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception as e:
            log.warning(f"Failed to load template {f.name}: {e}")
    return templates


def extract_abn(text: str) -> Optional[str]:
    """Extract first ABN-shaped number from text (11 digits, optionally spaced)."""
    match = ABN_PATTERN.search(text)
    if match:
        return re.sub(r"\s", "", match.group(1))
    return None


def _score_keywords(text: str, keywords: list[str]) -> float:
    if not keywords:
        return 0.0
    text_lower = text.lower()
    hits = sum(1 for kw in keywords if kw.lower() in text_lower)
    return hits / len(keywords)


def classify(sender_email: str, text: str) -> Tuple[Optional[str], float]:
    """
    Returns (template_name, confidence) or (None, 0.0) if no match.
    """
    templates = _load_templates()
    if not templates:
        log.warning("No customer templates found in config/templates/.")
        return None, 0.0

    sender_email_lower = sender_email.lower()
    sender_domain = sender_email_lower.split("@")[-1] if "@" in sender_email_lower else ""
    found_abn = extract_abn(text)

    for tmpl in templates:
        name = tmpl.get("customer_name", "unknown")

        # Strategy 1: exact email match (highest confidence — handles personal addresses)
        emails = [e.lower() for e in tmpl.get("sender_emails", [])]
        if sender_email_lower and sender_email_lower in emails:
            log.info(f"Customer matched by exact email: {name}")
            return name, 0.98

        # Strategy 2: domain match (high confidence — handles business addresses)
        domains = [d.lower() for d in tmpl.get("sender_domains", [])]
        if sender_domain and sender_domain in domains:
            log.info(f"Customer matched by domain: {name}")
            return name, 0.95

        # Strategy 3: ABN match (high confidence)
        abns = [re.sub(r"\s", "", a) for a in tmpl.get("abns", [])]
        if found_abn and found_abn in abns:
            log.info(f"Customer matched by ABN: {name}")
            return name, 0.90

    # Strategy 3: keyword scoring across all templates
    best_name, best_score = None, 0.0
    for tmpl in templates:
        name = tmpl.get("customer_name", "unknown")
        score = _score_keywords(text, tmpl.get("keywords", []))
        if score > best_score:
            best_score = score
            best_name = name

    if best_score > 0:
        log.info(f"Customer matched by keywords: {best_name} (score={best_score:.2f})")

    return best_name, best_score
