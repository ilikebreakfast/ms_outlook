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
  ┌─ Sender allowlist check (BEFORE any download) ──────────────┐
  │  Known sender?  ──Yes──▶  continue                          │
  │  Unknown sender? ──No──▶  skip email entirely (no download) │
  └─────────────────────────────────────────────────────────────┘
        │
        ▼
  Download PDFs / images  →  saved to  attachments/
        │
        ▼
  File-level security checks
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
    ├── 1. Exact sender email match  (personal addresses)
    ├── 2. Sender domain match       (business addresses)
    ├── 3. ABN found in document
    └── 4. Keyword matching
        │
        ├─ Match found ──▶  Parse fields using customer template
        │                   → customer_name, abn, address, invoice_number, dates, line_items
        │
        └─ No match ─────▶  Auto-generate suggested template
                            → config/suggested_templates/<sender>.json
                            Review, edit if needed, copy to config/templates/ to activate
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

## Azure Setup

This pipeline authenticates with Microsoft Graph API using an Azure App Registration. You need to create this once — it takes about 10 minutes.

> **Two different Azure portals — don't mix them up:**
> - **https://portal.azure.com** — where you create the App Registration for this pipeline (Microsoft Entra ID / Azure Active Directory). This is what this section covers.
> - **https://dev.azure.com** — Azure DevOps, used for code repositories, CI/CD pipelines, and work items. Not required for this pipeline, but useful if your team wants to host the code there instead of GitHub.

---

### Step 1 — Create the App Registration

1. Go to [https://portal.azure.com](https://portal.azure.com) and sign in
2. Search for **"App registrations"** in the top search bar and open it
3. Click **"New registration"**
4. Fill in:
   - **Name:** `ms_outlook_pipeline` (or any name you'll recognise)
   - **Supported account types:** see [Personal vs Organisational](#personal-vs-organisational-accounts) below
   - **Redirect URI:** select `Web` and enter `http://localhost:3333/auth/callback`
5. Click **Register**
6. On the app overview page, copy the **Application (client) ID** — this is your `MS_CLIENT_ID`

---

### Step 2 — Add API Permissions

1. In your app registration, go to **API permissions** (left menu)
2. Click **"Add a permission"** → **Microsoft Graph** → **Delegated permissions**
3. Search for and add each of these:

   | Permission | Why it's needed |
   |---|---|
   | `Mail.ReadWrite` | Read emails and move them after processing |
   | `User.Read` | Read the logged-in user's profile |
   | `offline_access` | Keep the session alive without re-logging in every time |

4. Click **"Grant admin consent"** if you have admin rights — otherwise ask your Azure admin to do this. Without it, users will see a consent prompt on first login (which is fine for personal accounts).

> **Note:** This pipeline does **not** request `Mail.Send`, `Calendars`, or `Files` permissions — only what it needs.

---

### Step 3 — Enable Public Client Flows

Device code flow (the browser login prompt) is a "public client" flow. Azure blocks it by default and requires you to opt in.

1. In your app registration, go to **Authentication** (left menu)
2. Scroll to **Advanced settings**
3. Set **"Allow public client flows"** to **Yes**
4. Click **Save**

> **If you skip this step** you will get:
> `AADSTS70002: The client application must be marked as 'mobile.'`

Alternatively, set it via the **Manifest** editor:
```json
"allowPublicClient": true
```

---

### Step 4 — Create a Client Secret

The pipeline uses device code flow (browser login) so a client secret is technically optional for personal accounts. However it's good practice to create one in case you switch to app-only auth later.

1. Go to **Certificates & secrets** (left menu)
2. Click **"New client secret"**
3. Set a description and expiry (12–24 months recommended)
4. Click **Add**
5. **Copy the secret VALUE immediately** — it's only shown once. This is not the same as the Secret ID.

If you choose not to create a secret, leave `MS_CLIENT_SECRET` blank in `.env`.

---

### Step 5 — Configure `.env`

```env
MS_CLIENT_ID=paste-your-client-id-here
MS_TENANT_ID=consumers        # for personal accounts — see below
```

---

### Personal vs Organisational Accounts

The **"Supported account types"** setting you choose at registration time controls who can log in, and what `MS_TENANT_ID` you should set.

#### Personal Microsoft Account (e.g. @outlook.com, @hotmail.com)

This is the setup used by this pipeline by default — suitable for personal use or small teams using personal Outlook.

**At registration:** select **"Personal Microsoft accounts only"**

**Known issue with the portal UI:** If you try to change this via the **Authentication** tab, you may see:
> `api.requestedAccessTokenVersion is invalid`

**Fix:** Use the **Manifest** editor instead (left menu → Manifest):
```json
"signInAudience": "PersonalMicrosoftAccount",
"api": {
    "requestedAccessTokenVersion": 2
}
```
Save the Manifest directly — do not use the Authentication tab UI for this change.

**In `.env`:**
```env
MS_TENANT_ID=consumers
```

**Why `consumers`?** Microsoft routes personal account logins through `login.microsoftonline.com/consumers`. Using `/common` will produce this error:
> `AADSTS9002346: Please use the /consumers endpoint`

---

#### Work or School Account (e.g. @yourcompany.com)

For use within a business with an Azure Active Directory (Entra ID) tenant — e.g. if this is deployed for a team or company.

**At registration:** select **"Accounts in this organizational directory only (Single tenant)"**

**In `.env`:**
```env
MS_TENANT_ID=your-tenant-id-here
```

Find your tenant ID: Azure Portal → **Microsoft Entra ID** → **Overview** → Directory (tenant) ID.

**Adding users to the app:**
1. Go to **Enterprise applications** (search in portal)
2. Find your app by name
3. Go to **Users and groups** → **Add user/group**
4. Assign the users who should be able to log in

If you leave "Assignment required" off (default), any user in your organisation can log in automatically.

**Admin consent:** For work accounts, an Azure admin typically needs to grant consent once for the whole organisation:
1. In your app registration → **API permissions**
2. Click **"Grant admin consent for [your organisation]"**
3. Users then log in without seeing a consent prompt

---

#### Both Personal and Work Accounts

**At registration:** select **"Accounts in any organizational directory and personal Microsoft accounts"**

This is what [ryaker/outlook-mcp](https://github.com/ryaker/outlook-mcp) uses by default.

**In `.env`:**
```env
MS_TENANT_ID=common
```

**Caveat:** The `/common` endpoint has quirks. If your app is configured for personal accounts only and you use `/common`, you will get `AADSTS9002346`. Make sure the Manifest `signInAudience` matches the tenant you're using.

---

### Comparison: Personal vs Organisational

| | Personal | Organisational | Both |
|---|---|---|---|
| Account type | @outlook.com, @hotmail.com | @company.com (Azure AD) | Either |
| `MS_TENANT_ID` | `consumers` | your tenant GUID | `common` |
| `signInAudience` (Manifest) | `PersonalMicrosoftAccount` | `AzureADMyOrg` | `AzureADandPersonalMicrosoftAccount` |
| Admin consent required | No | Yes (recommended) | Depends |
| User management | Not applicable | Entra ID → Users and groups | Mixed |
| Good for | Personal/solo use | Business teams | Mixed environments |

---

### Optional: ryaker/outlook-mcp (Node.js MCP Server)

If you also want to control your Outlook from an AI assistant (like Claude), [ryaker/outlook-mcp](https://github.com/ryaker/outlook-mcp) is a companion Node.js server that uses the same Azure App Registration.

```bash
git clone https://github.com/ryaker/outlook-mcp.git
cd outlook-mcp
```

Follow the setup instructions in that repo's README. It uses the same `MS_CLIENT_ID` and `MS_CLIENT_SECRET` — you can point it at the same app registration you created above. The two tools are independent and can run side by side.

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
| Phishing from unknown senders | Sender allowlist checked **before any download** — unknown senders are skipped entirely, no files written to disk |
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

## Personal email senders (hotmail, gmail, etc.)

Business senders are identified by their email domain (e.g. `evergy.com.au`). For personal senders, the domain is shared by millions of people and useless for identification — instead, use the exact email address.

In your customer template, use `sender_emails` instead of `sender_domains`:

```json
{
  "customer_name": "Bonita Hua",
  "sender_emails": ["bonitahua@hotmail.com"],
  "sender_domains": [],
  ...
}
```

The security allowlist works the same way — `bonitahua@hotmail.com` is allowed through even though `hotmail.com` isn't a trusted domain.

**Matching priority:**

| Priority | Strategy | Confidence | Use for |
|---|---|---|---|
| 1 | Exact email address | 98% | Personal senders |
| 2 | Email domain | 95% | Business senders |
| 3 | ABN in document | 90% | Either |
| 4 | Keyword scoring | Variable | Fallback |

See `config/templates/example_personal_contact.json` for a full example.

---

## Suggested templates for unknown senders

When an email comes in from a sender not in any template, the pipeline automatically generates a draft template and saves it to `config/suggested_templates/`. Nothing is parsed for that email, but you get a starting point to work from.

**What gets generated:**

```json
{
  "_status": "SUGGESTED — review and copy to config/templates/ to activate",
  "_generated_at": "2026-04-22 12:00 UTC",
  "_sender_seen": "supplier@newcompany.com.au",
  "_field_examples_found_in_document": {
    "invoice_number_example": "INV00123",
    "order_date_example": "15 Apr 2026",
    "amounts_found": ["1,250.00", "85.00"],
    "address_example": "42 Smith St Sydney NSW 2000"
  },
  "customer_name": "New Company",
  "sender_emails": [],
  "sender_domains": ["newcompany.com.au"],
  "abns": ["12345678901"],
  "keywords": ["newcompany", "order", "supply", ...],
  "fields": {
    "invoice_number": ["(?:Invoice|INV|PO)[\\s#:.]*(\\w{3,20})"],
    ...
  }
}
```

The `_field_examples_found_in_document` section shows **actual values pulled from the PDF** so you can see what the regex needs to match without opening the document yourself.

**To activate a suggested template:**

1. Open `config/suggested_templates/<sender>.json`
2. Check the field examples — adjust any regex patterns that look wrong
3. Copy the file to `config/templates/`
4. Remove the `_status`, `_generated_at`, `_sender_seen`, and `_field_examples_found_in_document` keys (they're just notes)
5. Run the pipeline again — the sender will now be recognised

If the same unknown sender emails again before you activate their template, the suggestion file is updated with any new field examples found — it doesn't overwrite your edits.

> **Suggested templates are gitignored** — they won't be committed to the repo since they may contain customer-specific information.

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
│   ├── templates/                 ← active customer templates
│   │   ├── evergy.json
│   │   ├── example_new_customer.json
│   │   └── example_personal_contact.json
│   └── suggested_templates/       ← auto-generated drafts (gitignored)
│       └── supplier_at_gmail.com.json
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
│   ├── email_mover.py             ← move email after processing
│   └── template_suggester.py      ← auto-generate draft templates for unknown senders
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
