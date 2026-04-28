"""
Main pipeline orchestrator.

Usage:
    python main.py                    # interactive: prompts for days (TTY only)
    python main.py --days 1           # non-interactive: process last 1 day
    python main.py --days 7           # go back further
    python main.py --schedule 60      # run every 60 minutes (daemon mode)
    python main.py --check-auth       # verify token health and exit
    python main.py --dry-run          # classify emails, write nothing

The sender allowlist is loaded from config/address_book.json BEFORE
downloading any attachments — unknown senders are skipped entirely.
If address_book.json is missing the pipeline will DENY ALL senders and
log an error (use --allow-all-senders to override for testing only).
"""
import argparse
import hashlib
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from utils.logger import setup_logging
from auth.graph_client import GraphClient, check_token_health
from pipeline.email_reader import fetch_unread_with_attachments
from pipeline.attachment_downloader import download_attachments
from pipeline.text_extractor import extract_text
from pipeline.customer_classifier import classify
from pipeline.template_parser import parse, parse_xlsx
from pipeline.json_output import build_output, save_json
from pipeline.email_mover import move_to_processed
from pipeline.security import validate_attachment, scrub_prompt_injection, is_allowed_sender
from pipeline.template_suggester import suggest as suggest_template
from pipeline.claude_reviewer import review as claude_review
from pipeline import metrics as pipeline_metrics
from config.settings import (
    MOVE_AFTER_PROCESSING, TEMPLATES_DIR, ADDRESS_BOOK_PATH,
    DEFAULT_SCHEDULE_MINUTES, CLAUDE_REVIEW_ENABLED, CLAUDE_REVIEW_THRESHOLD,
    DEDUP_REPLY_ATTACHMENTS,
)

from database import db

setup_logging()
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ms_outlook pipeline — email attachment extraction",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--days", type=int, default=None,
        help="How many days of emails to process (default: 1, or interactive prompt on TTY)",
    )
    parser.add_argument(
        "--schedule", type=int, default=None, metavar="MINUTES",
        help="Run in daemon mode, polling every MINUTES minutes",
    )
    parser.add_argument(
        "--check-auth", action="store_true",
        help="Check token health and exit without processing any emails",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch and classify emails but do not write files or move messages",
    )
    parser.add_argument(
        "--allow-all-senders", action="store_true",
        help="Skip the sender allowlist (for testing only — do not use in production)",
    )
    return parser.parse_args()


def _resolve_days(args: argparse.Namespace) -> int:
    """Return the number of days to process, prompting only when on a real TTY."""
    if args.days is not None:
        return max(1, args.days)
    # Interactive fallback — only if we have a real terminal
    if sys.stdin.isatty():
        try:
            raw = input("How many days of emails to process? [default: 1]: ").strip()
            if not raw:
                return 1
            value = int(raw)
            return max(1, value)
        except (ValueError, EOFError):
            print("Invalid input — using default of 1 day.")
    return 1


# ---------------------------------------------------------------------------
# Address book helpers
# ---------------------------------------------------------------------------

def _load_contacts(allow_all: bool = False) -> list[dict]:
    """
    Load contacts from config/address_book.json.

    SECURITY: if the file is missing and allow_all is False (the default),
    returns a sentinel that causes all senders to be denied.  This prevents
    a misconfigured deployment from silently processing emails from anyone.
    """
    if not ADDRESS_BOOK_PATH.exists():
        if allow_all:
            log.warning(
                "config/address_book.json not found and --allow-all-senders is set — "
                "all senders will be allowed (testing mode)."
            )
            return []
        log.error(
            f"config/address_book.json not found at {ADDRESS_BOOK_PATH}. "
            "All senders will be DENIED until the file is created. "
            "Copy config/address_book.example.json to get started."
        )
        # Return a non-empty list with a dummy entry that will never match,
        # ensuring is_allowed_sender() receives non-empty sets and denies all.
        return [{"name": "__deny_all__", "domains": ["__no_match__"], "emails": []}]

    try:
        data = json.loads(ADDRESS_BOOK_PATH.read_text(encoding="utf-8"))
        contacts = data.get("contacts", [])
        log.info(f"Loaded {len(contacts)} contact(s) from address_book.json")
        return contacts
    except Exception as e:
        log.error(f"Failed to load address_book.json: {e} — all senders will be DENIED.")
        return [{"name": "__deny_all__", "domains": ["__no_match__"], "emails": []}]


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
        log.warning("No sender allowlist entries found — all senders will be allowed.")
    return domains, emails


# ---------------------------------------------------------------------------
# Per-attachment processing
# ---------------------------------------------------------------------------

def process_attachment(
    client, message, attachment_path, allowed_domains, allowed_emails,
    contacts, dry_run: bool = False, known_hashes: set = None,
) -> bool:
    """Returns True if processing succeeded (used to decide whether to move the email)."""
    msg_id = message["id"]
    filename = attachment_path.name
    sender = message.get("from", {}).get("emailAddress", {}).get("address", "")
    received = message.get("receivedDateTime", "")

    if db.already_processed(msg_id, filename):
        log.info(f"Already processed, skipping: {filename}")
        return True

    content_hash = hashlib.sha256(attachment_path.read_bytes()).hexdigest()
    if known_hashes is not None and content_hash in known_hashes:
        log.info(f"Duplicate attachment (same content, reply chain?), skipping: {filename}")
        return True

    ok, issues = validate_attachment(attachment_path, sender, allowed_domains, allowed_emails)
    for issue in issues:
        log.warning(f"SECURITY [{filename}]: {issue}")
    if not ok:
        log.warning(f"Attachment blocked by security checks, skipping: {filename}")
        if not dry_run:
            db.record(
                message_id=msg_id,
                attachment_filename=filename,
                sender_email=sender,
                received_at=received,
                processed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                error="BLOCKED: " + " | ".join(issues),
            )
        return False

    try:
        log.info(f"Extracting text: {filename}")
        text, is_native = extract_text(attachment_path)
        text = scrub_prompt_injection(text)

        log.info(f"Classifying sender for: {filename}")
        customer_name, class_confidence, template_name = classify(sender, text, contacts)

        can_parse = False
        if template_name:
            template_path = TEMPLATES_DIR / f"{template_name}.yaml"
            if template_path.exists():
                can_parse = True

        if can_parse:
            log.info(f"Parsing with template: {template_name!r}")
            if attachment_path.suffix.lower() in (".xlsx", ".xls"):
                parsed = parse_xlsx(attachment_path, template_name)
            else:
                parsed = parse(text, template_name)

            # Claude review fallback: supplement regex when confidence is low
            if (
                CLAUDE_REVIEW_ENABLED
                and parsed.get("_confidence", 0.0) < CLAUDE_REVIEW_THRESHOLD
            ):
                log.info(
                    f"Regex confidence {parsed.get('_confidence', 0.0):.0%} below threshold "
                    f"— invoking Claude reviewer for {filename}"
                )
                claude_result = claude_review(
                    text=text,
                    template_name=template_name,
                    attachment_path=attachment_path,
                    is_native=is_native,
                    existing_fields=parsed,
                )
                if claude_result and claude_result.get("_confidence", 0.0) > parsed.get("_confidence", 0.0):
                    parsed = claude_result

            doc = build_output(parsed, customer_name, class_confidence, message, attachment_path)
            # Record template stats for drift detection
            if not dry_run:
                db.record_template_stat(
                    template_name=template_name,
                    confidence=parsed.get("_confidence", 0.0),
                    required_fields_matched=sum(
                        1 for f in parsed if f not in ("_confidence", "line_items") and parsed[f]
                    ),
                    required_fields_total=len(
                        [k for k in parsed if not k.startswith("_") and k != "line_items"]
                    ),
                )
        else:
            log.info(f"No template for {customer_name!r} — saving extracted text only.")

            # Claude review for extracted_only: attempt field extraction without a template
            if CLAUDE_REVIEW_ENABLED:
                log.info(f"Invoking Claude reviewer for no-template sender: {filename}")
                claude_result = claude_review(
                    text=text,
                    template_name=None,
                    attachment_path=attachment_path,
                    is_native=is_native,
                    existing_fields={},
                )
            else:
                claude_result = None

            if claude_result:
                doc = build_output(
                    claude_result, customer_name, class_confidence, message, attachment_path
                )
            else:
                doc = build_output(
                    {}, customer_name, class_confidence, message, attachment_path,
                    status="extracted_only",
                )

            display_name = message.get("from", {}).get("emailAddress", {}).get("name", "")
            suggestion_path = suggest_template(sender, display_name, text)
            if suggestion_path:
                log.info(
                    f"Template suggestion saved to "
                    f"{suggestion_path.relative_to(Path.cwd())} — "
                    "review patterns and copy to config/templates/ to enable parsing."
                )

        if dry_run:
            log.info(
                f"[DRY RUN] Would save: {filename} | "
                f"customer={doc.customer_name} | status={doc.status} | "
                f"conf={doc.confidence}"
            )
            return True

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
            status=doc.status,
            json_path=str(json_path),
            attachment_path=str(attachment_path),
            processed_at=doc.processed_at,
            template_name=template_name,
            content_hash=content_hash,
        )
        try:
            import json as _json
            db.record_parsed_invoice(_json.loads(json_path.read_text(encoding="utf-8")))
        except Exception as _e:
            log.debug(f"parsed_invoices sync skipped for {filename}: {_e}")

        if doc.status == "extracted_only":
            log.info(
                f"Extracted only (no template): {filename} | customer={doc.customer_name} | "
                "Add a YAML template to enable field parsing."
            )
            pipeline_metrics.notify_low_confidence(
                customer_name=doc.customer_name,
                filename=filename,
                confidence=doc.confidence,
                status=doc.status,
                sender_email=sender,
                json_path=str(json_path),
                template_name=template_name,
            )
        elif doc.needs_review:
            log.warning(f"LOW CONFIDENCE ({doc.confidence:.0%}) - flagged for review: {filename}")
            pipeline_metrics.notify_low_confidence(
                customer_name=doc.customer_name,
                filename=filename,
                confidence=doc.confidence,
                status=doc.status,
                sender_email=sender,
                json_path=str(json_path),
                template_name=template_name,
            )
        else:
            log.info(f"Done: {filename} | customer={doc.customer_name} | conf={doc.confidence:.0%}")

        return True

    except Exception as exc:
        log.error(f"Failed to process {filename}: {exc}", exc_info=True)
        if not dry_run:
            db.record(
                message_id=msg_id,
                attachment_filename=filename,
                sender_email=sender,
                received_at=received,
                processed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                error=str(exc),
            )
        return False


# ---------------------------------------------------------------------------
# Single run
# ---------------------------------------------------------------------------

def run_once(days: int, dry_run: bool, allow_all: bool, interactive: bool = True) -> dict:
    """
    Execute one full pipeline pass. Returns a stats dict for metrics/logging.
    """
    run_started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    log.info(
        f"Pipeline starting — processing last {days} day(s) of emails."
        + (" [DRY RUN]" if dry_run else "")
    )

    contacts = _load_contacts(allow_all=allow_all)
    allowed_domains, allowed_emails = _contacts_to_allowlists(contacts)
    try:
        db.sync_contacts(contacts)
    except Exception as _e:
        log.debug(f"contacts sync skipped: {_e}")
    # In-run dedup: track hashes seen this run to skip reply-chain duplicates.
    # Starts empty each run so historical runs are always preserved.
    known_hashes: set[str] = set() if DEDUP_REPLY_ATTACHMENTS else None
    client = GraphClient(interactive=interactive)

    total = 0
    blocked = 0
    moved = 0
    errors = 0
    needs_review_count = 0
    confidences: list[float] = []

    for message in fetch_unread_with_attachments(client, days=days):
        subject = message.get("subject", "(no subject)")
        sender = message.get("from", {}).get("emailAddress", {}).get("address", "")

        if not is_allowed_sender(sender, allowed_domains, allowed_emails):
            log.info(f"Skipping email from unknown sender (no download): {sender!r} | {subject!r}")
            blocked += 1
            continue

        log.info(f"Processing email from {sender!r}: {subject!r}")

        if dry_run:
            paths = []
            log.info(f"[DRY RUN] Would download attachments for: {subject!r}")
        else:
            paths = download_attachments(
                client,
                message["id"],
                sender=sender,
                received=message.get("receivedDateTime", ""),
                known_hashes=known_hashes,
            )

        if not paths and not dry_run:
            log.info(f"No supported attachments in: {subject!r}")
            continue

        all_succeeded = True
        for path in paths:
            success = process_attachment(
                client, message, path, allowed_domains, allowed_emails,
                contacts, dry_run=dry_run, known_hashes=known_hashes,
            )
            if not success:
                all_succeeded = False
                errors += 1
            else:
                total += 1
                # Track hash in-run so later messages in same run also dedup
                if not dry_run:
                    h = hashlib.sha256(path.read_bytes()).hexdigest()
                    known_hashes.add(h)

        if MOVE_AFTER_PROCESSING and all_succeeded and not dry_run:
            if move_to_processed(client, message["id"]):
                moved += 1

    # Gather review queue depth from DB for metrics
    try:
        stats = db.get_run_stats()
        pending_review = stats["pending_review"]
        avg_conf = stats["avg_confidence"]
    except Exception:
        pending_review = 0
        avg_conf = None

    log.info(
        f"Pipeline complete. {total} attachment(s) processed, "
        f"{blocked} blocked/skipped, {errors} error(s), {moved} email(s) moved."
    )

    if not dry_run:
        pipeline_metrics.emit(
            run_started_at=run_started_at,
            days_processed=days,
            total_attachments=total,
            blocked=blocked,
            moved=moved,
            errors=errors,
            needs_review=needs_review_count,
            avg_confidence=avg_conf,
            pending_review_queue=pending_review,
        )

    return {
        "total": total,
        "blocked": blocked,
        "moved": moved,
        "errors": errors,
        "pending_review": pending_review,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()

    # --check-auth: validate token and exit without processing
    if args.check_auth:
        health = check_token_health()
        if health["valid"]:
            print(f"Token OK — account: {health['account']}")
            sys.exit(0)
        else:
            print("No valid token found. Run `python main.py` interactively to authenticate.")
            sys.exit(1)

    days = _resolve_days(args)
    # In scheduled mode the run is non-interactive (token must already exist)
    interactive = args.schedule is None

    if args.schedule is not None:
        interval_minutes = args.schedule or DEFAULT_SCHEDULE_MINUTES
        log.info(f"Scheduled mode: running every {interval_minutes} minute(s).")
        while True:
            try:
                run_once(
                    days=days,
                    dry_run=args.dry_run,
                    allow_all=args.allow_all_senders,
                    interactive=False,
                )
            except RuntimeError as exc:
                # Auth failure in headless mode — already alerted via webhook
                log.error(f"Run aborted: {exc}")
            except Exception as exc:
                log.error(f"Unexpected error during scheduled run: {exc}", exc_info=True)
            log.info(f"Next run in {interval_minutes} minute(s).")
            time.sleep(interval_minutes * 60)
    else:
        run_once(
            days=days,
            dry_run=args.dry_run,
            allow_all=args.allow_all_senders,
            interactive=interactive,
        )


if __name__ == "__main__":
    main()
