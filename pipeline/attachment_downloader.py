"""
Downloads PDF and image attachments from a given email message.
Saves originals to attachments/ for audit trail.
"""
import hashlib
import logging
import base64
import re
from pathlib import Path
from typing import List, Set

from auth.graph_client import GraphClient
from config.settings import ATTACHMENTS_DIR, ATTACHMENT_EXTENSIONS

log = logging.getLogger(__name__)


def _attachment_folder(message_id: str, sender: str, received: str) -> Path:
    """
    Build a human-readable, collision-safe folder:
      attachments/YYYY-MM-DD_<sender-slug>_<8-char-hash>/

    sender:   full email address, e.g. 'noreply@evergy.com.au'
    received: ISO-8601 string from Graph, e.g. '2026-04-24T11:05:00Z'
    """
    date = received[:10] if received else "unknown-date"
    # Use domain part of sender; fall back to full address if no @
    domain = sender.split("@")[-1] if "@" in sender else sender
    slug = re.sub(r"[^\w]", "-", domain).strip("-").lower()[:30]
    short_hash = hashlib.sha1(message_id.encode()).hexdigest()[:8]
    folder = ATTACHMENTS_DIR / f"{date}_{slug}_{short_hash}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def download_attachments(
    client: GraphClient,
    message_id: str,
    sender: str = "",
    received: str = "",
    known_hashes: Set[str] = None,
) -> List[Path]:
    """
    Downloads all PDF/image attachments for a message.
    Returns list of local file paths.

    known_hashes: set of SHA-256 hex digests already in the DB. Attachments
    whose content matches a known hash are skipped without writing to disk —
    this avoids re-downloading identical files from email reply chains.
    """
    data = client.get(f"/me/messages/{message_id}/attachments")
    attachments = data.get("value", [])
    saved = []

    for att in attachments:
        name = att.get("name", "unknown")
        ext = Path(name).suffix.lower()

        if ext not in ATTACHMENT_EXTENSIONS:
            log.debug(f"Skipping {name!r} (unsupported type).")
            continue

        content_bytes = att.get("contentBytes")
        if not content_bytes:
            log.warning(f"No contentBytes for {name!r}, skipping.")
            continue

        raw = base64.b64decode(content_bytes)

        if known_hashes is not None:
            content_hash = hashlib.sha256(raw).hexdigest()
            if content_hash in known_hashes:
                log.info(f"Skipping duplicate attachment (reply chain): {name!r}")
                continue

        folder = _attachment_folder(message_id, sender, received)
        dest = folder / name
        dest.write_bytes(raw)
        log.info(f"Saved attachment: {dest}")
        saved.append(dest)

    return saved
