"""
Moves a processed email to a destination folder using Microsoft Graph API.
Requires Mail.ReadWrite scope.

The destination folder is created automatically if it doesn't exist.
"""
import logging
import requests

from auth.graph_client import GraphClient
from config.settings import PROCESSED_FOLDER_NAME

log = logging.getLogger(__name__)

# Cache folder ID so we only look it up once per run
_folder_id_cache: dict[str, str] = {}


def _get_or_create_folder(client: GraphClient, folder_name: str) -> str:
    if folder_name in _folder_id_cache:
        return _folder_id_cache[folder_name]

    # Try to find existing folder
    data = client.get("/me/mailFolders", params={"$filter": f"displayName eq '{folder_name}'"})
    folders = data.get("value", [])

    if folders:
        folder_id = folders[0]["id"]
        log.info(f"Found existing folder: {folder_name!r}")
    else:
        # Create it
        created = client.post("/me/mailFolders", json={"displayName": folder_name})
        folder_id = created["id"]
        log.info(f"Created new folder: {folder_name!r}")

    _folder_id_cache[folder_name] = folder_id
    return folder_id


def move_to_processed(client: GraphClient, message_id: str) -> bool:
    """
    Moves email to the PROCESSED_FOLDER_NAME folder.
    Returns True on success, False on failure (logs error but does not raise).
    """
    try:
        folder_id = _get_or_create_folder(client, PROCESSED_FOLDER_NAME)
        client.post(f"/me/messages/{message_id}/move", json={"destinationId": folder_id})
        log.info(f"Moved message {message_id[:16]}... to {PROCESSED_FOLDER_NAME!r}")
        return True
    except Exception as exc:
        log.error(f"Failed to move message {message_id[:16]}...: {exc}")
        return False
