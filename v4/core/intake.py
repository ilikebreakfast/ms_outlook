"""
Shared-mailbox intake layer — delta-poll, deduplication, attachment download.

Design constraints:
  - NEVER writes to the shared mailbox (no mark-as-read, no move, no reply).
  - Deduplication is entirely local: processed internet-message-IDs are stored
    in SQLite, and the @odata.deltaLink is persisted so each poll returns only
    messages added since the previous run.
  - Trigger-agnostic: run_poll() fetches new messages and returns structured
    data.  The caller (main.py / APScheduler) decides the interval.

Security: every attachment passes through security.py before touching disk:
  sender allowlist → filename sanitize → size limit → magic bytes → PDF scan.

First run: no delta link exists → full inbox sync is performed to build the
initial baseline and populate the processed-IDs table.  Attachments from
pre-existing messages are NOT downloaded on first run (they are only marked
as seen so they won't be reprocessed).  Set FIRST_RUN_PROCESS=1 in .env to
override this and process existing messages on the first sync.
"""
import base64
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from v4.core.config import (
    SHARED_MAILBOX_UPN,
    DELTA_LINK_PATH,
    DB_PATH,
    STAGING_DIR,
)
from v4.core.graph_client import GraphClient
from v4.core.security import (
    allowlist_configured,
    allowlist_hint,
    check_magic_bytes,
    check_size,
    is_allowed_sender,
    sanitize_filename,
    scan_pdf_structure,
)

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

_FIRST_RUN_PROCESS = os.getenv("FIRST_RUN_PROCESS", "0") == "1"
_DELTA_SELECT = "id,subject,from,hasAttachments,internetMessageId,receivedDateTime"


# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------

@dataclass
class AttachmentBlob:
    message_id:          str
    internet_message_id: str
    subject:             str
    sender:              str
    received_at:         str
    filename:            str
    content_type:        str
    content_bytes:       bytes
    staging_path:        Path


@dataclass
class PollResult:
    new_messages:           int
    attachments:            list[AttachmentBlob]
    skipped_duplicate:      int
    skipped_no_attachment:  int
    skipped_blocked:        int    # sender blocked / file rejected
    first_run:              bool


# ---------------------------------------------------------------------------
# SQLite dedupe store
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create pipeline tables if they do not already exist."""
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_messages (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                internet_message_id TEXT NOT NULL UNIQUE,
                graph_message_id    TEXT,
                subject             TEXT,
                sender              TEXT,
                received_at         TEXT,
                processed_at        TEXT DEFAULT (datetime('now')),
                status              TEXT DEFAULT 'pending'
            )
        """)
        conn.commit()


def _is_processed(imid: str) -> bool:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM processed_messages WHERE internet_message_id = ?",
            (imid,),
        ).fetchone()
    return row is not None


def _mark_pending(msg: dict) -> None:
    imid   = msg.get("internetMessageId", "")
    sender = (msg.get("from") or {}).get("emailAddress", {}).get("address", "")
    if not imid:
        return
    with _get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO processed_messages
               (internet_message_id, graph_message_id, subject, sender, received_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                imid,
                msg.get("id", ""),
                msg.get("subject", ""),
                sender,
                msg.get("receivedDateTime", ""),
            ),
        )
        conn.commit()


def mark_processed(internet_message_id: str, status: str = "processed") -> None:
    """Called by the pipeline after a message has been handled (any outcome)."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE processed_messages SET status = ? WHERE internet_message_id = ?",
            (status, internet_message_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Delta-link persistence
# ---------------------------------------------------------------------------

def _load_delta_link() -> Optional[str]:
    if DELTA_LINK_PATH.exists():
        try:
            return json.loads(DELTA_LINK_PATH.read_text()).get("deltaLink")
        except Exception:
            pass
    return None


def _save_delta_link(link: str) -> None:
    DELTA_LINK_PATH.parent.mkdir(parents=True, exist_ok=True)
    DELTA_LINK_PATH.write_text(
        json.dumps({
            "deltaLink": link,
            "saved_at":  datetime.now(timezone.utc).isoformat(),
        })
    )


# ---------------------------------------------------------------------------
# Attachment download (with full security gate)
# ---------------------------------------------------------------------------

def _download_attachments(
    client: GraphClient,
    msg: dict,
) -> list[AttachmentBlob]:
    """
    Download eligible attachments for one message through the five-layer
    security gate.  Pure read — never modifies the shared mailbox.
    """
    if not msg.get("hasAttachments"):
        return []

    msg_id = msg["id"]
    imid   = msg.get("internetMessageId", msg_id)

    try:
        resp = client.get(f"/users/{SHARED_MAILBOX_UPN}/messages/{msg_id}/attachments")
    except Exception as e:
        log.warning("Failed to list attachments for message %s: %s", msg_id, e)
        return []

    blobs: list[AttachmentBlob] = []
    for att in resp.get("value", []):
        if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
            continue  # reference / item attachments have no inline bytes

        raw_name = att.get("name", "")

        # --- Layer 2: filename sanitization ---
        safe_name = sanitize_filename(raw_name)
        if safe_name is None:
            log.warning(
                "Attachment rejected (unsafe filename): %r  subject=%r",
                raw_name, msg.get("subject", ""),
            )
            continue

        # --- Layer 3: size limit (check declared size before decoding) ---
        declared_size = att.get("size", 0)
        if not check_size(declared_size, safe_name):
            continue

        content_b64 = att.get("contentBytes", "")
        if not content_b64:
            log.warning("Attachment %s has no contentBytes — skipping", safe_name)
            continue

        content_bytes = base64.b64decode(content_b64)

        # --- Layer 4: magic-byte validation ---
        ext = Path(safe_name).suffix.lower()
        if not check_magic_bytes(content_bytes, ext):
            log.warning(
                "Attachment rejected (magic-byte mismatch): %s  subject=%r",
                safe_name, msg.get("subject", ""),
            )
            continue

        # --- Layer 5: PDF structure scan ---
        if ext == ".pdf":
            dangers = scan_pdf_structure(content_bytes, safe_name)
            if dangers:
                log.warning(
                    "PDF rejected (dangerous constructs: %s): %s  subject=%r",
                    ", ".join(dangers), safe_name, msg.get("subject", ""),
                )
                continue

        # --- Write to staging dir ---
        safe_imid    = imid.replace("<", "").replace(">", "").replace("@", "_at_")
        staging_path = STAGING_DIR / safe_imid / safe_name
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path.write_bytes(content_bytes)

        blobs.append(AttachmentBlob(
            message_id          = msg_id,
            internet_message_id = imid,
            subject             = msg.get("subject", ""),
            sender              = (msg.get("from") or {}).get("emailAddress", {}).get("address", ""),
            received_at         = msg.get("receivedDateTime", ""),
            filename            = safe_name,
            content_type        = att.get("contentType", ""),
            content_bytes       = content_bytes,
            staging_path        = staging_path,
        ))

    return blobs


# ---------------------------------------------------------------------------
# Delta-poll core
# ---------------------------------------------------------------------------

def run_poll() -> PollResult:
    """
    Fetch all messages added to the inbox since the last successful poll.

    On first run (no saved delta link), snapshots the current inbox state
    without downloading attachments unless FIRST_RUN_PROCESS=1.

    Never writes to the shared mailbox.
    """
    client    = GraphClient()
    saved_link = _load_delta_link()
    first_run  = saved_link is None

    # One-time bootstrap warning when the allowlist has not been configured.
    _allowlist_warning_emitted = getattr(run_poll, "_allowlist_warned", False)
    if not allowlist_configured() and not _allowlist_warning_emitted:
        log.warning(
            "\n"
            "  ⚠  No sender allowlist configured — processing mail from ALL senders.\n"
            "     This is safe for initial discovery; lock it down before production.\n"
            "     Set ALLOWED_SENDER_DOMAINS=your-supplier.com in v4/.env\n"
            "     or edit %s (see v4/allowlist.json.example).",
            str(STAGING_DIR.parent / "allowlist.json"),
        )
        run_poll._allowlist_warned = True  # type: ignore[attr-defined]

    start_path: str
    if saved_link:
        start_path = saved_link
        log.info("Delta poll: resuming from saved link")
    else:
        start_path = (
            f"/users/{SHARED_MAILBOX_UPN}/mailFolders/Inbox/messages/delta"
            f"?$select={_DELTA_SELECT}"
        )
        log.info("Delta poll: no saved link — performing initial inbox snapshot")

    new_messages    = 0
    skipped_dup     = 0
    skipped_no_att  = 0
    skipped_blocked = 0
    all_blobs: list[AttachmentBlob] = []
    next_delta_link: Optional[str] = None
    path: Optional[str] = start_path

    while path:
        try:
            resp = client.get(path)
        except Exception as e:
            log.error("Delta poll request failed: %s", e)
            break

        for msg in resp.get("value", []):
            imid = msg.get("internetMessageId", "")
            if not imid:
                continue  # delta tombstone (deleted message)

            if _is_processed(imid):
                skipped_dup += 1
                continue

            sender  = (msg.get("from") or {}).get("emailAddress", {}).get("address", "")
            subject = msg.get("subject", "(no subject)")

            # --- Layer 1: sender allowlist ---
            if allowlist_configured() and not is_allowed_sender(sender):
                log.warning(
                    "\n"
                    "  ⚠  UNKNOWN SENDER: %s\n"
                    "     Subject: %s\n"
                    "%s\n"
                    "     Skipping — no attachments downloaded.",
                    sender, subject, allowlist_hint(sender),
                )
                _mark_pending(msg)
                mark_processed(imid, status="sender_blocked")
                skipped_blocked += 1
                continue

            _mark_pending(msg)

            if first_run and not _FIRST_RUN_PROCESS:
                mark_processed(imid, status="snapshot")
                continue

            if not msg.get("hasAttachments"):
                mark_processed(imid, status="no_attachment")
                skipped_no_att += 1
                continue

            new_messages += 1
            blobs = _download_attachments(client, msg)
            all_blobs.extend(blobs)

        if "@odata.nextLink" in resp:
            path = resp["@odata.nextLink"]
        elif "@odata.deltaLink" in resp:
            next_delta_link = resp["@odata.deltaLink"]
            path = None
        else:
            path = None

    if next_delta_link:
        _save_delta_link(next_delta_link)

    status_parts = [
        f"new={new_messages}",
        f"attachments={len(all_blobs)}",
        f"no-attachment={skipped_no_att}",
        f"duplicate={skipped_dup}",
    ]
    if skipped_blocked:
        status_parts.append(f"sender-blocked={skipped_blocked}")
    if first_run and not _FIRST_RUN_PROCESS:
        status_parts.append("mode=first-run-snapshot")

    log.info("Poll complete — %s", ", ".join(status_parts))

    return PollResult(
        new_messages          = new_messages,
        attachments           = all_blobs,
        skipped_duplicate     = skipped_dup,
        skipped_no_attachment = skipped_no_att,
        skipped_blocked       = skipped_blocked,
        first_run             = first_run,
    )
