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
  Extract text
    ├── Native PDF (pdfplumber)
    └── Scanned PDF or image (PyMuPDF → Tesseract OCR)  →  saved to  raw_text/
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
3. During install, tick **"Add to PATH"** — or set it manually in `.env` (see below)

**Verify install:**
```bash
tesseract --version
```

---

## Setup

### 1. Clone the repo
```bash
git clone https://github.com/ilikebreakfast/ms_outlook.git
cd ms_outlook
```

### 2. Create a virtual environment (recommended)
```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Mac / Linux
source venv/bin/activate
```

### 3. Install dependencies
```bash
pip install -r requirements.txt
```

### 4. Create your .env file
```bash
cp .env.example .env
```

Then open `.env` and fill in your values:

```env
MS_CLIENT_ID=your-azure-client-id
MS_TENANT_ID=consumers
TARGET_FOLDER=
TESSERACT_CMD=tesseract
MOVE_AFTER_PROCESSING=true
PROCESSED_FOLDER_NAME=Processed-Pipeline
```

The `.env` file is gitignored and will never be committed. It keeps your credentials off GitHub.

---

## Configuration

| Setting | Where | What it does |
|---|---|---|
| `MS_CLIENT_ID` | `.env` | Azure app client ID (required) |
| `MS_TENANT_ID` | `.env` | `consumers` for personal Outlook, tenant GUID for work accounts |
| `TARGET_FOLDER` | `.env` | Folder to read from — leave blank for Inbox |
| `TESSERACT_CMD` | `.env` | Full path to tesseract if not on PATH, e.g. `C:\Program Files\Tesseract-OCR\tesseract.exe` |
| `MOVE_AFTER_PROCESSING` | `.env` | `true` moves processed emails to a separate folder (recommended) |
| `PROCESSED_FOLDER_NAME` | `.env` | Name of the destination folder — created automatically if it doesn't exist |
| `LOW_CONFIDENCE_THRESHOLD` | `config/settings.py` | Documents below this score (0.0–1.0) get `needs_review: true` |

---

## Running

```bash
python main.py
```

You'll be prompted at the start:

```
How many days of emails to process? [default: 1]:
```

- Press **Enter** to process only today/yesterday's emails (safe default).
- Type a number (e.g. `7`) to go back further.
- This filters at the server side — your 13,000+ unread emails in the inbox are not all fetched.

**First run only:** You'll see a device login prompt:

```
============================================================
To sign in, use a web browser to open the page https://microsoft.com/devicelogin
and enter the code XXXXXXXX to authenticate.
============================================================
```

1. Open the URL in your browser
2. Enter the code shown
3. Sign in with your Outlook account
4. Return to the terminal — the pipeline continues automatically

**After first login**, the token is cached in `config/token_cache.bin`. Subsequent runs are silent until the token expires (~90 days).

> **Note:** If you change `MS_CLIENT_ID` or the permission scopes, delete `config/token_cache.bin` first so MSAL prompts a fresh login.

---

## Email movement

When `MOVE_AFTER_PROCESSING=true`, each email is moved to the `Processed-Pipeline` folder (or whatever you set) after all its attachments are processed successfully.

- The folder is created automatically if it doesn't exist.
- If any attachment fails, the email stays in place so you can retry.
- This prevents the same email from being picked up on the next run.

To disable, set `MOVE_AFTER_PROCESSING=false`. The pipeline will then rely on SQLite dedup instead — if an attachment has already been recorded with no error, it's skipped.

---

## Output files

For each processed attachment you get three files:

```
attachments/<message_id>/   original file (e.g. INV001.pdf)
raw_text/<message_id>/      extracted text  (e.g. INV001.txt)
parsed/<message_id>/        structured JSON (e.g. INV001.json)
```

**Example JSON output:**
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
   - `sender_domains` — the email domain(s) they send from
   - `abns` — their ABN(s) as 11 digits, no spaces
   - `keywords` — words that appear in their documents
   - `fields` — regex patterns for each data field you want to extract

The pipeline picks up new template files automatically on the next run — no code changes needed.

---

## Checking results

**View flagged documents:**
```bash
sqlite3 database/pipeline.db "SELECT attachment_filename, customer_name, confidence FROM processed_documents WHERE needs_review=1;"
```

**View all runs:**
```bash
sqlite3 database/pipeline.db "SELECT * FROM processed_documents ORDER BY processed_at DESC LIMIT 20;"
```

**View errors:**
```bash
sqlite3 database/pipeline.db "SELECT attachment_filename, error FROM processed_documents WHERE error IS NOT NULL;"
```

**View logs:**
```bash
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
├── .env                           ← your credentials (gitignored, never committed)
├── .env.example                   ← safe template to copy
├── config/
│   ├── settings.py                ← loads .env, defines all config
│   ├── token_cache.bin            ← created on first login (gitignored)
│   └── templates/
│       ├── evergy.json            ← example: Evergy bills
│       └── example_new_customer.json
├── auth/
│   └── graph_client.py            ← Microsoft login + API calls
├── pipeline/
│   ├── email_reader.py            ← fetch emails filtered by date range
│   ├── attachment_downloader.py   ← download PDFs/images
│   ├── text_extractor.py          ← extract text + OCR fallback
│   ├── customer_classifier.py     ← identify customer
│   ├── template_parser.py         ← extract fields using regex templates
│   ├── json_output.py             ← validate + save JSON
│   └── email_mover.py             ← move email after processing
├── database/
│   └── db.py                      ← SQLite (records every run, enables dedup)
├── utils/
│   └── logger.py                  ← logging setup
├── attachments/                   ← original files saved here (gitignored)
├── raw_text/                      ← extracted text saved here (gitignored)
├── parsed/                        ← JSON output saved here (gitignored)
├── logs/                          ← log files (gitignored)
└── database/
    └── pipeline.db                ← created automatically (gitignored)
```
