# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

A Python CLI pipeline that reads unread emails from Microsoft Outlook (via Graph API), downloads and security-validates attachments, extracts text via PDF parsing or OCR, classifies the sender against a known contacts address book, parses structured fields using YAML regex templates, and outputs JSON. Processed emails are moved to a "Processed-Pipeline" folder.

## Running the Pipeline

```bash
# Native Python
python main.py

# Docker (includes Tesseract and ClamAV)
docker-compose up --build   # first time only
docker-compose up           # subsequent runs — reuses existing image
```

The pipeline interactively prompts for the number of days to look back (default: 1). There is no test suite or linter configured.

## Management CLI

```bash
# Test a YAML template against a PDF or image
python manage.py test-template evergy invoice.pdf
python manage.py test-template bonitahua scan.png --show-text

# Extract text from a PDF without running the full pipeline
python manage.py parse-pdf invoice.pdf --show-text

# Run a template against an already-extracted text file
python manage.py parse-text topcut raw_text/2026-04-24_.../invoice.txt

# Sender management
python manage.py list-senders --days 30
python manage.py add-sender billing@acme.com.au --name "ACME Corp" --template acme

# Review queue
python manage.py review-queue
python manage.py resolve-review 3 [--dismiss]

# Template drift analysis
python manage.py analyze-template topcut --last 10

# Database
python manage.py sync-db            # backfill parsed_invoices from parsed/**/*.json
python manage.py sync-contacts      # sync address_book.json → contacts table

# Cleanup
python manage.py dedup-attachments --dry-run   # preview duplicate attachment folders
python manage.py dedup-attachments             # remove duplicates, keep earliest copy

# Health check
python manage.py health
```

`--show-text` prints the first 3000 chars of extracted text, which is useful when writing new regex patterns.

## Environment Setup

Copy `.env.example` to `.env` and fill in:
- `MS_CLIENT_ID` and `MS_TENANT_ID` — from Azure app registration
- `TESSERACT_CMD` — full path to the tesseract binary (e.g. `C:\Program Files\Tesseract-OCR\tesseract.exe` on Windows, `/usr/local/bin/tesseract` on Mac). The `pytesseract` Python package is just a wrapper — the binary must be installed separately. If running via Docker, Tesseract is pre-installed and no path is needed.
- `CLAMAV_ENABLED=true/false` — optional antivirus scanning
- `MOVE_AFTER_PROCESSING=true/false` — whether to move emails after processing

For personal Microsoft accounts (Hotmail/Outlook.com), set `MS_TENANT_ID=consumers`.

## Architecture

### Two-Layer Contact System

Sender identity is now separated from parsing rules:

1. **`config/address_book.json`** — the allowlist. Lists every known sender with their `emails`, `domains`, `abns`, `keywords`, and an optional `template` link. This file is checked BEFORE any download.
2. **`config/templates/<name>.yaml`** — parsing rules only. Contains regex `fields`, `required_fields`, and `line_items_pattern`. No sender identity.

A sender in the address book without a linked template still gets their email downloaded and text extracted — saved with `status: "extracted_only"`. The email is still moved to Processed. This lets you register a sender immediately and add the parsing template later.

### Pipeline Flow

```
Email Fetch (email_reader.py)
  → Sender Allowlist Check (security.py) — reads address_book.json, skip unknown senders WITHOUT downloading
  → Attachment Download (attachment_downloader.py) — skips within-run duplicates if DEDUP_REPLY_ATTACHMENTS=true
  → Security Validation (security.py) — magic bytes, PDF structure scan, ClamAV
  → Text Extraction (text_extractor.py) — pdfplumber native → PyMuPDF+Tesseract fallback
  → Prompt Injection Scrub (security.py)
  → Customer Classification (customer_classifier.py) — 4-tier matching against address book contacts
  → [If template linked] Field Parsing (template_parser.py) — regex against YAML template; ABNs auto-normalized to 11 digits
  → [Optional] Claude AI Reviewer (claude_reviewer.py) — fallback when regex confidence < threshold
  → JSON Output + Pydantic Validation (json_output.py) — status: "parsed" | "extracted_only" | "low_confidence"
  → SQLite Record (database/db.py) — deduplicates by (message_id, filename) and content hash
  → Email Move (email_mover.py)
```

`main.py` orchestrates all stages. `auth/graph_client.py` is a thin REST wrapper around Microsoft Graph using MSAL device code flow with disk-cached tokens (`config/token_cache.bin`).

## Address Book Format

`config/address_book.json`:
```json
{
  "contacts": [
    {
      "name": "Evergy",
      "domains": ["evergy.com.au"],
      "abns": ["56623005836"],
      "keywords": ["evergy", "electricity bill"],
      "template": "evergy"
    },
    {
      "name": "Jane Smith",
      "emails": ["jane.smith@example.com"],
      "template": "janesmith"
    }
  ]
}
```

- `domains` — matched against the sender's email domain (business senders)
- `emails` — matched against the exact email address (personal senders like Gmail/Hotmail)
- `template` — optional; the stem of a `.yaml` file in `config/templates/`. Omit to extract text only.

## YAML Template Format

Templates live in `config/templates/<name>.yaml`. Regex patterns use **single backslashes** (no JSON double-escaping):

```yaml
customer_name: Evergy
required_fields: [invoice_number, abn, order_date]
min_line_items: 1   # optional: counts as one extra required slot for confidence scoring
fields:
  invoice_number:
    - 'INV(\d+)'
    - 'invoice.*?(INV\d+)'
  abn:
    - 'ABN\s+(\d{2}\s?\d{3}\s?\d{3}\s?\d{3})'
  amount_due:
    - '\$([\d,]+\.\d{2})'
line_items_patterns:   # list of patterns; results merged and deduplicated
  - '^(?P<qty>\d+)\s+(?P<uom>\w+)\s+(?P<product_code>\d+)\s+(?P<description>.+)$'
```

- Use `line_items_patterns` (list) for multiple line item formats; legacy `line_items_pattern` (single string) still works
- Any field with `abn` in the name is automatically normalized to 11 digits (spaces/punctuation stripped)
- The 4-tier classification order: exact email → domain → ABN extraction → keyword scoring
- Confidence is the ratio of successfully matched `required_fields` to total (plus `min_line_items` slot if set); documents below `LOW_CONFIDENCE_THRESHOLD` (in `config/settings.py`) are flagged for manual review
- Templates are gitignored (`config/templates/*.yaml`) — only `example_*.yaml` files are committed

When an email arrives from an unknown sender, `pipeline/template_suggester.py` auto-generates a draft YAML template in `config/suggested_templates/`. The generated file includes a `_address_book_entry` block showing exactly what to paste into `address_book.json`.

## Database

`database/db.py` is the SQLite persistence layer. Six tables:

| Table | Purpose |
|---|---|
| `processed_documents` | One row per attachment per run; dedup key `(message_id, filename)` + `content_hash` |
| `review_queue` | Entries added when `needs_review=True`; cleared via `manage.py review-queue` |
| `template_stats` | Per-run confidence scores per template; used by `manage.py analyze-template` |
| `parsed_invoices` | All extracted JSON fields; columns added automatically when new fields appear |
| `invoice_lines` | One row per line item; invoice-level fields repeated on each row |
| `contacts` | Synced from `address_book.json` on every run and via `manage.py sync-contacts` |

The schema is auto-migrated on startup — new columns are added via `ALTER TABLE ADD COLUMN` without manual migration scripts.

## Security Model

The security design is intentional and layered — do not bypass these checks:

1. **Sender allowlist checked before any download** — emails from senders not in `address_book.json` are logged and skipped without touching disk
2. **Magic byte validation** — actual file type verified against extension (not just filename)
3. **PDF structure scan** — checks for embedded JavaScript, auto-actions, and external URIs before opening
4. **ClamAV scan** — optional antivirus, enabled by default in Docker
5. **Prompt injection scrubbing** — text sanitized before any downstream processing

The Docker environment (`python:3.11-slim` + Tesseract + ClamAV) provides additional isolation for processing untrusted PDFs.

## Output Structure

Directories created at runtime (all gitignored):
- `attachments/` — raw downloaded files, one subfolder per message (`YYYY-MM-DD_<domain>_<hash>/`)
- `raw_text/` — extracted text per attachment (same folder structure as `attachments/`)
- `parsed/` — validated JSON output per document (same folder structure; includes `status` field)
- `logs/` — rotating log files
- `database/pipeline.db` — SQLite database
