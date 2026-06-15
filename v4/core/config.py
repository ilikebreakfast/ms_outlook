"""
Central v4 config — all values loaded from .env (never committed).
Copy v4/.env.example to v4/.env and fill in values before running.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

V4_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(V4_ROOT / ".env")

# ---------------------------------------------------------------------------
# Microsoft Entra — app-only client-credentials (certificate)
# ---------------------------------------------------------------------------
CLIENT_ID  = os.environ["MS_CLIENT_ID"]
TENANT_ID  = os.environ["MS_TENANT_ID"]
AUTHORITY  = f"https://login.microsoftonline.com/{TENANT_ID}"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]

# Path to the private-key PEM file (gitignored). The cert's public half
# must be uploaded to the app registration in Azure Portal.
CERT_PATH = Path(os.environ["MS_CERT_PATH"])

# SHA-1 thumbprint (hex). Required when the PEM contains only the private key
# and not the full certificate chain. MSAL can compute it from a bundle PEM.
CERT_THUMBPRINT = os.getenv("MS_CERT_THUMBPRINT", "")

# ---------------------------------------------------------------------------
# Shared mailbox (read-only)
# ---------------------------------------------------------------------------
SHARED_MAILBOX_UPN = os.environ["SHARED_MAILBOX_UPN"]

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
V4_DATA_DIR    = V4_ROOT / "data"
DELTA_LINK_PATH = V4_DATA_DIR / "delta_link.json"
DB_PATH        = V4_DATA_DIR / "v4_pipeline.db"
STAGING_DIR    = V4_DATA_DIR / "staging"

# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------
POLL_INTERVAL_MINUTES = int(os.getenv("POLL_INTERVAL_MINUTES", "5"))

# ---------------------------------------------------------------------------
# Table math gate
# ---------------------------------------------------------------------------
# Minimum fraction of checkable rows (those with qty/price/total columns)
# whose math reconciles before we trust the local engine over Azure DI.
MATH_GATE_MIN_RATIO     = float(os.getenv("MATH_GATE_MIN_RATIO", "0.8"))
MIN_LINE_ITEMS_FOR_GATE = int(os.getenv("MIN_LINE_ITEMS_FOR_GATE", "1"))

# ---------------------------------------------------------------------------
# Azure Document Intelligence (tier-3 table fallback — optional)
# ---------------------------------------------------------------------------
AZURE_DI_ENDPOINT = os.getenv("AZURE_DI_ENDPOINT", "")
AZURE_DI_KEY      = os.getenv("AZURE_DI_KEY", "")

# ---------------------------------------------------------------------------
# LLM escalation — Claude Haiku 4.5
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL      = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")
