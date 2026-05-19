import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Set

# Ensure v2 root is importable
sys.path.insert(0, str(Path(__file__).parent))

from core import settings
from core.security import validate_attachment, scrub_prompt_injection
from core.extractor import extract_text
from core.parser import generate_layout_hash, DeterministicParser
from core.bootstrapper import bootstrap_template
from core.ingest import (
    GraphClient, fetch_unread_emails, download_email_attachments, get_access_token
)
from database import db

# Setup pipeline logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(settings.LOGS_DIR / "pipeline.log", encoding="utf-8")
    ]
)
log = logging.getLogger("v2_pipeline")


def get_known_hashes() -> Set[str]:
    """Retrieve set of all successfully extracted attachment content hashes to skip duplicates."""
    conn = db.get_db_connection()
    try:
        rows = conn.execute("SELECT DISTINCT filename FROM extraction_history;").fetchall()
        return {r["filename"] for r in rows}
    finally:
        conn.close()


def process_mailbox_run(interactive: bool = True, days: int = 1) -> None:
    """Run one single complete pass of the invoice processing pipeline."""
    log.info("Starting automated v2 pipeline run.")
    
    # 1. Initialize MS Graph API client
    try:
        client = GraphClient(interactive=interactive)
    except Exception as e:
        log.error(f"Failed to acquire Microsoft Graph client: {e}")
        return

    # 2. Fetch completed attachment hashes to avoid reply-chain duplicates
    known_filenames = get_known_hashes()
    
    # 3. Fetch unread emails containing attachments
    unread_emails = list(fetch_unread_emails(client, days=days))
    log.info(f"Identified {len(unread_emails)} unread email(s) to process.")

    for email in unread_emails:
        msg_id = email["id"]
        subject = email.get("subject", "No Subject")
        sender_info = email.get("from", {}).get("emailAddress", {})
        sender_email = sender_info.get("address", "unknown@domain.com")
        received = email.get("receivedDateTime", "")
        
        log.info(f"Processing message '{subject}' from '{sender_email}' (Received: {received})")
        
        # 4. Download approved attachments
        try:
            saved_paths = download_email_attachments(
                client=client,
                message_id=msg_id,
                sender=sender_email,
                received=received
            )
        except Exception as e:
            log.error(f"Failed to download attachments for message {msg_id}: {e}")
            continue

        for path in saved_paths:
            log.info(f"Evaluating attachment file: {path.name}")
            
            # 5. Execute strict security gates
            ok, issues = validate_attachment(path)
            if not ok:
                log.warning(f"Attachment {path.name} failed security gate scan: {issues}. Skipping.")
                hist_id = db.log_extraction(
                    message_id=msg_id,
                    filename=path.name,
                    layout_hash="failed_security",
                    parsed_data={"issues": issues},
                    status="failed"
                )
                db.add_to_review_queue(hist_id, f"Security gate failure: {'; '.join(issues)}")
                continue

            # 6. Extract raw text structure
            try:
                raw_text, is_native = extract_text(path)
                # Scrub prompt injection vectors
                raw_text = scrub_prompt_injection(raw_text)
            except Exception as e:
                log.error(f"Failed text extraction for {path.name}: {e}")
                hist_id = db.log_extraction(
                    message_id=msg_id,
                    filename=path.name,
                    layout_hash="failed_extraction",
                    parsed_data={"error": str(e)},
                    status="failed"
                )
                db.add_to_review_queue(hist_id, f"Text extraction error: {e}")
                continue

            # 7. Generate layout signature fingerprint
            file_type = "pdf" if path.suffix.lower() == ".pdf" else ("excel" if path.suffix.lower() in (".xlsx", ".xls") else "csv")
            layout_hash = generate_layout_hash(raw_text, file_type)
            log.info(f"Document fingerprint established: {layout_hash} ({file_type})")

            # 8. Classification Routing check
            rule_details = db.get_rule_details(layout_hash)
            
            if rule_details:
                status = rule_details["status"]
                
                if status == "active":
                    # --- DETERMINISTIC PARSING ENGINE ---
                    log.info(f"Matching active template rules found. Executing deterministic rule engine.")
                    rules = json.loads(rule_details["extraction_rules"])
                    parser = DeterministicParser(raw_text, rules)
                    
                    extracted, confidence = parser.extract_fields()
                    log.info(f"Extraction completed. Confidence rating: {confidence * 100}%")
                    
                    # Normal active parsing success
                    db.log_extraction(
                        message_id=msg_id,
                        filename=path.name,
                        layout_hash=layout_hash,
                        parsed_data=extracted,
                        status="success"
                    )
                    
                elif status == "pending_approval":
                    log.warning(f"Document matches a template under 'pending_approval' review. Holding.")
                    hist_id = db.log_extraction(
                        message_id=msg_id,
                        filename=path.name,
                        layout_hash=layout_hash,
                        parsed_data=None,
                        status="drifted"
                    )
                    # Add to queue only if not already present
                    pending_queue = db.get_review_queue(status="pending")
                    if not any(q["layout_hash"] == layout_hash for q in pending_queue):
                        db.add_to_review_queue(hist_id, "Matches layout awaiting operator dashboard approval.")
            
            else:
                # --- NEW TEMPLATE ONE-TIME BOOTSTRAPPING ENGINE ---
                log.info(f"Unrecognized layout fingerprint. Triggering one-time LLM bootstrapper.")
                bootstrap_res = bootstrap_template(
                    raw_text=raw_text,
                    document_type=file_type,
                    layout_hash=layout_hash
                )
                
                if bootstrap_res:
                    supplier_name, rules = bootstrap_res
                    hist_id = db.log_extraction(
                        message_id=msg_id,
                        filename=path.name,
                        layout_hash=layout_hash,
                        parsed_data=None,
                        status="drifted"
                    )
                    db.add_to_review_queue(
                        hist_id,
                        f"New layout compiled for '{supplier_name}'. Awaiting visual review."
                    )
                else:
                    log.error(f"One-time bootstrapping failed for unrecognized layout.")
                    hist_id = db.log_extraction(
                        message_id=msg_id,
                        filename=path.name,
                        layout_hash=layout_hash,
                        parsed_data=None,
                        status="failed"
                    )
                    db.add_to_review_queue(hist_id, "One-time template compilation failed.")

        # 9. Mark email as processed in Graph folder (Optional webhook notifications can fire here)
        try:
            # Mark read
            client.post(f"/me/messages/{msg_id}", json_data={"isRead": True})
            log.info(f"Email {msg_id} marked as read.")
        except Exception as e:
            log.warning(f"Failed to update email message state: {e}")

    log.info("Mailbox pass completed successfully.")


def main() -> None:
    """Entry point parsing script CLI parameters."""
    parser = argparse.ArgumentParser(description="Invoice Processing Pipeline v2 Orchestrator")
    parser.add_argument("--check-auth", action="store_true", help="Pre-flight check MSAL cached token")
    parser.add_argument("--days", type=int, default=1, help="Poll lookback period in days (default: 1)")
    parser.add_argument("--schedule", type=int, help="Continually run pipeline loop polling at INTERVAL minutes")
    parser.add_argument("--headless", action="store_true", help="Disallow blocking interactive device logins")
    
    args = parser.parse_args()

    # Preflight Check Auth
    if args.check_auth:
        print("Checking cached authentication state...")
        try:
            token = get_access_token(interactive=not args.headless)
            print("Status: AUTHENTICATED. Active token is cached.")
            sys.exit(0)
        except Exception as e:
            print(f"Status: UNAUTHENTICATED. Auth error: {e}")
            sys.exit(1)

    # Continuous Polling Loop
    if args.schedule:
        log.info(f"Entering continuous scheduling loop (Polling Interval: {args.schedule} minute(s)).")
        while True:
            try:
                process_mailbox_run(interactive=not args.headless, days=args.days)
            except Exception as e:
                log.error(f"Pipeline loop encounter critical uncaught error: {e}")
            log.info(f"Poller sleeping for {args.schedule} minutes...")
            time.sleep(args.schedule * 60)
    else:
        # Run once
        process_mailbox_run(interactive=not args.headless, days=args.days)


if __name__ == "__main__":
    db.init_db()
    main()
