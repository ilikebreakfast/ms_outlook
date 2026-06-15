"""
Microsoft Graph API client — app-only client-credentials flow (certificate).

Ported from v1/auth/graph_client.py.  The device-code PublicClientApplication
is replaced by a ConfidentialClientApplication that acquires tokens silently
via the client-credentials grant.  No user interaction; runs fully headless.

MSAL maintains an in-memory token cache inside the app object — the token
(~1 h lifetime) is silently refreshed on the next acquire_token_for_client()
call when it nears expiry.  For a long-running local daemon, this is sufficient.

Retry: all Graph API calls are wrapped with tenacity exponential back-off.
HTTP 429/500/502/503/504 and transient network errors are retried up to 4×;
the Retry-After header is honoured on 429 responses in both seconds and
HTTP-date formats (both are permitted by the spec).
"""
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import msal
import requests
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    before_sleep_log,
)

from v4.core.config import (
    CLIENT_ID, AUTHORITY, GRAPH_SCOPE,
    CERT_PATH, CERT_THUMBPRINT,
    ALERT_WEBHOOK_URL,
)

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
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
    Honour the Retry-After header on 429 responses.
    Graph may send it as seconds ("120") or as an HTTP-date
    ("Wed, 21 Oct 2026 07:28:00 GMT") — both are handled.
    Falls back to exponential back-off capped at 16 s.
    """
    exc = retry_state.outcome.exception()
    if (
        isinstance(exc, requests.HTTPError)
        and exc.response is not None
        and exc.response.status_code == 429
    ):
        header = exc.response.headers.get("Retry-After", "")
        if header:
            # Try seconds format first
            try:
                return max(1.0, float(header))
            except ValueError:
                pass
            # Try HTTP-date format
            try:
                dt   = parsedate_to_datetime(header)
                secs = (dt - datetime.now(timezone.utc)).total_seconds()
                return max(1.0, secs)
            except Exception:
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
# App-only auth
# ---------------------------------------------------------------------------

_app: msal.ConfidentialClientApplication | None = None


def _build_app() -> msal.ConfidentialClientApplication:
    if CERT_PATH is None:
        raise RuntimeError("MS_CERT_PATH is not configured — cannot build MSAL app")
    private_key_pem = CERT_PATH.read_text()
    credential: dict = {"private_key": private_key_pem}
    if CERT_THUMBPRINT:
        credential["thumbprint"] = CERT_THUMBPRINT
    # When the .pem is a bundle (private key + certificate chain), MSAL computes
    # the thumbprint automatically and "thumbprint" can be omitted.
    return msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        client_credential=credential,
    )


def get_access_token() -> str:
    """
    Acquire an app-only access token (client-credentials, certificate).
    Fully silent — no browser, no device code.
    MSAL caches and silently refreshes the token; call this before every request.
    """
    global _app
    if _app is None:
        _app = _build_app()

    result = _app.acquire_token_for_client(scopes=GRAPH_SCOPE)

    if "access_token" not in result:
        # Log only the error code/description — never the full result dict,
        # which may contain diagnostic fields the webhook endpoint shouldn't see.
        err = result.get("error_description") or result.get("error") or "unknown error"
        _send_alert(f"App-only token acquisition failed: {err}")
        raise RuntimeError(f"Graph auth failed: {err}")

    return result["access_token"]


def _send_alert(message: str) -> None:
    """Fire-and-forget webhook alert — must never raise."""
    if not ALERT_WEBHOOK_URL:
        return
    try:
        requests.post(
            ALERT_WEBHOOK_URL,
            json={"text": f"[v4 pipeline] {message}"},
            timeout=5,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# GraphClient
# ---------------------------------------------------------------------------

class GraphClient:
    """Thin REST wrapper around Microsoft Graph with retry and per-call token refresh."""

    def _headers(self) -> dict:
        # Always call get_access_token() — MSAL returns the cached token
        # and refreshes silently ~5 min before expiry.
        return {
            "Authorization": f"Bearer {get_access_token()}",
            "Accept": "application/json",
        }

    @_graph_retry
    def get(self, path: str, params: dict = None) -> dict:
        url = f"{GRAPH_BASE}{path}" if path.startswith("/") else path
        resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    @_graph_retry
    def get_bytes(self, path: str) -> bytes:
        url = f"{GRAPH_BASE}{path}" if path.startswith("/") else path
        resp = requests.get(url, headers=self._headers(), timeout=60)
        resp.raise_for_status()
        return resp.content

    @_graph_retry
    def post(self, path: str, json: dict = None) -> dict:
        url = f"{GRAPH_BASE}{path}" if path.startswith("/") else path
        headers = {**self._headers(), "Content-Type": "application/json"}
        resp = requests.post(url, headers=headers, json=json, timeout=30)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def get_paginated(self, path: str, params: dict = None) -> list[dict]:
        """Follow @odata.nextLink automatically and return all items as a flat list."""
        items: list[dict] = []
        resp = self.get(path, params=params)
        items.extend(resp.get("value", []))
        while next_link := resp.get("@odata.nextLink"):
            resp = self.get(next_link)   # nextLink is absolute; get() handles it
            items.extend(resp.get("value", []))
        return items
