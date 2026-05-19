"""
Identifies which contact an email belongs to using four strategies (in order):

  1. Exact sender email address -> match against contact emails list
  2. Sender email domain        -> match against contact domains list
  3. ABN extracted from text    -> match against contact abns list
  4. Keyword matching           -> score each contact, pick highest

Strategy 1 exists specifically for personal email senders (hotmail.com, gmail.com etc.)
where the domain is shared by millions of people and useless for identification.

Returns (customer_name, confidence, template_name) where template_name is the
YAML file stem to use for parsing, or None if no template is linked.
"""
import logging
import re
from typing import Optional, Tuple

log = logging.getLogger(__name__)

ABN_PATTERN = re.compile(r"\b(\d{2}\s?\d{3}\s?\d{3}\s?\d{3})\b")


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


def classify(
    sender_email: str,
    text: str,
    contacts: list[dict],
) -> Tuple[Optional[str], float, Optional[str]]:
    """
    Returns (customer_name, confidence, template_name).
    template_name is the YAML file stem (e.g. "evergy") or None if no template is linked.
    Returns (None, 0.0, None) if no contact matched.
    """
    if not contacts:
        log.warning("No contacts provided — cannot classify sender.")
        return None, 0.0, None

    sender_email_lower = sender_email.lower()
    sender_domain = sender_email_lower.split("@")[-1] if "@" in sender_email_lower else ""
    found_abn = extract_abn(text)

    for contact in contacts:
        name = contact.get("name", "unknown")
        template = contact.get("template")

        # Strategy 1: exact email match (handles personal addresses)
        emails = [e.lower() for e in contact.get("emails", [])]
        if sender_email_lower and sender_email_lower in emails:
            log.info(f"Customer matched by exact email: {name}")
            return name, 0.98, template

        # Strategy 2: domain match (handles business addresses)
        domains = [d.lower() for d in contact.get("domains", [])]
        if sender_domain and sender_domain in domains:
            log.info(f"Customer matched by domain: {name}")
            return name, 0.95, template

        # Strategy 3: ABN match
        abns = [re.sub(r"\s", "", a) for a in contact.get("abns", []) if a]
        if found_abn and found_abn in abns:
            log.info(f"Customer matched by ABN: {name}")
            return name, 0.90, template

    # Strategy 4: keyword scoring across all contacts
    best_contact, best_score = None, 0.0
    for contact in contacts:
        score = _score_keywords(text, contact.get("keywords", []))
        if score > best_score:
            best_score = score
            best_contact = contact

    if best_contact and best_score > 0:
        name = best_contact.get("name", "unknown")
        template = best_contact.get("template")
        log.info(f"Customer matched by keywords: {name} (score={best_score:.2f})")
        return name, best_score, template

    return None, 0.0, None
