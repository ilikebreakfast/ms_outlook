"""
Microsoft Graph API client using MSAL device code flow.

Personal Microsoft accounts (consumers tenant) don't support
client credentials flow, so we use interactive device code login.
The token is cached locally so you only need to log in once.
"""
import json
import logging
import msal
import requests

from config.settings import CLIENT_ID, TENANT_ID, SCOPES, TOKEN_CACHE_PATH

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


def get_access_token() -> str:
    """
    Returns a valid access token, prompting device-code login if needed.
    Token is cached so subsequent calls are silent.
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
