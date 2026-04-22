"""
Microsoft Graph API client using MSAL device code flow.

Personal Microsoft accounts (consumers tenant) don't support
client credentials flow, so we use interactive device code login.
The token is cached locally so you only need to log in once.

Fallback: if device flow fails (e.g. Azure portal "Allow public client flows"
not yet enabled), the client will attempt to load a token from
FALLBACK_TOKEN_PATH (the outlook-mcp Node.js token file) if it exists
and is not expired.
"""
import json
import logging
import time
import msal
import requests

from config.settings import CLIENT_ID, TENANT_ID, SCOPES, TOKEN_CACHE_PATH, FALLBACK_TOKEN_PATH

log = logging.getLogger(__name__)

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


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
        TOKEN_CACHE_PATH.write_text(cache.serialize())


def _load_fallback_token() -> str | None:
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
        if token and time.time() < expires_at / 1000:  # Node stores ms, Python uses seconds
            log.warning(
                f"Using fallback token from {FALLBACK_TOKEN_PATH}. "
                "Fix: enable 'Allow public client flows' in Azure Portal → App Registration → Authentication."
            )
            return token
        # expires_at might already be in seconds (Python-written file)
        if token and time.time() < expires_at:
            log.warning(f"Using fallback token from {FALLBACK_TOKEN_PATH}.")
            return token
    except Exception as e:
        log.debug(f"Fallback token load failed: {e}")
    return None


def get_access_token() -> str:
    """
    Returns a valid access token, prompting device-code login if needed.
    Token is cached so subsequent calls are silent.

    Falls back to the outlook-mcp token file if device flow is blocked
    (Azure portal 'Allow public client flows' not yet enabled).
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
            # Device flow blocked — try fallback token before raising
            fallback = _load_fallback_token()
            if fallback:
                return fallback
            raise RuntimeError(f"Device flow failed: {flow}")

        print("\n" + "=" * 60)
        print(flow["message"])
        print("=" * 60 + "\n")
        result = app.acquire_token_by_device_flow(flow)

    _save_cache(cache)

    if "access_token" not in result:
        raise RuntimeError(f"Auth failed: {result.get('error_description', result)}")

    log.info("Access token acquired.")
    return result["access_token"]


class GraphClient:
    """Thin wrapper around Microsoft Graph REST API."""

    def __init__(self):
        self._token = get_access_token()

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    def get(self, path: str, params: dict = None) -> dict:
        url = f"{GRAPH_BASE}{path}"
        resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_bytes(self, path: str) -> bytes:
        url = f"{GRAPH_BASE}{path}"
        resp = requests.get(url, headers=self._headers(), timeout=60)
        resp.raise_for_status()
        return resp.content

    def post(self, path: str, json: dict = None) -> dict:
        url = f"{GRAPH_BASE}{path}"
        headers = {**self._headers(), "Content-Type": "application/json"}
        resp = requests.post(url, headers=headers, json=json, timeout=30)
        resp.raise_for_status()
        return resp.json() if resp.content else {}
