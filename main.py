"""
Main pipeline orchestrator.

  python main.py

Prompts for how many days of emails to process (default: 1).
The sender allowlist is loaded from config/address_book.json BEFORE
downloading any attachments — unknown senders are skipped entirely.

If a sender has no linked template yet, the pipeline still downloads and
extracts text, saves a JSON with status="extracted_only", and moves the
email to Processed. Add a YAML template later and it will parse on the
next run.
"""
import json
import logging
from datetime import datetime
from pathlib import Path

from utils.logger import setup_logging
from auth.graph_client import GraphClient
from pipeline.email_reader import fetch_unread_with_attachments
from pipeline.attachment_downloader import download_attachments
from pipeline.text_extractor import extract_text
from pipeline.customer_classifier import classify
from pipeline.template_parser import parse
from pipeline.json_output import build_output, save_json
from pipeline.email_mover import move_to_processed
from pipeline.security import validate_attachment, scrub_prompt_injection, is_allowed_sender
from pipeline.template_suggester import suggest as suggest_template
from config.settings import MOVE_AFTER_PROCESSING, TEMPLATES_DIR, ADDRESS_BOOK_PATH

try:
    from database import db
except ImportError:
    class _DBStub:
        def already_processed(self, *args, **kwargs): return False
        def record(self, *args, **kwargs): pass
    db = _DBStub()

setup_logging()
log = logging.getLogger(__name__)


def _ask_days() -> int:
    try:
        raw = input("How many days of emails to process? [default: 1]: ").strip()
        if not raw:
            return 1
        value = int(raw)
        if value < 1:
            raise ValueError
        return value
    except ValueError:
        print("Invalid input — using default of 1 day.")
        return 1


def _load_contacts() -> list[dict]:
    """Load contacts from config/address_book.json."""
    if not ADDRESS_BOOK_PATH.exists():
        log.warning("config/address_book.json not found — all senders will be allowed.")
        return []
    try:
        data = json.loads(ADDRESS_BOOK_PATH.read_text(encoding="utf-8"))
        contacts = data.get("contacts", [])
        log.info(f"Loaded {len(contacts)} contact(s) from address_book.json")
        return contacts
    except Exception as e:
        log.warning(f"Failed to load address_book.json: {e}")
        return []


def _contacts_to_allowlists(contacts: list[dict]) -> tuple[set[str], set[str]]:
    """Extract domain and email sets for the sender allowlist check."""
    domains: set[str] = set()
    emails: set[str] = set()
    for c in contacts:
        for d in c.get("domains", []):
            domains.add(d.lower())
        for e in c.get("emails", []):
            emails.add(e.lower())
    if domains or emails:
        log.info(f"Sender allowlist — domains: {sorted(domains)}, emails: {sorted(emails)}")
    else:
        log.warning("No sender allowlist configured — all senders will be allowed.")
    return domains, emails


def process_attachment(
    client, message, attachment_path, allowed_domains, allowed_emails, contacts
) -> bool:
    """Returns True if processing succeeded (used to decide whether to move the email)."""
    msg_id = message["id"]
    filename = attachment_path.name
    sender = message.get("from", {}).get("emailAddress", {}).get("address", "")
    received = message.get("receivedDateTime", "")

    if db.already_processed(msg_id, filename):
        log.info(f"Already processed, skipping: {filename}")
        return True

    # --- File-level security gate (sender already checked before download) ---
    ok, issues = validate_attachment(attachment_path, sender, allowed_domains, allowed_emails)
    for issue in issues:
        log.warning(f"SECURITY [{filename}]: {issue}")
    if not ok:
        log.warning(f"Attachment blocked by security checks, skipping: {filename}")
        db.record(
            message_id=msg_id,
            attachment_filename=filename,
            sender_email=sender,
            received_at=received,
            processed_at=datetime.utcnow().isoformat() + "Z",
            error="BLOCKED: " + " | ".join(issues),
        )
        return False

    try:
        log.info(f"Extracting text: {filename}")
        text, is_native = extract_text(attachment_path)
        text = scrub_prompt_injection(text)

        log.info(f"Classifying sender for: {filename}")
        customer_name, class_confidence, template_name = classify(sender, text, contacts)

        # Determine whether we have a usable template
        can_parse = False
        if template_name:
            template_path = TEMPLATES_DIR / f"{template_name}.yaml"
            if template_path.exists():
                can_parse = True

        if can_parse:
            log.info(f"Parsing with template: {template_name!r}")
            parsed = parse(text, template_name)
            doc = build_output(parsed, customer_name, class_confidence, message, attachment_path)
        else:
            log.info(f"No template for {customer_name!r} — saving extracted text only.")
            doc = build_output(
                {}, customer_name, class_confidence, message, attachment_path,
                status="extracted_only"
            )
            # Auto-generate a YAML template suggestion so the user has a starting point
            display_name = message.get("from", {}).get("emailAddress", {}).get("name", "")
            suggestion_path = suggest_template(sender, display_name, text)
            if suggestion_path:
                log.info(
                    f"Template suggestion saved to "
                    f"{suggestion_path.relative_to(Path.cwd())} — "
                    "review patterns and copy to config/templates/ to enable parsing."
                )

        json_path = save_json(doc, attachment_path)

        db.record(
            message_id=msg_id,
            attachment_filename=filename,
            sender_email=sender,
            received_at=received,
            customer_name=doc.customer_name,
            invoice_number=doc.invoice_number,
            confidence=doc.confidence,
            needs_review=doc.needs_review,
            json_path=str(json_path),
            attachment_path=str(attachment_path),
            processed_at=doc.processed_at,
        )

        if doc.status == "extracted_only":
            log.info(
                f"Extracted only (no template): {filename} | customer={doc.customer_name} | "
                "Add a YAML template to enable field parsing."
            )
        elif doc.needs_review:
            log.warning(f"LOW CONFIDENCE ({doc.confidence:.0%}) - flagged for review: {filename}")
        else:
            log.info(f"Done: {filename} | customer={doc.customer_name} | conf={doc.confidence:.0%}")

        return True

    except Exception as exc:
        log.error(f"Failed to process {filename}: {exc}", exc_info=True)
        db.record(
            message_id=msg_id,
            attachment_filename=filename,
            sender_email=sender,
            received_at=received,
            processed_at=datetime.utcnow().isoformat() + "Z",
            error=str(exc),
        )
        return False


def main():
    days = _ask_days()
    log.info(f"Pipeline starting — processing last {days} day(s) of emails.")

    contacts = _load_contacts()
    allowed_domains, allowed_emails = _contacts_to_allowlists(contacts)
    client = GraphClient()

    total = 0
    blocked = 0
    moved = 0

    for message in fetch_unread_with_attachments(client, days=days):
        subject = message.get("subject", "(no subject)")
        msg_id = message["id"]
        sender = message.get("from", {}).get("emailAddress", {}).get("address", "")

        # --- Sender check BEFORE downloading anything ---
        if not is_allowed_sender(sender, allowed_domains, allowed_emails):
            log.info(f"Skipping email from unknown sender (no download): {sender!r} | {subject!r}")
            blocked += 1
            continue

        log.info(f"Processing email from {sender!r}: {subject!r}")

        paths = download_attachments(client, msg_id)
        if not paths:
            log.info(f"No supported attachments in: {subject!r}")
            continue

        all_succeeded = True
        for path in paths:
            success = process_attachment(
                client, message, path, allowed_domains, allowed_emails, contacts
            )
            if not success:
                all_succeeded = False
                blocked += 1
            total += 1

        if MOVE_AFTER_PROCESSING and all_succeeded:
            if move_to_processed(client, msg_id):
                moved += 1

    log.info(
        f"Pipeline complete. {total} attachment(s) processed, "
        f"{blocked} blocked/skipped, {moved} email(s) moved."
    )


if __name__ == "__main__":
    main()
