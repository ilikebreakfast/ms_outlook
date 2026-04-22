"""
Fetches unread emails that have attachments from the target mailbox folder.
Read-only. Does NOT mark emails as read.
"""
import logging
from typing import Generator

from auth.graph_client import GraphClient
from config.settings import TARGET_FOLDER

log = logging.getLogger(__name__)


def _get_folder_id(client: GraphClient, folder_name: str) -> str:
    data = client.get("/me/mailFolders", params={"$filter": f"displayName eq '{folder_name}'"})
    folders = data.get("value", [])
    if not folders:
        raise ValueError(f"Mailbox folder not found: {folder_name!r}")
    return folders[0]["id"]


def _messages_url(folder_id: str = None) -> str:
    if folder_id:
        return f"/me/mailFolders/{folder_id}/messages"
    return "/me/messages"


def fetch_unread_with_attachments(client: GraphClient) -> Generator[dict, None, None]:
    """
    Yields unread email message dicts that have at least one attachment.
    Handles pagination automatically.
    """
    folder_id = None
    if TARGET_FOLDER:
        folder_id = _get_folder_id(client, TARGET_FOLDER)

    url = _messages_url(folder_id)
    params = {
        "$filter": "isRead eq false and hasAttachments eq true",
        "$select": "id,subject,from,receivedDateTime,hasAttachments,isRead",
        "$top": 50,
        "$orderby": "receivedDateTime desc",
    }

    while url:
        data = client.get(url, params=params)
        messages = data.get("value", [])
        log.info(f"Fetched {len(messages)} unread emails with attachments.")

        for msg in messages:
            yield msg

        # Follow pagination links
        url = data.get("@odata.nextLink", "").replace("https://graph.microsoft.com/v1.0", "")
        params = None  # nextLink already includes params
