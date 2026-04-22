"""
Downloads PDF and image attachments from a given email message.
Saves originals to attachments/ for audit trail.
"""
import logging
import base64
from pathlib import Path
from typing import List

from auth.graph_client import GraphClient
from config.settings import ATTACHMENTS_DIR, ATTACHMENT_EXTENSIONS

log = logging.getLogger(__name__)


def _safe_filename(message_id: str, filename: str) -> Path:
    """Build a collision-safe local path: attachments/<msg_id>/<filename>"""
    folder = ATTACHMENTS_DIR / message_id[:16]
    folder.mkdir(parents=True, exist_ok=True)
    return folder / filename


def download_attachments(client: GraphClient, message_id: str) -> List[Path]:
    """
    Downloads all PDF/image attachments for a message.
    Returns list of local file paths.
    Read-only - does not alter the email.
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

        dest = _safe_filename(message_id, name)
        dest.write_bytes(base64.b64decode(content_bytes))
        log.info(f"Saved attachment: {dest}")
        saved.append(dest)

    return saved
