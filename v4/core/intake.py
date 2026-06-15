"""
Shared-mailbox intake layer — delta-poll, deduplication, attachment download.

Design constraints:
  - NEVER writes to the shared mailbox (no mark-as-read, no move, no reply).
  - Deduplication is entirely local: processed internet-message-IDs are stored
    in SQLite, and the @odata.deltaLink is persisted so each poll returns only
    messages added since the previous run.
  - Trigger-agnostic: run_poll() fetches new messages and returns structured
    data.  The caller (main.py / APScheduler) decides the interval.

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
    ATTACHMENT_EXTENSIONS,
)
from v4.core.graph_client import GraphClient

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# On the very first run (no delta link), process existing messages only if
# explicitly requested; otherwise just snapshot the inbox state.
_FIRST_RUN_PROCESS = os.getenv("FIRST_RUN_PROCESS", "0") == "1"

# Fields fetched on every delta call — keep minimal to reduce payload size.
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
    """Create the processed_messages table if it does not already exist."""
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
    """Insert a new row in pending state; ignore if already present (IGNORE)."""
    imid = msg.get("internetMessageId", "")
    if not imid:
        return
    sender = (msg.get("from") or {}).get("emailAddress", {}).get("address", "")
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
            data = json.loads(DELTA_LINK_PATH.read_text())
            return data.get("deltaLink")
        except Exception:
            pass
    return None


def _save_delta_link(link: str) -> None:
    DELTA_LINK_PATH.parent.mkdir(parents=True, exist_ok=True)
    DELTA_LINK_PATH.write_text(
        json.dumps({
            "deltaLink": link,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        })
    )


# ---------------------------------------------------------------------------
# Attachment download
# ---------------------------------------------------------------------------

def _download_attachments(client: GraphClient, msg: dict) -> list[AttachmentBlob]:
    """
    Download all eligible file attachments for one message.
    Pure read — never POSTs or modifies anything in the mailbox.
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
            continue  # reference attachments have no inline bytes

        name = att.get("name", "")
        ext  = Path(name).suffix.lower()
        if ext not in ATTACHMENT_EXTENSIONS:
            log.debug("Skipping %s (unsupported extension %s)", name, ext)
            continue

        content_b64 = att.get("contentBytes", "")
        if not content_b64:
            log.warning("Attachment %s has no contentBytes — skipping", name)
            continue

        content_bytes = base64.b64decode(content_b64)

        # Stage to disk so downstream stages can access the file path
        safe_imid = imid.replace("<", "").replace(">", "").replace("@", "_at_")
        staging_path = STAGING_DIR / safe_imid / name
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path.write_bytes(content_bytes)

        blobs.append(AttachmentBlob(
            message_id=msg_id,
            internet_message_id=imid,
            subject=msg.get("subject", ""),
            sender=(msg.get("from") or {}).get("emailAddress", {}).get("address", ""),
            received_at=msg.get("receivedDateTime", ""),
            filename=name,
            content_type=att.get("contentType", ""),
            content_bytes=content_bytes,
            staging_path=staging_path,
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
    client   = GraphClient()
    saved_link = _load_delta_link()
    first_run  = saved_link is None

    if saved_link:
        # saved_link is an absolute URL — GraphClient.get() accepts absolute URLs
        start_path: str = saved_link
        log.info("Delta poll: resuming from saved link")
    else:
        start_path = (
            f"/users/{SHARED_MAILBOX_UPN}/mailFolders/Inbox/messages/delta"
            f"?$select={_DELTA_SELECT}"
        )
        log.info("Delta poll: no saved link — performing initial inbox snapshot")

    new_messages  = 0
    skipped_dup   = 0
    skipped_no_att = 0
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
                # Delta tombstone for a deleted message — ignore
                continue

            if _is_processed(imid):
                skipped_dup += 1
                continue

            _mark_pending(msg)

            if first_run and not _FIRST_RUN_PROCESS:
                # Snapshot mode: mark seen without downloading
                mark_processed(imid, status="snapshot")
                continue

            if not msg.get("hasAttachments"):
                mark_processed(imid, status="no_attachment")
                skipped_no_att += 1
                continue

            new_messages += 1
            blobs = _download_attachments(client, msg)
            all_blobs.extend(blobs)

        # Advance: nextLink = more pages; deltaLink = end-of-results cursor
        if "@odata.nextLink" in resp:
            path = resp["@odata.nextLink"]
        elif "@odata.deltaLink" in resp:
            next_delta_link = resp["@odata.deltaLink"]
            path = None
        else:
            path = None

    if next_delta_link:
        _save_delta_link(next_delta_link)

    log.info(
        "Poll complete — new=%d, no-attachment=%d, duplicate=%d, attachments=%d%s",
        new_messages, skipped_no_att, skipped_dup, len(all_blobs),
        " [first-run snapshot]" if first_run and not _FIRST_RUN_PROCESS else "",
    )
    return PollResult(
        new_messages=new_messages,
        attachments=all_blobs,
        skipped_duplicate=skipped_dup,
        skipped_no_attachment=skipped_no_att,
        first_run=first_run,
    )
