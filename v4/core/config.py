"""
Central v4 config — all values loaded from .env (never committed).
Copy v4/.env.example to v4/.env and fill in values before running.

Values are read with os.getenv (returns "" when missing) so that importing
this module never crashes at startup.  Call startup_check(mode) in main.py
before any pipeline work begins — it validates the required keys for the
requested mode and prints a friendly, actionable error if any are missing.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

V4_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(V4_ROOT / ".env")

# ---------------------------------------------------------------------------
# Microsoft Entra — app-only client-credentials (certificate)
# ---------------------------------------------------------------------------
CLIENT_ID  = os.getenv("MS_CLIENT_ID", "")
TENANT_ID  = os.getenv("MS_TENANT_ID", "")
AUTHORITY  = f"https://login.microsoftonline.com/{TENANT_ID}" if TENANT_ID else ""
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]

# Path to the private-key PEM file (gitignored).
# The certificate's public half must be uploaded to the app registration.
_cert_raw  = os.getenv("MS_CERT_PATH", "")
CERT_PATH  = Path(_cert_raw) if _cert_raw else None

# SHA-1 thumbprint (hex). Required when the PEM contains only the private key.
# MSAL can compute it automatically from a full-chain bundle PEM.
CERT_THUMBPRINT = os.getenv("MS_CERT_THUMBPRINT", "")

# ---------------------------------------------------------------------------
# Shared mailbox (read-only — Mail.Read Application permission)
# ---------------------------------------------------------------------------
SHARED_MAILBOX_UPN = os.getenv("SHARED_MAILBOX_UPN", "")

ATTACHMENT_EXTENSIONS: frozenset[str] = frozenset(
    e.strip().lower()
    for e in os.getenv(
        "ATTACHMENT_EXTENSIONS",
        ".pdf,.png,.jpg,.jpeg,.tiff,.bmp,.xlsx,.xls,.csv",
    ).split(",")
    if e.strip()
)

# ---------------------------------------------------------------------------
# Runtime data paths (all inside v4/data/, gitignored)
# ---------------------------------------------------------------------------
V4_DATA_DIR     = V4_ROOT / "data"
DELTA_LINK_PATH = V4_DATA_DIR / "delta_link.json"
DB_PATH         = V4_DATA_DIR / "v4_pipeline.db"
STAGING_DIR     = V4_DATA_DIR / "staging"

# ---------------------------------------------------------------------------
# Sender allowlist
# ---------------------------------------------------------------------------
# Quick config: comma-separated in .env (no file needed).
ALLOWED_SENDER_DOMAINS: frozenset[str] = frozenset(
    d.strip().lower()
    for d in os.getenv("ALLOWED_SENDER_DOMAINS", "").split(",")
    if d.strip()
)
ALLOWED_SENDER_EMAILS: frozenset[str] = frozenset(
    e.strip().lower()
    for e in os.getenv("ALLOWED_SENDER_EMAILS", "").split(",")
    if e.strip()
)
# JSON file for larger allowlists; loaded at runtime by security.py.
ALLOWLIST_PATH = Path(os.getenv("ALLOWLIST_PATH", str(V4_DATA_DIR / "allowlist.json")))

# ---------------------------------------------------------------------------
# Attachment security
# ---------------------------------------------------------------------------
MAX_ATTACHMENT_BYTES = int(os.getenv("MAX_ATTACHMENT_BYTES", str(25 * 1024 * 1024)))  # 25 MB

# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------
POLL_INTERVAL_MINUTES = int(os.getenv("POLL_INTERVAL_MINUTES", "5"))

# ---------------------------------------------------------------------------
# Table math gate
# ---------------------------------------------------------------------------
MATH_GATE_MIN_RATIO     = float(os.getenv("MATH_GATE_MIN_RATIO", "0.8"))
MIN_LINE_ITEMS_FOR_GATE = int(os.getenv("MIN_LINE_ITEMS_FOR_GATE", "1"))

# ---------------------------------------------------------------------------
# Azure Document Intelligence (tier-3, optional)
# ---------------------------------------------------------------------------
AZURE_DI_ENDPOINT = os.getenv("AZURE_DI_ENDPOINT", "")
AZURE_DI_KEY      = os.getenv("AZURE_DI_KEY", "")

# ---------------------------------------------------------------------------
# LLM escalation — Claude Haiku 4.5
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL      = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")


# ---------------------------------------------------------------------------
# Startup validation (call from main.py before any work begins)
# ---------------------------------------------------------------------------
_GRAPH_KEYS = ["MS_CLIENT_ID", "MS_TENANT_ID", "MS_CERT_PATH", "SHARED_MAILBOX_UPN"]
_LLM_KEYS   = ["ANTHROPIC_API_KEY"]


def startup_check(mode: str = "full") -> None:
    """
    Validate required env vars for the given run mode.
    Raises SystemExit with a friendly, actionable message if anything is missing.

    mode = "auth"  → Graph + cert keys only (used by --check-auth)
    mode = "full"  → all keys including LLM   (used by --once / --schedule)
    """
    required = _GRAPH_KEYS if mode == "auth" else _GRAPH_KEYS + _LLM_KEYS
    missing  = [k for k in required if not os.getenv(k)]
    if not missing:
        return

    lines = [
        "",
        "  ─────────────────────────────────────────────────",
        "  Missing required config — pipeline cannot start.",
        "  ─────────────────────────────────────────────────",
        "",
    ]
    for k in missing:
        lines.append(f"    ✗  {k}")
    lines += [
        "",
        "  Fix:  copy v4/.env.example → v4/.env and fill in the values.",
        "        Each key is documented in .env.example.",
        "",
    ]
    raise SystemExit("\n".join(lines))
