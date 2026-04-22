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

# --- Mailbox ---
TARGET_FOLDER = os.getenv("TARGET_FOLDER") or None

ATTACHMENT_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp"}

# --- Storage ---
ATTACHMENTS_DIR = ROOT / "attachments"
RAW_TEXT_DIR    = ROOT / "raw_text"
PARSED_DIR      = ROOT / "parsed"
LOGS_DIR        = ROOT / "logs"
DB_PATH         = ROOT / "database" / "pipeline.db"
TEMPLATES_DIR   = ROOT / "config" / "templates"

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
