"""
Central configuration for Pipeline v2.
Loads settings from .env in the project root.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent.parent
load_dotenv(ROOT / ".env")

# --- App Directories (v2 isolated paths) ---
V2_ROOT = Path(__file__).parent.parent
ATTACHMENTS_DIR = V2_ROOT / "data" / "attachments"
RAW_TEXT_DIR    = V2_ROOT / "data" / "raw_text"
PARSED_DIR      = V2_ROOT / "data" / "parsed"
LOGS_DIR        = V2_ROOT / "data" / "logs"
DB_PATH         = V2_ROOT / "data" / "pipeline.db"

# Create directories on startup
for path in (ATTACHMENTS_DIR, RAW_TEXT_DIR, PARSED_DIR, LOGS_DIR):
    path.mkdir(parents=True, exist_ok=True)

# Allowed attachment extensions
ATTACHMENT_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".xlsx", ".xls", ".csv"}

# --- Microsoft Graph API ---
CLIENT_ID = os.getenv("MS_CLIENT_ID", "")
TENANT_ID = os.getenv("MS_TENANT_ID", "consumers")
SCOPES = ["Mail.ReadWrite", "User.Read"]
TOKEN_CACHE_PATH = V2_ROOT / "data" / "token_cache.bin"

# --- Security Scanner ---
CLAMAV_ENABLED = os.getenv("CLAMAV_ENABLED", "false").lower() == "true"
CLAMAV_CMD     = os.getenv("CLAMAV_CMD", "clamscan")
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "20"))

# --- OCR Engine ---
TESSERACT_CMD = os.getenv("TESSERACT_CMD", "tesseract")

# --- Ingestion & Polling ---
MOVE_AFTER_PROCESSING = os.getenv("MOVE_AFTER_PROCESSING", "true").lower() == "true"
PROCESSED_FOLDER_NAME = os.getenv("PROCESSED_FOLDER_NAME", "Processed-Pipeline")
DEFAULT_SCHEDULE_MINUTES = int(os.getenv("DEFAULT_SCHEDULE_MINUTES", "60"))

# --- Model Selection & API Keys (One-time Bootstrapping) ---
# Supports either Deepseek API or Anthropic Haiku.
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Webhooks for HITL review
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
