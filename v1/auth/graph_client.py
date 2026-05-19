"""
Microsoft Graph API client using MSAL device code flow.

Personal Microsoft accounts (consumers tenant) don't support
client credentials flow, so we use interactive device code login.
The token is cached locally so you only need to log in once.

Fallback: if device flow fails (e.g. Azure portal "Allow public client flows"
not yet enabled), the client will attempt to load a token from
FALLBACK_TOKEN_PATH (the outlook-mcp Node.js token file) if it exists
and is not expired.

Retry: all Graph API calls are wrapped with tenacity exponential backoff.
HTTP 429 and 503 are retried up to 3 times; the Retry-After header is
respected when present.
"""
import json
import logging
import sys
import time
from typing import Optional

import msal
import requests
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

from config.settings import (
    CLIENT_ID, TENANT_ID, SCOPES, TOKEN_CACHE_PATH,
    FALLBACK_TOKEN_PATH, ALERT_WEBHOOK_URL,
)

log = logging.getLogger(__name__)

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# HTTP status codes that are safe to retry
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------

def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, requests.HTTPError):
        return exc.response is not None and exc.response.status_code in _RETRYABLE_STATUS
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    return False


def _wait_respecting_retry_after(retry_state) -> float:
    """
    If the last exception was a 429 with a Retry-After header, honour it.
    Otherwise fall back to exponential backoff (1 → 2 → 4 seconds).
    """
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
    # Default: exponential backoff 1s/2s/4s
    return min(2 ** (retry_state.attempt_number - 1), 16)


_graph_retry = retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(4),
    wait=_wait_respecting_retry_after,
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _build_app(cache: msal.SerializableTokenCache) -> msal.PublicClientApplication:
    return msal.PublicClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        token_cache=cache,
    )


def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if TOKEN_CACHE_PATH.exists():
        cache.deserialize(TOKEN_CACHE_PATH.read_text())
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_CACHE_PATH.write_text(cache.serialize())


def _load_fallback_token() -> Optional[str]:
    """
    Attempt to load a valid token from the outlook-mcp Node.js token file.
    Returns the access token string if valid and not expired, else None.
    """
    if not FALLBACK_TOKEN_PATH or not FALLBACK_TOKEN_PATH.exists():
        return None
    try:
        data = json.loads(FALLBACK_TOKEN_PATH.read_text())
        token = data.get("access_token")
        expires_at = data.get("expires_at", 0)
        if token and time.time() < expires_at / 1000:  # Node stores ms
            log.warning(
                f"Using fallback token from {FALLBACK_TOKEN_PATH}. "
                "Fix: enable 'Allow public client flows' in Azure Portal → App Registration → Authentication."
            )
            return token
        if token and time.time() < expires_at:  # Python-written file uses seconds
            log.warning(f"Using fallback token from {FALLBACK_TOKEN_PATH}.")
            return token
    except Exception as e:
        log.debug(f"Fallback token load failed: {e}")
    return None


def _send_auth_alert(message: str) -> None:
    """Fire-and-forget webhook alert for auth events in non-interactive contexts."""
    if not ALERT_WEBHOOK_URL:
        return
    try:
        requests.post(
            ALERT_WEBHOOK_URL,
            json={"text": f"[ms_outlook pipeline] {message}"},
            timeout=5,
        )
    except Exception:
        pass  # alert failure must never crash the pipeline


def get_access_token(interactive: bool = True) -> str:
    """
    Returns a valid access token, prompting device-code login if needed.
    Token is cached so subsequent calls are silent.

    Args:
        interactive: if False (scheduled/headless mode) and device flow is needed,
                     send an alert webhook instead of blocking.
    """
    cache = _load_cache()
    app = _build_app(cache)

    accounts = app.get_accounts()
    result = None

    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])

    if not result:
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            fallback = _load_fallback_token()
            if fallback:
                return fallback
            raise RuntimeError(f"Device flow failed: {flow}")

        if not interactive:
            # Non-interactive context (scheduled run). Alert and bail out.
            msg = (
                "Authentication required but pipeline is running headlessly. "
                f"Please run `python main.py --check-auth` interactively to refresh the token. "
                f"Device code: {flow.get('user_code', 'N/A')}"
            )
            log.error(msg)
            _send_auth_alert(msg)
            raise RuntimeError(msg)

        print("\n" + "=" * 60)
        print(flow["message"])
        print("=" * 60 + "\n")
        result = app.acquire_token_by_device_flow(flow)

    _save_cache(cache)

    if "access_token" not in result:
        raise RuntimeError(f"Auth failed: {result.get('error_description', result)}")

    log.info("Access token acquired.")
    return result["access_token"]


def check_token_health() -> dict:
    """
    Inspect the cached token without making any network call.

    Returns a dict:
        valid         — True if a cached account exists
        account       — email/username of cached account (or "")
        cache_exists  — True if token_cache.bin is present on disk
    """
    cache = _load_cache()
    app = _build_app(cache)
    accounts = app.get_accounts()

    if not accounts:
        return {
            "valid": False,
            "account": "",
            "cache_exists": TOKEN_CACHE_PATH.exists(),
        }

    account = accounts[0]
    return {
        "valid": True,
        "account": account.get("username", ""),
        "cache_exists": True,
    }


# ---------------------------------------------------------------------------
# GraphClient
# ---------------------------------------------------------------------------

class GraphClient:
    """Thin wrapper around Microsoft Graph REST API with retry built in."""

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
    def get_bytes(self, path: str) -> bytes:
        url = f"{GRAPH_BASE}{path}"
        resp = requests.get(url, headers=self._headers(), timeout=60)
        resp.raise_for_status()
        return resp.content

    @_graph_retry
    def post(self, path: str, json: dict = None) -> dict:
        url = f"{GRAPH_BASE}{path}"
        headers = {**self._headers(), "Content-Type": "application/json"}
        resp = requests.post(url, headers=headers, json=json, timeout=30)
        resp.raise_for_status()
        return resp.json() if resp.content else {}
