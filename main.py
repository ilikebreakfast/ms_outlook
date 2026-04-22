"""
Main pipeline orchestrator.

  python main.py

Prompts for how many days of emails to process (default: 1).
After each email is successfully processed, moves it to the
"Processed-Pipeline" folder so it won't be picked up again.
"""
import logging
import sys
from datetime import datetime

from utils.logger import setup_logging
from auth.graph_client import GraphClient
from pipeline.email_reader import fetch_unread_with_attachments
from pipeline.attachment_downloader import download_attachments
from pipeline.text_extractor import extract_text
from pipeline.customer_classifier import classify
from pipeline.template_parser import parse
from pipeline.json_output import build_output, save_json
from pipeline.email_mover import move_to_processed
from config.settings import MOVE_AFTER_PROCESSING
from database import db

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


def process_attachment(client, message, attachment_path) -> bool:
    """Returns True if processing succeeded (used to decide whether to move the email)."""
    msg_id = message["id"]
    filename = attachment_path.name
    sender = message.get("from", {}).get("emailAddress", {}).get("address", "")
    received = message.get("receivedDateTime", "")

    if db.already_processed(msg_id, filename):
        log.info(f"Already processed, skipping: {filename}")
        return True

    try:
        log.info(f"Extracting text: {filename}")
        text, is_native = extract_text(attachment_path)

        log.info(f"Classifying customer for: {filename}")
        customer_name, class_confidence = classify(sender, text)

        log.info(f"Parsing with template: {customer_name!r}")
        parsed = parse(text, customer_name) if customer_name else {"_confidence": 0.0}

        doc = build_output(parsed, customer_name, class_confidence, message, attachment_path)
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

        if doc.needs_review:
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

    client = GraphClient()

    total = 0
    moved = 0

    for message in fetch_unread_with_attachments(client, days=days):
        subject = message.get("subject", "(no subject)")
        msg_id = message["id"]
        log.info(f"Processing email: {subject!r}")

        paths = download_attachments(client, msg_id)
        if not paths:
            log.info(f"No supported attachments in: {subject!r}")
            continue

        all_succeeded = True
        for path in paths:
            success = process_attachment(client, message, path)
            if not success:
                all_succeeded = False
            total += 1

        # Move email only if every attachment processed without error
        if MOVE_AFTER_PROCESSING and all_succeeded:
            if move_to_processed(client, msg_id):
                moved += 1

    log.info(f"Pipeline complete. {total} attachment(s) processed, {moved} email(s) moved.")


if __name__ == "__main__":
    main()
