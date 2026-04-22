"""
Main pipeline orchestrator. Run this file to process new emails.

  python main.py

Each unread email with attachments is processed:
  1. Attachments downloaded
  2. Text extracted (native PDF or OCR fallback)
  3. Customer identified
  4. Data parsed using customer template
  5. JSON output saved
  6. Result recorded in SQLite
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
from database import db

setup_logging()
log = logging.getLogger(__name__)


def process_attachment(client, message, attachment_path):
    msg_id = message["id"]
    filename = attachment_path.name
    sender = message.get("from", {}).get("emailAddress", {}).get("address", "")
    received = message.get("receivedDateTime", "")

    if db.already_processed(msg_id, filename):
        log.info(f"Already processed, skipping: {filename}")
        return

    try:
        log.info(f"Extracting text: {filename}")
        text, is_native = extract_text(attachment_path)
        raw_text_path = None  # set inside extract_text already

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


def main():
    log.info("Pipeline starting.")
    client = GraphClient()

    total = 0
    for message in fetch_unread_with_attachments(client):
        subject = message.get("subject", "(no subject)")
        msg_id = message["id"]
        log.info(f"Processing email: {subject!r}")

        paths = download_attachments(client, msg_id)
        for path in paths:
            process_attachment(client, message, path)
            total += 1

    log.info(f"Pipeline complete. {total} attachment(s) processed.")


if __name__ == "__main__":
    main()
