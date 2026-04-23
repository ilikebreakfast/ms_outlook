"""
Central config. Values are loaded from .env (never committed).
Copy .env.example to .env and fill in your values before running.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

# --- Microsoft Graph ---
CLIENT_ID = os.environ["MS_CLIENT_ID"]
TENANT_ID = os.getenv("MS_TENANT_ID", "consumers")
SCOPES = ["Mail.ReadWrite", "User.Read"]

# Token cache — persists login between runs, gitignored
TOKEN_CACHE_PATH = ROOT / "config" / "token_cache.bin"

# Fallback: reuse the outlook-mcp Node.js token if device flow is blocked.
# Remove or set to empty string once Azure portal is configured correctly.
_fallback_raw = os.getenv("FALLBACK_TOKEN_PATH", str(Path.home() / ".outlook-mcp-tokens.json"))
FALLBACK_TOKEN_PATH = Path(_fallback_raw) if _fallback_raw else None

# --- Mailbox ---
TARGET_FOLDER = os.getenv("TARGET_FOLDER") or None

ATTACHMENT_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp"}

# --- Storage ---
ATTACHMENTS_DIR = ROOT / "attachments"
RAW_TEXT_DIR    = ROOT / "raw_text"
PARSED_DIR      = ROOT / "parsed"
LOGS_DIR        = ROOT / "logs"
DB_PATH         = ROOT / "database" / "pipeline.db"
TEMPLATES_DIR           = ROOT / "config" / "templates"
SUGGESTED_TEMPLATES_DIR = ROOT / "config" / "suggested_templates"
ADDRESS_BOOK_PATH       = ROOT / "config" / "address_book.json"

# --- OCR ---
TESSERACT_CMD = os.getenv("TESSERACT_CMD", "tesseract")

# --- Email movement ---
MOVE_AFTER_PROCESSING = os.getenv("MOVE_AFTER_PROCESSING", "true").lower() == "true"
PROCESSED_FOLDER_NAME = os.getenv("PROCESSED_FOLDER_NAME", "Processed-Pipeline")

# --- Security ---
# ClamAV: set to "true" to enable AV scanning (requires clamscan on PATH or in container)
CLAMAV_ENABLED = os.getenv("CLAMAV_ENABLED", "false").lower() == "true"
CLAMAV_CMD     = os.getenv("CLAMAV_CMD", "clamscan")

# --- Confidence ---
LOW_CONFIDENCE_THRESHOLD = 0.6

# --- Classification: personal email domains ---
# Senders on these domains are matched by exact email address, not by domain,
# because the domain is shared by millions of unrelated people.
# Override via PERSONAL_EMAIL_DOMAINS env var (comma-separated) to add more.
_extra_personal = os.getenv("PERSONAL_EMAIL_DOMAINS", "")
PERSONAL_EMAIL_DOMAINS: frozenset[str] = frozenset(
    d.strip().lower()
    for d in (
        "gmail.com,hotmail.com,outlook.com,yahoo.com,"
        "live.com,icloud.com,bigpond.com,optusnet.com.au,"
        + _extra_personal
    ).split(",")
    if d.strip()
)

# --- Automation: customer onboarding ---
# Set AUTO_APPROVE_CONFIDENCE=0.85 to automatically promote a suggested template
# to config/templates/ and its address_book_entry to address_book.json when
# field-extraction confidence meets or exceeds this threshold.
# 0 (default) disables auto-approval entirely.
AUTO_APPROVE_CONFIDENCE = float(os.getenv("AUTO_APPROVE_CONFIDENCE", "0"))

# --- Notifications ---
# Optional webhook URL (Slack incoming webhook, n8n, Teams, etc.).
# If set, a run-summary POST is sent after every pipeline run.
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

# Separate channel for urgent alerts (auth failures, large review queue, etc.).
# Falls back to WEBHOOK_URL if not set.
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "") or WEBHOOK_URL

# --- Scheduling (used when --schedule flag is passed to main.py) ---
# Default poll interval in minutes when running in scheduled/daemon mode.
DEFAULT_SCHEDULE_MINUTES = int(os.getenv("DEFAULT_SCHEDULE_MINUTES", "60"))
