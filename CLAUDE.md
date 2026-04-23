# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

A Python CLI pipeline that reads unread emails from Microsoft Outlook (via Graph API), downloads and security-validates attachments, extracts text via PDF parsing or OCR, classifies the sender against known customer templates, parses structured fields using regex, and outputs JSON. Processed emails are moved to a "Processed-Pipeline" folder.

## Running the Pipeline

```bash
# Native Python
python main.py

# Docker (includes Tesseract and ClamAV)
docker-compose up --build
```

The pipeline interactively prompts for the number of days to look back (default: 1). There is no test suite or linter configured.

## Environment Setup

Copy `.env.example` to `.env` and fill in:
- `MS_CLIENT_ID` and `MS_TENANT_ID` — from Azure app registration
- `TESSERACT_CMD` — path to tesseract binary (or `tesseract` if on PATH)
- `CLAMAV_ENABLED=true/false` — optional antivirus scanning
- `MOVE_AFTER_PROCESSING=true/false` — whether to move emails after processing

For personal Microsoft accounts (Hotmail/Outlook.com), set `MS_TENANT_ID=consumers`.

## Architecture

The pipeline is a sequential, modular chain. Each stage is a separate module in `pipeline/`:

```
Email Fetch (email_reader.py)
  → Sender Allowlist Check (security.py) — skip unknown senders WITHOUT downloading
  → Attachment Download (attachment_downloader.py)
  → Security Validation (security.py) — magic bytes, PDF structure scan, ClamAV
  → Text Extraction (text_extractor.py) — pdfplumber native → PyMuPDF+Tesseract fallback
  → Prompt Injection Scrub (security.py)
  → Customer Classification (customer_classifier.py) — 4-tier matching
  → Field Parsing (template_parser.py) — regex against customer template
  → JSON Output + Pydantic Validation (json_output.py)
  → SQLite Record (database/db.py) — ⚠️ THIS MODULE IS MISSING (see below)
  → Email Move (email_mover.py)
```

`main.py` orchestrates all stages. `auth/graph_client.py` is a thin REST wrapper around Microsoft Graph using MSAL device code flow with disk-cached tokens (`config/token_cache.bin`).

## Customer Templates

Templates live in `config/templates/<name>.json`. They drive both the sender allowlist and field extraction:

```json
{
  "customer_name": "Acme Corp",
  "sender_domains": ["acme.com"],
  "sender_emails": ["billing@personal.com"],
  "abn": "12345678901",
  "keywords": ["invoice", "acme"],
  "fields": {
    "invoice_number": "Invoice\\s*#?\\s*([A-Z0-9-]+)",
    "total_amount": "Total\\s*\\$?([\\d,]+\\.\\d{2})"
  }
}
```

- `sender_domains` — matched against the email domain (business senders)
- `sender_emails` — matched against the exact email address (personal senders like Gmail/Hotmail)
- The 4-tier classification order: exact email → domain → ABN extraction → keyword scoring
- Confidence is the ratio of successfully matched fields to total fields; documents below `LOW_CONFIDENCE_THRESHOLD = 0.6` (in `config/settings.py`) are flagged for manual review

When an email arrives from an unknown sender, `pipeline/template_suggester.py` auto-generates a draft template in `config/suggested_templates/` for the user to review and move into `config/templates/`.

## Known Issue: Missing Database Module

`main.py` imports `from database import db` and calls `db.already_processed(msg_id, filename)` and `db.record(...)`, but `database/db.py` does not exist in the repository. The pipeline will crash at runtime when reaching this code. This SQLite layer needs to be implemented — it should deduplicate runs by message ID + filename and record pipeline metadata (customer, confidence, timestamp, etc.).

## Security Model

The security design is intentional and layered — do not bypass these checks:

1. **Sender allowlist checked before any download** — emails from unknown senders are logged and skipped without touching disk
2. **Magic byte validation** — actual file type verified against extension (not just filename)
3. **PDF structure scan** — checks for embedded JavaScript, auto-actions, and external URIs before opening
4. **ClamAV scan** — optional antivirus, enabled by default in Docker
5. **Prompt injection scrubbing** — text sanitized before any downstream processing

The Docker environment (`python:3.11-slim` + Tesseract + ClamAV) provides additional isolation for processing untrusted PDFs.

## Output Structure

Directories created at runtime (all gitignored):
- `attachments/` — raw downloaded files
- `raw_text/` — extracted text per attachment
- `parsed/` — validated JSON output per document
- `logs/` — rotating log files
- `database/` — SQLite DB (not yet implemented)
