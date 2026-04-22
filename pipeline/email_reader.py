"""
Fetches emails with attachments from the target mailbox folder,
filtered to a given number of days back.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Generator, Optional

from auth.graph_client import GraphClient
from config.settings import TARGET_FOLDER

log = logging.getLogger(__name__)


def _get_folder_id(client: GraphClient, folder_name: str) -> str:
    data = client.get("/me/mailFolders", params={"$filter": f"displayName eq '{folder_name}'"})
    folders = data.get("value", [])
    if not folders:
        raise ValueError(f"Mailbox folder not found: {folder_name!r}")
    return folders[0]["id"]


def _messages_url(folder_id: Optional[str] = None) -> str:
    if folder_id:
        return f"/me/mailFolders/{folder_id}/messages"
    return "/me/messages"


def fetch_unread_with_attachments(
    client: GraphClient, days: int = 1
) -> Generator[dict, None, None]:
    """
    Yields email message dicts that have at least one attachment,
    received within the last `days` days.
    Handles pagination automatically.
    """
    folder_id = None
    if TARGET_FOLDER:
        folder_id = _get_folder_id(client, TARGET_FOLDER)

    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.info(f"Fetching emails with attachments since {since} ({days} day(s)).")

    url = _messages_url(folder_id)
    params = {
        "$filter": f"hasAttachments eq true and receivedDateTime ge {since}",
        "$select": "id,subject,from,receivedDateTime,hasAttachments,isRead",
        "$top": 50,
        "$orderby": "receivedDateTime desc",
    }

    while url:
        data = client.get(url, params=params)
        messages = data.get("value", [])
        log.info(f"Fetched page of {len(messages)} email(s).")

        for msg in messages:
            yield msg

        url = data.get("@odata.nextLink", "").replace("https://graph.microsoft.com/v1.0", "")
        params = None
