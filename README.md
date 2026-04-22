# ms_outlook — Email Attachment Extraction Pipeline

Reads emails from your Outlook mailbox, downloads PDF and image attachments, extracts text (with OCR fallback for scanned documents), identifies the customer, and saves structured JSON output — all locally, no cloud services required.

---

## What it does

```
Outlook mailbox
        │
        ▼
  Prompt: how many days of emails to process?
        │
        ▼
  Fetch emails with attachments within that date range
        │
        ▼
  Download PDFs / images  →  saved to  attachments/
        │
        ▼
  Security checks (before any file is opened)
    ├── Sender domain allowlist
    ├── File size limit
    ├── Magic byte validation (real file type check)
    ├── PDF structure scan (embedded JS, auto-actions)
    └── ClamAV antivirus scan (if enabled)
        │
        ▼
  Extract text
    ├── Native PDF (pdfplumber)
    └── Scanned PDF or image (PyMuPDF → Tesseract OCR)  →  saved to  raw_text/
        │
        ▼
  Prompt injection scrubbing (sanitise text before parsing)
        │
        ▼
  Identify customer
    ├── 1. Sender email domain
    ├── 2. ABN found in document
    └── 3. Keyword matching
        │
        ▼
  Parse fields using customer template (config/templates/*.json)
    → customer_name, abn, address, invoice_number, dates, line_items
        │
        ▼
  Save structured JSON  →  parsed/
  Record in SQLite      →  database/pipeline.db
  Log everything        →  logs/pipeline.log
        │
        ▼
  Move processed email to "Processed-Pipeline" folder
  (so it won't be picked up again on the next run)
```

Documents below 60% confidence are flagged `needs_review: true` in the JSON.

---

## Requirements

### Python
Python 3.10 or later. Check with:
```bash
python --version
```

### Tesseract OCR
Required for scanned PDFs and image files.

**Windows:**
1. Download the installer from https://github.com/UB-Mannheim/tesseract/wiki
2. Run the installer (default path: `C:\Program Files\Tesseract-OCR\`)
3. During install, tick **"Add to PATH"** — or set it manually in `.env`

**Verify:**
```bash
tesseract --version
```

---

## Setup

### Option A — Run directly on Windows (simpler)

**1. Clone the repo**
```bash
git clone https://github.com/ilikebreakfast/ms_outlook.git
cd ms_outlook
```

**2. Create a virtual environment**
```bash
python -m venv venv
venv\Scripts\activate
```

**3. Install dependencies**
```bash
pip install -r requirements.txt
```

**4. Create your `.env` file**
```bash
copy .env.example .env
```
Open `.env` and fill in your values (see [Configuration](#configuration) below).

**5. Run**
```bash
python main.py
```

---

### Option B — Run in Docker (recommended for security)

Docker runs the pipeline in an isolated container. Even if a malicious PDF exploited the parser, it cannot reach your host machine. ClamAV is also included automatically — no separate install needed.

**Prerequisites:** [Docker Desktop for Windows](https://www.docker.com/products/docker-desktop/)

**1. Clone and configure**
```bash
git clone https://github.com/ilikebreakfast/ms_outlook.git
cd ms_outlook
copy .env.example .env
# Edit .env with your values
```

**2. Build the image**
```bash
docker compose build
```
This installs Tesseract, ClamAV, and downloads the latest virus signatures. Takes a few minutes on first build; subsequent builds are fast.

**3. Run**
```bash
docker compose run --rm pipeline
```

Output files (`attachments/`, `raw_text/`, `parsed/`, `logs/`, `database/`) are written back to your local machine via volume mounts, so you can access them normally after the container exits.

**First run (login):** The device code prompt will appear in the terminal. Follow the instructions to sign in. The token is saved to `config/token_cache.bin` on your host machine and reused on subsequent runs.

> **Note:** `CLAMAV_ENABLED` is automatically set to `true` inside the Docker container via `docker-compose.yml`. You don't need to change your `.env` for this.

---

## Configuration

All settings go in `.env` (gitignored, never committed).

| Setting | Default | What it does |
|---|---|---|
| `MS_CLIENT_ID` | — | Azure app client ID **(required)** |
| `MS_TENANT_ID` | `consumers` | `consumers` for personal Outlook, tenant GUID for work accounts |
| `TARGET_FOLDER` | *(blank = Inbox)* | Read from a specific folder e.g. `Orders` |
| `TESSERACT_CMD` | `tesseract` | Full path if not on PATH e.g. `C:\Program Files\Tesseract-OCR\tesseract.exe` |
| `CLAMAV_ENABLED` | `false` | Enable ClamAV antivirus scan — set to `true` if installed or using Docker |
| `CLAMAV_CMD` | `clamscan` | Full path to clamscan if not on PATH |
| `MOVE_AFTER_PROCESSING` | `true` | Move emails after processing to prevent reprocessing |
| `PROCESSED_FOLDER_NAME` | `Processed-Pipeline` | Destination folder name (auto-created if missing) |

`LOW_CONFIDENCE_THRESHOLD` (default `0.6`) is in `config/settings.py` — documents below this score are flagged `needs_review: true`.

> **Credentials changed?** Delete `config/token_cache.bin` before the next run so MSAL prompts a fresh login with the updated permissions.

---

## Security

The pipeline processes emails from the internet, which means it will encounter phishing emails and potentially malicious PDFs. The following defences run before any file is opened or parsed.

### What's protected against

| Threat | Defence |
|---|---|
| Phishing from unknown senders | Sender domain allowlist — built from your customer templates automatically |
| Fake file extensions (`.pdf` that's actually `.exe`) | Magic byte check — reads the actual first bytes of the file |
| Malicious PDF with embedded JavaScript | PDF structure scan — rejects any PDF containing `/JS`, `/JavaScript`, `/OpenAction`, `/Launch`, `/EmbeddedFile`, `/XFA` |
| Zip-bomb / memory exhaustion | File size limit (default 20 MB) |
| Malware in attachments | ClamAV antivirus scan (when enabled) |
| Path traversal in filenames (`../../evil.py`) | Filename sanitisation — strips directory components |
| Prompt injection via PDF text | Text scrubbing — removes LLM instruction patterns before the text enters any parser |
| Parser exploitation / sandbox escape | Docker isolation — parser runs in a container with no access to host filesystem beyond mounted output dirs |

### ClamAV (antivirus)

ClamAV is free, open-source antivirus software.

**With Docker (easiest):** ClamAV is installed and enabled automatically inside the container. Nothing extra needed.

**Without Docker (Windows native):**
1. Download from https://www.clamav.net/downloads
2. Install and note the install path (e.g. `C:\ClamAV\`)
3. Run `freshclam.exe` to download virus signatures
4. In `.env` set:
   ```
   CLAMAV_ENABLED=true
   CLAMAV_CMD=C:\ClamAV\clamscan.exe
   ```

If ClamAV is not installed and `CLAMAV_ENABLED=false`, the pipeline skips the AV scan and logs a notice — it won't crash.

### What Docker isolation adds

When you run via Docker (`docker compose run`), the pipeline executes inside a container that:
- Has **no access to your host filesystem** except the specific output folders you've mounted
- Cannot reach other processes on your machine
- Is **destroyed after each run** (`--rm` flag) — no state carries over

This means that even if a zero-day exploit in PyMuPDF or pdfplumber was triggered by a crafted PDF, the attacker would land inside an empty container with nothing useful, not on your desktop.

### What's not covered

- **Emails you deliberately open yourself** — these defences apply to the pipeline only, not your Outlook client
- **Links inside emails** — the pipeline never follows links, only downloads attachments listed by the Graph API
- **Zero-day AV evasion** — ClamAV uses signature-based detection; novel malware may not be caught until signatures are updated (`freshclam`)

---

## Running

```bash
# Native
python main.py

# Docker
docker compose run --rm pipeline
```

You'll be prompted:
```
How many days of emails to process? [default: 1]:
```

- Press **Enter** for the last 24 hours (safe default for daily runs).
- Type a number e.g. `7` to go back further.
- Filtering happens at the Graph API — your full inbox is not fetched.

---

## Output files

For each processed attachment you get three files:

```
attachments/<message_id>/   original file (e.g. INV001.pdf)
raw_text/<message_id>/      extracted text  (e.g. INV001.txt)
parsed/<message_id>/        structured JSON (e.g. INV001.json)
```

**Example JSON:**
```json
{
  "customer_name": "Evergy",
  "abn": "56623005836",
  "address": "1905 36 Walker Street Rhodes NSW 2138",
  "order_date": "07 Apr 2026",
  "requested_delivery_date": "24 Apr 2026",
  "invoice_number": "INV281930",
  "line_items": [],
  "confidence": 0.93,
  "needs_review": false,
  "processed_at": "2026-04-22T03:00:00Z"
}
```

---

## Adding a new customer

1. Copy `config/templates/example_new_customer.json`
2. Rename it to your customer (e.g. `acme.json`)
3. Fill in:
   - `sender_domains` — the email domain(s) they send from (also used for the allowlist)
   - `abns` — their ABN(s) as 11 digits, no spaces
   - `keywords` — words that appear in their documents
   - `fields` — regex patterns for each data field you want to extract

No code changes needed — new templates are picked up automatically on the next run.

---

## Checking results

```bash
# Blocked / failed attachments
sqlite3 database/pipeline.db "SELECT attachment_filename, error FROM processed_documents WHERE error IS NOT NULL;"

# Documents flagged for manual review
sqlite3 database/pipeline.db "SELECT attachment_filename, customer_name, confidence FROM processed_documents WHERE needs_review=1;"

# All recent runs
sqlite3 database/pipeline.db "SELECT * FROM processed_documents ORDER BY processed_at DESC LIMIT 20;"
```

```bash
# Live log
tail -f logs/pipeline.log
```

---

## What the pipeline will NOT do

- Mark emails as read
- Delete emails
- Write back to any external system
- Send any data outside your machine
- Commit your credentials (`.env` is gitignored)

---

## Folder structure

```
ms_outlook/
├── main.py                        ← run this
├── requirements.txt
├── Dockerfile                     ← builds isolated container with ClamAV + Tesseract
├── docker-compose.yml             ← volume mounts, env wiring
├── .dockerignore
├── .env                           ← your credentials (gitignored)
├── .env.example                   ← safe template to copy
├── config/
│   ├── settings.py                ← loads .env, defines all config
│   ├── token_cache.bin            ← created on first login (gitignored)
│   └── templates/
│       ├── evergy.json
│       └── example_new_customer.json
├── auth/
│   └── graph_client.py            ← Microsoft login + API calls
├── pipeline/
│   ├── email_reader.py            ← fetch emails filtered by date range
│   ├── attachment_downloader.py   ← download PDFs/images
│   ├── security.py                ← all security checks (allowlist, AV, magic bytes, etc.)
│   ├── text_extractor.py          ← extract text + OCR fallback
│   ├── customer_classifier.py     ← identify customer
│   ├── template_parser.py         ← extract fields using regex templates
│   ├── json_output.py             ← validate + save JSON
│   └── email_mover.py             ← move email after processing
├── database/
│   └── db.py                      ← SQLite (records every run, enables dedup)
├── utils/
│   └── logger.py
├── attachments/                   ← original files (gitignored)
├── raw_text/                      ← extracted text (gitignored)
├── parsed/                        ← JSON output (gitignored)
├── logs/                          ← log files (gitignored)
└── database/
    └── pipeline.db                ← auto-created (gitignored)
```
