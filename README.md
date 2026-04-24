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
  Download PDFs / images / Excel files  →  saved to  attachments/
        │
        ▼
  File-level security checks
    ├── File size limit
    ├── Magic byte validation (real file type check, including .xlsx ZIP header)
    ├── PDF structure scan (embedded JS, auto-actions — skipped for Excel)
    └── ClamAV antivirus scan (if enabled)
        │
        ▼
  Extract text
    ├── Native PDF (pdfplumber)
    ├── Scanned PDF or image (PyMuPDF → Tesseract OCR)
    └── Excel .xlsx (openpyxl → tab-separated flat text)  →  saved to  raw_text/
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
        ├─ Match found, template linked ──▶  Parse fields using YAML template
        │                                   → customer_name, abn, address, invoice_number, dates, line_items
        │
        ├─ Match found, no template yet ──▶  Save raw text only  (status: extracted_only)
        │                                   → email still moved to Processed
        │                                   → add a YAML template later to enable parsing
        │
        └─ No match ──────────────────────▶  Auto-generate suggested YAML template
                                            → config/suggested_templates/<sender>.yaml
                                            → includes _address_book_entry snippet to copy
                                            Add to address_book.json + copy template to activate
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

### openpyxl (Excel support)
Required for `.xlsx` attachments. Installed automatically via `requirements.txt` (`pip install -r requirements.txt`). No binary install needed.

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

> **"Tesseract is not installed or it's not in your PATH"** — `pytesseract` is just a Python wrapper; the actual Tesseract binary must be installed separately. Set the full path in `.env`:
> ```env
> # Windows
> TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
> # Mac (Homebrew)
> TESSERACT_CMD=/usr/local/bin/tesseract
> # Linux
> TESSERACT_CMD=/usr/bin/tesseract
> ```
> If running via Docker, Tesseract is pre-installed in the container — no `.env` change needed.

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

**2. Build the image (first time only)**
```bash
docker compose build
```
This installs Tesseract, ClamAV, and downloads the latest virus signatures. Takes a few minutes the first time. You only need to re-run this if `Dockerfile` or `requirements.txt` changes.

**3. Run**
```bash
docker compose run --rm pipeline
```
For every run after the first, skip the build step — just run `docker compose run --rm pipeline` directly. The image is reused as-is.

To refresh ClamAV signatures without a full rebuild:
```bash
docker compose run --rm pipeline freshclam
```

Output files (`attachments/`, `raw_text/`, `parsed/`, `logs/`, `database/`) are written back to your local machine via volume mounts, so you can access them normally after the container exits.

**First run (login):** The device code prompt will appear in the terminal. Follow the instructions to sign in. The token is saved to `config/token_cache.bin` on your host machine and reused on subsequent runs.

> **Note:** `CLAMAV_ENABLED` is automatically set to `true` inside the Docker container via `docker-compose.yml`. You don't need to change your `.env` for this.

---

## Configuration

All settings go in `.env` (gitignored, never committed).

### Core

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

### Scheduling

| Setting | Default | What it does |
|---|---|---|
| `DEFAULT_SCHEDULE_MINUTES` | `60` | Poll interval when running with `--schedule` or via the Docker scheduler service |

### Automation

| Setting | Default | What it does |
|---|---|---|
| `AUTO_APPROVE_CONFIDENCE` | `0` | When `> 0`, automatically promote a suggested template to active status if its field-extraction confidence meets this threshold. `0.85` is a reasonable starting point. `0` disables auto-approval entirely. |
| `PERSONAL_EMAIL_DOMAINS` | *(built-in list)* | Extra comma-separated domains treated as personal (matched by exact email, not domain). Built-in: `gmail.com`, `hotmail.com`, `outlook.com`, `yahoo.com`, `live.com`, `icloud.com`, `bigpond.com`, `optusnet.com.au` |

### Notifications

| Setting | Default | What it does |
|---|---|---|
| `WEBHOOK_URL` | *(blank)* | POST a run summary here after every pipeline run. Supports Slack, Teams, n8n, and any webhook-accepting service. |
| `ALERT_WEBHOOK_URL` | *(falls back to `WEBHOOK_URL`)* | Separate URL for urgent alerts: auth failures, large review queue, new unknown senders. |

`LOW_CONFIDENCE_THRESHOLD` (default `0.6`) is in `config/settings.py` — documents below this score are flagged `needs_review: true`.

> **Credentials changed?** Delete `config/token_cache.bin` before the next run so MSAL prompts a fresh login with the updated permissions.

---

## Security

The pipeline processes emails from the internet, which means it will encounter phishing emails and potentially malicious PDFs. The following defences run before any file is opened or parsed.

### What's protected against

| Threat | Defence |
|---|---|
| Phishing from unknown senders | Sender allowlist checked **before any download** — unknown senders are skipped entirely, no files written to disk. If `address_book.json` is missing or corrupt, **all senders are denied** (fail-closed). |
| Fake file extensions (`.pdf` that's actually `.exe`) | Magic byte check — reads the actual first bytes of the file. `.xlsx` files are validated against the ZIP magic bytes (`PK\x03\x04`) that OOXML requires. |
| Malicious PDF with embedded JavaScript | PDF structure scan — rejects PDFs containing `/JS`, `/JavaScript`, `/Launch`, `/XFA`. `/AA` and `/OpenAction` are only blocked when an execution payload is also present (standalone triggers in legitimate POS receipts are allowed through as warnings). Excel files skip the PDF scan entirely. |
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
# Native — interactive (prompts for days on a real terminal)
python main.py

# Native — non-interactive (safe to call from scripts/cron)
python main.py --days 1

# Docker — one-shot run
docker compose run --rm pipeline --days 1
```

**CLI flags:**

| Flag | Default | Description |
|---|---|---|
| `--days N` | `1` (or prompt on TTY) | How many days of emails to process |
| `--schedule MINUTES` | off | Run continuously, polling every N minutes |
| `--check-auth` | off | Verify token health and exit — no emails processed |
| `--dry-run` | off | Classify emails but write no files and move nothing |
| `--allow-all-senders` | off | Skip the sender allowlist (testing only) |

When `--days` is not set and you are on a real terminal, the prompt still appears:
```
How many days of emails to process? [default: 1]:
```
When called from a script (no TTY), it defaults silently to 1 day.

---

## Scheduled / Automated Runs

### Native (cron / Task Scheduler)

```bash
# Verify the token is cached before scheduling
python main.py --check-auth

# Cron example: run every hour, process last 1 day
0 * * * * cd /path/to/ms_outlook && python main.py --days 1 >> logs/cron.log 2>&1
```

If the token expires (~90 days inactivity), the pipeline will log an error and send a webhook alert (if configured) rather than hanging waiting for a browser.

### Docker — scheduler service

```bash
# Start the background scheduler (runs every DEFAULT_SCHEDULE_MINUTES minutes)
docker compose up -d scheduler

# Follow logs
docker compose logs -f scheduler

# Stop
docker compose down scheduler
```

The `scheduler` service runs non-interactively with `restart: unless-stopped`.
It requires a valid `config/token_cache.bin` already on disk — authenticate first by running the `pipeline` service at least once:

```bash
docker compose run --rm pipeline --check-auth   # prompts login if needed
docker compose up -d scheduler                   # then start the daemon
```

Set `DEFAULT_SCHEDULE_MINUTES` in `.env` to control frequency (default: `60`).

---

## Output files

For each processed attachment you get three files:

Folders are named `YYYY-MM-DD_<sender-domain>_<hash>/` — one folder per email, human-readable:

```
attachments/2026-04-24_acmecorp-com-au_a3f1b2c4/   original files
raw_text/2026-04-24_acmecorp-com-au_a3f1b2c4/       extracted text
parsed/2026-04-24_acmecorp-com-au_a3f1b2c4/         structured JSON
```

**Example JSON (fully parsed):**
```json
{
  "customer_name": "ACME Corp",
  "abn": "12345678901",
  "address": "42 Example Street Sydney NSW 2000",
  "order_date": "07 Apr 2026",
  "po_number": "PO-20260407",
  "delivery_date": "24 Apr 2026",
  "company_name": "Buyer Pty Ltd",
  "company_abn": "98765432100",
  "subtotal": "1500.00",
  "tax_amount": "0.00",
  "total_amount": "1500.00",
  "line_items": [
    {
      "product_code": "100793",
      "description": "Burger patty wagyu 180g frozen",
      "qty": "4.000",
      "uom": "piece/unit",
      "unit_price": "71.26",
      "subtotal": null,
      "total": "285.03"
    }
  ],
  "confidence": 0.93,
  "status": "parsed",
  "needs_review": false,
  "processed_at": "2026-04-22T03:00:00Z"
}
```

**Example JSON (sender in address book, no template yet):**
```json
{
  "customer_name": "New Supplier",
  "abn": null,
  "po_number": null,
  "line_items": [],
  "confidence": 0.95,
  "status": "extracted_only",
  "needs_review": true,
  "processed_at": "2026-04-22T03:00:00Z"
}
```
The raw extracted text is in `raw_text/<folder>/`. Add a YAML template and the next run will parse the fields.

---

## Personal email senders (hotmail, gmail, etc.)

Business senders are identified by their email domain (e.g. `acmecorp.com.au`). For personal senders, the domain is shared by millions of people and useless for identification — instead, use the exact email address.

In `config/address_book.json`, use `emails` instead of `domains`:

```json
{
  "name": "Jane Smith",
  "emails": ["jane.smith@hotmail.com"],
  "template": "janesmith"
}
```

The allowlist check works the same way — `jane.smith@hotmail.com` is allowed through even though `hotmail.com` isn't a trusted domain.

**Matching priority:**

| Priority | Strategy | Confidence | Use for |
|---|---|---|---|
| 1 | Exact email address | 98% | Personal senders |
| 2 | Email domain | 95% | Business senders |
| 3 | ABN in document | 90% | Either |
| 4 | Keyword scoring | Variable | Fallback |

See `config/templates/example_personal_contact.yaml` for a full template example.

---

## Management CLI

`manage.py` provides commands for day-to-day operations without editing JSON files or querying SQLite directly.

```bash
python manage.py <command> [options]
```

### Customer onboarding

```bash
# Scan your mailbox and see who's been sending emails with attachments
python manage.py list-senders
python manage.py list-senders --days 90

# Add a sender to address_book.json without opening a text editor
python manage.py add-sender billing@acme.com.au
python manage.py add-sender billing@acme.com.au --name "ACME Corp" --template acme

# Show all auto-generated template suggestions waiting for review
python manage.py list-suggestions

# Promote a suggestion to active status (copies template + updates address book)
python manage.py approve-suggestion billing@acme.com.au
```

### Template testing

```bash
# Test a YAML template against a real PDF
python manage.py test-template acme invoice.pdf

# Show the raw extracted text (useful when writing regex patterns)
python manage.py test-template acme invoice.pdf --show-text

# Check confidence trend for a template over recent runs
python manage.py analyze-template acme
python manage.py analyze-template acme --last 10
```

### Review queue

Documents flagged `needs_review: true` are automatically added to the review queue.

```bash
# List documents awaiting review
python manage.py review-queue

# Mark a document as reviewed (use the ID from review-queue output)
python manage.py resolve-review 3
python manage.py resolve-review 3 --dismiss    # mark as dismissed instead
python manage.py resolve-review 3 --by "Alice"
```

### Health check

```bash
# Check token, database, address book, and last run status in one command
python manage.py health
```

Sample output:
```
  [OK]  Auth token: account=you@outlook.com
  [OK]  Database: 47 total processed, 0 errors, 2 pending review
  [OK]  Address book: 5 contact(s)
  [OK]  Last run: 2026-04-23T08:00:01 UTC — 3 attachment(s), avg confidence=0.91
  [--]  Pending suggestions: 1 (run list-suggestions)

Issues (1):
  - 1 suggested templates need review
```

---

## Suggested templates for unknown senders

When an email comes in from a sender not in `config/address_book.json`, the pipeline automatically generates a draft YAML template in `config/suggested_templates/`. Nothing is parsed for that email, but you get a starting point to work from.

**What gets generated:**

```yaml
_status: SUGGESTED — review patterns, then copy to config/templates/ to activate
_generated_at: '2026-04-22 12:00 UTC'
_address_book_entry:          # <-- copy this into config/address_book.json
  name: New Company
  domains:
    - newcompany.com.au
  abns:
    - '12345678901'
  keywords: [newcompany, order, supply, ...]
  template: supplier_at_newcompany_com_au
_field_examples_found_in_document:   # actual values from the PDF
  invoice_number_example: INV00123
  order_date_example: 15 Apr 2026
  amounts_found: ['1,250.00', '85.00']
customer_name: New Company
required_fields: [invoice_number, order_date]
fields:
  invoice_number:
    - '(?:Invoice|INV|PO)[\s#:.]*(\w{3,20})'
  ...
```

The `_field_examples_found_in_document` section shows **actual values pulled from the PDF** so you can see what the regex needs to match. Because templates now use YAML, regex patterns need only **single backslashes** — no more `\\d`, just `\d`.

**To activate a suggested template:**

**Option A — one command (recommended):**
```bash
python manage.py approve-suggestion supplier@newcompany.com.au
```
This copies the template to `config/templates/` and adds the sender to `address_book.json` in one step.

**Option B — manual:**
1. Open `config/suggested_templates/<sender>.yaml`
2. Copy the `_address_book_entry` block into `config/address_book.json` under `"contacts"`
3. Adjust any regex patterns in `fields` that look wrong (use `--show-text` to see the raw PDF text)
4. Copy the file to `config/templates/`
5. Run the pipeline again — the sender will now be recognised and their attachments parsed

**Option C — fully automatic:**

Set `AUTO_APPROVE_CONFIDENCE=0.85` in `.env`. When the pipeline generates a suggestion and the sniffed field examples indicate ≥ 85% confidence, the template is promoted automatically without any manual step. A webhook alert is still sent so you know it happened.

If the same unknown sender emails again before you activate their template, the suggestion file is updated with any new field examples found — it won't overwrite your edits.

> **Suggested templates are gitignored** — they won't be committed to the repo since they may contain customer-specific information.

---

## Adding a new customer

### Step 1 — Discover who's been sending you emails

Run this to scan your mailbox and generate ready-to-paste address book entries:

```bash
python manage.py list-senders          # last 30 days (default)
python manage.py list-senders --days 90
```

Output:
```
Scanned 47 email(s) — found 3 unique sender(s).

Already in address_book.json (1):
  ✓  ACME Corp <billing@acmecorp.com.au>

New senders not yet in address_book.json (2):
  +  New Supplier <orders@newsupplier.com.au>
  +  Jane Smith <jane@gmail.com>

────────────────────────────────────────────────────────────
Add these to config/address_book.json under "contacts":
────────────────────────────────────────────────────────────
{
  "name": "New Supplier",
  "domains": ["newsupplier.com.au"],
  "template": ""
},
{
  "name": "Jane Smith",
  "emails": ["jane@gmail.com"],
  "template": ""
},
```

Personal addresses (gmail, hotmail, etc.) automatically use `"emails"` instead of `"domains"`.

### Step 2 — Add them to the address book

**Quickest way — no text editor needed:**
```bash
python manage.py add-sender orders@newsupplier.com.au --name "New Supplier" --template newsupplier
python manage.py add-sender jane@gmail.com --name "Jane Smith"
```

**Or paste directly** into `config/address_book.json` under `"contacts"`. You can add `"abns"` and `"keywords"` to improve matching accuracy:

```json
{
  "name": "New Supplier",
  "domains": ["newsupplier.com.au"],
  "abns": ["12345678901"],
  "keywords": ["supply", "purchase order"],
  "template": "newsupplier"
}
```

The pipeline will now download and extract text from their emails immediately — even before you write a template.

### Step 3 — Create a parsing template

**Quickest way — use the `/generate-template` Claude Code skill:**

Open Claude Code in this project directory and run:
```
/generate-template newsupplier.com.au          # org domain
/generate-template jane@gmail.com              # personal email
/generate-template newsupplier                 # contact already in address book
```

Claude will read the raw text files for that sender, analyse the document structure, write a
YAML template with correct regex patterns, test it against your actual PDFs, and iterate until
all required fields extract successfully. The skill handles up to 5 different document layouts
from the same sender (each gets its own `line_items_patterns` entry).

**Or write manually:** Copy `config/templates/example_new_customer.yaml`, rename to match the
`"template"` value above (e.g. `newsupplier.yaml`), and fill in the `fields` section.

```bash
# Check which fields match against a real PDF from that sender
python manage.py test-template newsupplier invoice.pdf

# Add --show-text to see the raw extracted text (useful for writing patterns)
python manage.py test-template newsupplier invoice.pdf --show-text
```

Regex patterns use single backslashes in YAML (e.g. `\d+`, not `\\d+`).

#### Excel (.xlsx) templates

For Excel attachments, use `fields_xlsx` and `line_items_xlsx` instead of (or alongside) regex `fields`. The parser searches for a cell matching the label string and returns the adjacent cell value.

```yaml
required_fields: [po_number, delivery_date]

# Cell-label lookup: searches all cells for the label, returns adjacent cell value
fields_xlsx:
  po_number:     "PO Number"
  delivery_date: "Delivery Date"
  company_name:  "Company Name"
  total_amount:  "Total (inc GST)"

# Fallback regex on flat text for any fields not found via fields_xlsx
fields:
  po_number:
    - 'PO[\s#:]+([A-Z0-9\-]+)'

# Structured line item extraction using column headers
line_items_xlsx:
  sheet: 0               # sheet index (0-based) or name string
  header_row: 1          # 1-indexed row containing column headers
  columns:
    product_code: "Item Code"
    description:  "Description"
    qty:          "Quantity"
    uom:          "UOM"
    unit_price:   "Unit Price"
    subtotal:     "Sub Total"
    total:        "Total"
  skip_if_empty: "product_code"   # skip rows where this column is blank
```

Test Excel templates the same way as PDF:
```bash
python manage.py test-template mysupplier order.xlsx
python manage.py test-template mysupplier order.xlsx --show-text
```

No code changes needed — new templates are picked up automatically on the next run.

---

## Checking results

```bash
# Overall health (token, DB, address book, last run)
python manage.py health

# Documents flagged for manual review
python manage.py review-queue

# Mark a review item as resolved (ID from review-queue output)
python manage.py resolve-review 3

# Confidence trend for a template (detect vendor format changes)
python manage.py analyze-template acme
```

**Run metrics** are written to `logs/metrics.json` after every run and optionally POSTed to `WEBHOOK_URL`. If set, you'll get a Slack/Teams notification automatically.

**Direct SQLite queries** (if you need more detail):

```bash
# Blocked / failed attachments
sqlite3 database/pipeline.db "SELECT attachment_filename, error FROM processed_documents WHERE error IS NOT NULL;"

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
- Commit your credentials (`.env` is gitignored)

> **Webhooks:** if `WEBHOOK_URL` is set in `.env`, the pipeline will POST a run summary to that URL after each run. This is opt-in and off by default. The payload contains counts and confidence averages — it does not include email content, attachment text, or personal data.

---

## Folder structure

```
ms_outlook/
├── main.py                        ← run this  (--days, --schedule, --check-auth, --dry-run)
├── manage.py                      ← management CLI  (see Management CLI section)
├── requirements.txt
├── Dockerfile                     ← builds isolated container with ClamAV + Tesseract
├── docker-compose.yml             ← pipeline (one-shot) + scheduler (daemon) services
├── .env                           ← your credentials (gitignored)
├── .env.example                   ← safe template to copy
├── config/
│   ├── settings.py                ← loads .env, defines all config
│   ├── address_book.json          ← sender allowlist + customer→template links (gitignored)
│   ├── address_book.example.json  ← copy this to address_book.json to get started
│   ├── token_cache.bin            ← created on first login (gitignored)
│   ├── templates/                 ← active customer YAML templates (parsing rules only)
│   │   ├── example_new_customer.yaml
│   │   └── example_personal_contact.yaml
│   └── suggested_templates/       ← auto-generated drafts (gitignored)
│       └── supplier_at_newcompany_com_au.yaml
├── .claude/
│   └── commands/
│       └── generate-template.md   ← /generate-template skill (Claude Code)
├── auth/
│   └── graph_client.py            ← Microsoft login + Graph API (with retry)
├── pipeline/
│   ├── email_reader.py            ← fetch emails filtered by date range
│   ├── attachment_downloader.py   ← download to YYYY-MM-DD_domain_hash/ folders
│   ├── security.py                ← allowlist, magic bytes, PDF scan, ClamAV
│   ├── text_extractor.py          ← extract text: native PDF, OCR fallback, Excel (openpyxl)
│   ├── customer_classifier.py     ← identify customer from address book
│   ├── template_parser.py         ← parse() for PDF/image regex; parse_xlsx() for Excel (fields_xlsx + line_items_xlsx)
│   ├── json_output.py             ← validate + save JSON (LineItem: product_code, uom, subtotal)
│   ├── email_mover.py             ← move email after processing
│   ├── template_suggester.py      ← auto-generate drafts with table format detection
│   └── metrics.py                 ← run metrics (logs/metrics.json + webhook POST)
├── database/
│   └── db.py                      ← SQLite: processed_documents, review_queue, template_stats
├── utils/
│   └── logger.py
├── attachments/                   ← original files (gitignored)
├── raw_text/                      ← extracted text (gitignored)
├── parsed/                        ← JSON output (gitignored)
├── logs/
│   ├── pipeline.log               ← rotating log (gitignored)
│   └── metrics.json               ← last run summary (gitignored)
└── database/
    └── pipeline.db                ← auto-created on first run (gitignored)
```
