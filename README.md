# ms_outlook — Email Attachment Extraction Pipeline

Reads unread emails from your Outlook mailbox, downloads PDF and image attachments, extracts text (with OCR fallback for scanned documents), identifies the customer, and saves structured JSON output — all locally, read-only, no cloud services required.

---

## What it does

```
Outlook mailbox (read-only)
        │
        ▼
  Fetch unread emails with attachments
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
3. During install, tick **"Add to PATH"** — or set it manually (see Configuration below)

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

---

## Configuration

All settings live in `config/settings.py`. The defaults work out of the box — only change what you need.

| Setting | Default | What it does |
|---|---|---|
| `CLIENT_ID` | pre-filled | Azure app client ID |
| `TENANT_ID` | `consumers` | Use `consumers` for personal Outlook accounts |
| `TARGET_FOLDER` | `None` (Inbox) | Set to a folder name e.g. `"Orders"` to target a specific folder |
| `TESSERACT_CMD` | `tesseract` | Full path if Tesseract isn't on your PATH, e.g. `C:\Program Files\Tesseract-OCR\tesseract.exe` |
| `LOW_CONFIDENCE_THRESHOLD` | `0.6` | Documents below this score get `needs_review: true` |

You can also set any of these as environment variables instead of editing the file.

---

## Running

```bash
python main.py
```

**First run:** You'll see a message like this in the terminal:

```
============================================================
To sign in, use a web browser to open the page https://microsoft.com/devicelogin
and enter the code XXXXXXXX to authenticate.
============================================================
```

1. Open the URL in your browser
2. Enter the code shown
3. Sign in with your Outlook account (joelwu95@outlook.com)
4. Return to the terminal — the pipeline will continue automatically

**After the first login**, the token is cached in `config/token_cache.bin`. Subsequent runs are fully silent (no browser needed) until the token expires (~90 days).

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

**View logs:**
```bash
tail -f logs/pipeline.log
```

---

## What the pipeline will NOT do

- Mark emails as read
- Delete or move emails
- Write back to any external system
- Send any data outside your machine

All permissions are read-only. The mailbox is never modified.

---

## Folder structure

```
ms_outlook/
├── main.py                        ← run this
├── requirements.txt
├── config/
│   ├── settings.py                ← all configuration
│   ├── token_cache.bin            ← created on first login (do not commit)
│   └── templates/
│       ├── evergy.json            ← example: Evergy bills
│       └── example_new_customer.json
├── auth/
│   └── graph_client.py            ← Microsoft login + API calls
├── pipeline/
│   ├── email_reader.py            ← fetch unread emails
│   ├── attachment_downloader.py   ← download PDFs/images
│   ├── text_extractor.py          ← extract text + OCR fallback
│   ├── customer_classifier.py     ← identify customer
│   ├── template_parser.py         ← extract fields using regex templates
│   └── json_output.py             ← validate + save JSON
├── database/
│   └── db.py                      ← SQLite (records every run)
├── utils/
│   └── logger.py                  ← logging setup
├── attachments/                   ← original files saved here
├── raw_text/                      ← extracted text saved here
├── parsed/                        ← JSON output saved here
├── logs/                          ← log files
└── database/
    └── pipeline.db                ← created automatically
```
