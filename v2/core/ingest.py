import base64
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Generator, List, Optional, Set

import msal
import requests
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    before_sleep_log,
)

from core.settings import (
    CLIENT_ID, TENANT_ID, SCOPES, TOKEN_CACHE_PATH,
    ATTACHMENTS_DIR, ATTACHMENT_EXTENSIONS, ALERT_WEBHOOK_URL
)

log = logging.getLogger(__name__)

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


# ---------------------------------------------------------------------------
# Retry and Backoff Helpers
# ---------------------------------------------------------------------------

def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, requests.HTTPError):
        return exc.response is not None and exc.response.status_code in RETRYABLE_STATUS
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    return False


def _wait_respecting_retry_after(retry_state) -> float:
    """If the last exception was an HTTP 429 with Retry-After, honour it; else exp backoff."""
    exc = retry_state.outcome.exception()
    if (
        isinstance(exc, requests.HTTPError)
        and exc.response is not None
        and exc.response.status_code == 429
    ):
        retry_after = exc.response.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
    return min(2 ** (retry_state.attempt_number - 1), 16)


_graph_retry = retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(4),
    wait=_wait_respecting_retry_after,
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)


# ---------------------------------------------------------------------------
# Graph Authentication Core
# ---------------------------------------------------------------------------

def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if TOKEN_CACHE_PATH.exists():
        try:
            cache.deserialize(TOKEN_CACHE_PATH.read_text())
        except Exception as e:
            log.warning(f"Failed to deserialise token cache: {e}")
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            TOKEN_CACHE_PATH.write_text(cache.serialize())
        except Exception as e:
            log.warning(f"Failed to serialise token cache: {e}")


def get_access_token(interactive: bool = True) -> str:
    """Acquire token silent or trigger MSAL device login flow."""
    cache = _load_cache()
    app = msal.PublicClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        token_cache=cache,
    )

    accounts = app.get_accounts()
    result = None

    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])

    if not result:
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"MSAL Device Flow initialization failed: {flow}")

        if not interactive:
            msg = (
                "Microsoft Authentication required but running in headless mode.\n"
                f"Action Required: Run 'python -m core.ingest --auth' to complete device authentication.\n"
                f"Device Login Code: {flow.get('user_code')}"
            )
            log.error(msg)
            # Try firing webhook
            if ALERT_WEBHOOK_URL:
                try:
                    requests.post(ALERT_WEBHOOK_URL, json={"text": msg}, timeout=5)
                except Exception:
                    pass
            raise RuntimeError(msg)

        print("\n" + "=" * 80)
        print(flow["message"])
        print("=" * 80 + "\n")
        result = app.acquire_token_by_device_flow(flow)

    _save_cache(cache)

    if "access_token" not in result:
        raise RuntimeError(f"Auth failed: {result.get('error_description', result)}")
    return result["access_token"]


# ---------------------------------------------------------------------------
# Graph API REST Client
# ---------------------------------------------------------------------------

class GraphClient:
    """Wrapper around Microsoft Graph REST endpoints with exponential retries."""

    def __init__(self, interactive: bool = True):
        self._token = get_access_token(interactive=interactive)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    @_graph_retry
    def get(self, path: str, params: dict = None) -> dict:
        url = f"{GRAPH_BASE}{path}"
        resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    @_graph_retry
    def post(self, path: str, json_data: dict = None) -> dict:
        url = f"{GRAPH_BASE}{path}"
        headers = {**self._headers(), "Content-Type": "application/json"}
        resp = requests.post(url, headers=headers, json=json_data, timeout=30)
        resp.raise_for_status()
        return resp.json() if resp.content else {}


# ---------------------------------------------------------------------------
# Email & Attachment Fetching functions
# ---------------------------------------------------------------------------

def fetch_unread_emails(client: GraphClient, days: int = 1) -> Generator[dict, None, None]:
    """Yield unread messages received within the last N days containing attachments."""
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.info(f"Querying unread emails with attachments since {since}.")

    url = "/me/messages"
    params = {
        "$filter": f"hasAttachments eq true and isRead eq false and receivedDateTime ge {since}",
        "$select": "id,subject,from,receivedDateTime,hasAttachments",
        "$top": 50,
    }

    while url:
        data = client.get(url, params=params)
        messages = data.get("value", [])
        log.info(f"Fetched page of {len(messages)} unread email(s).")
        for msg in messages:
            yield msg
        url = data.get("@odata.nextLink", "").replace("https://graph.microsoft.com/v1.0", "")
        params = None


def _attachment_folder(message_id: str, sender: str, received: str) -> Path:
    """Build a human-readable, collision-safe directory slug under attachments/."""
    date = received[:10] if received else "unknown-date"
    domain = sender.split("@")[-1] if "@" in sender else sender
    slug = re.sub(r"[^\w]", "-", domain).strip("-").lower()[:30]
    short_hash = hashlib.sha1(message_id.encode()).hexdigest()[:8]
    folder = ATTACHMENTS_DIR / f"{date}_{slug}_{short_hash}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def download_email_attachments(
    client: GraphClient,
    message_id: str,
    sender: str,
    received: str,
    known_hashes: Optional[Set[str]] = None
) -> List[Path]:
    """Download approved attachments, skipping reply chain duplicates via content hashing."""
    data = client.get(f"/me/messages/{message_id}/attachments")
    attachments = data.get("value", [])
    saved = []

    for att in attachments:
        name = att.get("name", "unknown")
        ext = Path(name).suffix.lower()

        if ext not in ATTACHMENT_EXTENSIONS:
            log.debug(f"Skipping {name!r}: unsupported extension {ext}.")
            continue

        content_bytes = att.get("contentBytes")
        if not content_bytes:
            log.warning(f"No contentBytes for {name!r}, skipping.")
            continue

        raw = base64.b64decode(content_bytes)

        if known_hashes is not None:
            content_hash = hashlib.sha256(raw).hexdigest()
            if content_hash in known_hashes:
                log.info(f"Skipping identical reply chain attachment: {name!r}")
                continue

        folder = _attachment_folder(message_id, sender, received)
        dest = folder / name
        dest.write_bytes(raw)
        log.info(f"Downloaded attachment to: {dest}")
        saved.append(dest)

    return saved


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--auth", action="store_true", help="Refresh MSAL device authentication cache")
    args = parser.parse_args()
    if args.auth:
        print("Starting interactive Graph authentication...")
        token = get_access_token(interactive=True)
        print("Success! Token cached.")
