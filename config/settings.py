"""
Central config. Edit values here or set environment variables.
All paths are relative to project root.
"""
import os
from pathlib import Path

ROOT = Path(__file__).parent.parent

# --- Microsoft Graph ---
CLIENT_ID = os.getenv("MS_CLIENT_ID", "b0e9cc23-524a-48b7-bacb-0b76f62c9ceb")
TENANT_ID = os.getenv("MS_TENANT_ID", "consumers")
SCOPES = ["Mail.ReadWrite", "User.Read"]

# Token cache file (persists login between runs)
TOKEN_CACHE_PATH = ROOT / "config" / "token_cache.bin"

# --- Mailbox ---
# Set to a folder name like "Inbox" or a folder ID.
# Leave as None to use the default Inbox.
TARGET_FOLDER = os.getenv("TARGET_FOLDER", None)

# Only process emails where has_attachments = True
ATTACHMENT_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp"}

# --- Storage ---
ATTACHMENTS_DIR = ROOT / "attachments"
RAW_TEXT_DIR    = ROOT / "raw_text"
PARSED_DIR      = ROOT / "parsed"
LOGS_DIR        = ROOT / "logs"
DB_PATH         = ROOT / "database" / "pipeline.db"

TEMPLATES_DIR   = ROOT / "config" / "templates"

# --- OCR ---
# Path to tesseract executable. Adjust if not on PATH.
TESSERACT_CMD = os.getenv("TESSERACT_CMD", "tesseract")

# --- Email movement ---
# After successful processing, move emails to this folder.
# Set to None to disable (emails stay in place, SQLite dedup prevents reprocessing).
MOVE_AFTER_PROCESSING = os.getenv("MOVE_AFTER_PROCESSING", "true").lower() == "true"
PROCESSED_FOLDER_NAME = os.getenv("PROCESSED_FOLDER_NAME", "Processed-Pipeline")

# --- Confidence ---
# Documents below this threshold are flagged for manual review (0.0 - 1.0)
LOW_CONFIDENCE_THRESHOLD = 0.6
