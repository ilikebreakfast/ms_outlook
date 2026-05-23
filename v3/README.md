# v3 — Invoice / Purchase Order Processing Pipeline

A local-LLM-powered document parser designed for a **supplier (vendor) receiving
Purchase Orders from customers**. It extracts customer and line-item data via
[Ollama](https://ollama.com/), stores learned templates and product codes in SQLite,
routes low-confidence results to an interactive operator review CLI, and maintains a
self-improving knowledge base that gets smarter with every confirmed document.

---

## Business Context

This system is used **as the supplier**:

| Role | Who | Details on document |
|---|---|---|
| **Us** (Supplier / Vendor) | Your company | `Vendor:`, `Supplier:`, `To:` fields → **excluded** from extraction |
| **Them** (Customer / Buyer) | Companies that order from you | `From:`, `Buyer:`, `Ordered By:` fields → **extracted** |

Documents processed are typically **Purchase Orders sent TO us** by customers, though
it also handles picking slips, delivery dockets, and any other order-related PDFs.

---

## Folder Structure

```
v3/
├── core/
│   └── invoices/
│       ├── extractor.py        # data contracts, SQLite store, PDF rasterizer,
│       │                       # math inference, validator/stager
│       ├── llm_extractor.py    # Ollama client, dynamic context injection,
│       │                       # extraction prompts, payload mapping
│       ├── manual_review.py    # operator CLI (display, correct, accept/reject)
│       └── knowledge_base.py   # ERP CSV loader, per-customer prompts,
│                               # PROMPT.md generator
├── knowledge/
│   ├── PROMPT_BASE.md                      # static extraction guidelines (edit this)
│   ├── PROMPT.md                           # auto-generated knowledge summary (gitignored)
│   ├── product_mapping.json.example        # CSV column mapping template
│   ├── product_mapping.json                # your column mapping (gitignored — copy from .example)
│   ├── products/                           # ERP CSV exports (gitignored — drop files here)
│   └── customers/
│       ├── example_customer.md.example     # per-customer prompt template
│       └── <slug>.md                       # per-customer rules (gitignored — see below)
├── tests/
│   └── invoices/
│       ├── view_templates.bat              # Show saved customer templates (tests/data/ DB)
│       ├── view_pending.bat                # List pending staged test invoices
│       ├── view_approved.bat               # List approved staged test invoices
│       ├── run_on_attachment.bat           # Interactive PDF pipeline launcher (sandbox mode)
│       └── scripts/
│           ├── view_templates.py
│           ├── view_pending.py
│           ├── view_approved.py
│           └── run_on_attachment.py
├── invoice_parser.py           # main entry point
├── vendor_config.json          # OUR company exclusions (gitignored — copy from .example)
├── vendor_config.json.example
└── README.md
```

Runtime output (all gitignored):
```
v3/
├── invoice_memory.db           # SQLite: customer_templates + item_codes
└── invoice_staging/
    ├── pending/                # awaiting operator review
    ├── approved/               # operator-confirmed
    └── rejected/               # operator-rejected
```

---

## Prerequisites

| Dependency | Purpose |
|---|---|
| Python 3.11+ | Runtime |
| [Ollama](https://ollama.com/) | Local LLM server |
| `gemma3:4b` | Default model — text extraction and vision (see model guide below) |
| `pdfplumber` | PDF text extraction and page classification |
| `PyMuPDF` (`fitz`) | PDF rasterization (no Poppler required) |
| `PaddleOCR` | OCR fallback for scanned/image pages |
| `Pillow`, `numpy` | Image processing |
| `requests` | Ollama API client |

Install Python deps (using the shared v2 venv):
```bat
v2\venv\Scripts\pip install pdfplumber pymupdf paddleocr pillow numpy requests
```

Pull the default model:
```bash
ollama pull gemma3:4b
```

---

## Model Configuration

Models and vision mode are configured in `v3/core/config.py` and can be overridden via environment variables.

| Setting | Default | Env var override |
|---|---|---|
| `TEXT_MODEL` | `gemma3:4b` | `TEXT_MODEL=<name>` |
| `VISION_MODEL` | `gemma3:4b` | `VISION_MODEL=<name>` |
| `VISION_ENABLED` | `False` | `VISION_ENABLED=1` |
| `OLLAMA_BASE` | `http://localhost:11434` | `OLLAMA_BASE=<url>` |

**Vision is disabled by default** — local vision models require GPU acceleration to run at useful speeds. Enable it only when running on a machine with a supported discrete GPU.

### Hardware guide

| Machine | Recommended models | Vision |
|---|---|---|
| Laptop — Intel Arc 130V (iGPU, 32GB shared RAM) | `gemma3:4b` text | Disabled (CPU only — too slow) |
| Desktop — AMD RX 7800 XT (16GB VRAM) | `gemma3:12b` text + `gemma3:12b` vision | Enable with `VISION_ENABLED=1` |
| Any machine with NVIDIA 12GB+ VRAM | `gemma3:12b` or `qwen2.5:14b` | Enable with `VISION_ENABLED=1` |

On the desktop (7800 XT), ROCm-enabled Ollama can run `gemma3:12b` comfortably within 16GB VRAM,
giving significantly better extraction quality. Pull and configure:
```bash
ollama pull gemma3:12b
# Then set env vars or edit config.py:
TEXT_MODEL=gemma3:12b VISION_MODEL=gemma3:12b VISION_ENABLED=1 python v3/invoice_parser.py ...
```

---

## Setup

### 1. Our company exclusions
Copy and fill in your own company's details so the LLM never mistakes them for a customer:
```bat
copy v3\vendor_config.json.example v3\vendor_config.json
```
Edit `vendor_config.json` with your company name(s), ABN, email, and phone.

### 2. Static extraction guidelines (optional but recommended)
Edit `v3/knowledge/PROMPT_BASE.md` to add any rules that always apply
(e.g. "our supplier codes are always 5-digit numeric"). This file is tracked in git.
It is merged into the auto-generated `PROMPT.md` after each confirmed document.

### 3. ERP product codes (optional)
If you have a product list exported from your ERP:
```bat
copy v3\knowledge\product_mapping.json.example v3\knowledge\product_mapping.json
```
Edit `product_mapping.json` to match your CSV's column headers, then drop your
`.csv` files into `v3/knowledge/products/`. The pipeline loads them on each run
(idempotent upsert). This enables SKU verification — extracted codes are checked
against the ERP list and flagged if they don't match.

### 4. Per-customer prompts (optional, for tricky customers)
Create a `.md` file in `v3/knowledge/customers/` named after the customer's
lookup key (email or name), lowercased with spaces/symbols replaced by underscores:
```
Bavarian Bier Cafe  →  v3/knowledge/customers/bavarian_bier_cafe.md
info@bier.com.au    →  v3/knowledge/customers/info_bier_com_au.md
```
See `v3/knowledge/customers/example_customer.md.example` for the template.
Only create one when the general rules produce consistent errors for that customer.
These files are gitignored (may contain customer-specific details).

### 5. Start Ollama
```bash
ollama serve
```

---

## How to Run

All commands from the **repository root**.

### Parse a single document
```bat
"v2\venv\Scripts\python.exe" v3\invoice_parser.py path\to\order.pdf
```

### Parse with email body for context
```bat
"v2\venv\Scripts\python.exe" v3\invoice_parser.py path\to\order.pdf --email path\to\email_body.txt
```

### Batch / automated mode (skip operator prompts)
```bat
"v2\venv\Scripts\python.exe" v3\invoice_parser.py path\to\order.pdf --auto
```

### Review all pending documents interactively
```bat
"v2\venv\Scripts\python.exe" v3\invoice_parser.py --review-pending
```

---

## Test Scripts

| Script | What it does |
|---|---|
| `tests\invoices\view_templates.bat` | Show all saved customer templates in the DB |
| `tests\invoices\view_pending.bat` | List invoices waiting for review |
| `tests\invoices\view_approved.bat` | List approved invoices |
| `tests\invoices\run_on_attachment.bat` | Interactive menu: select PDF → run pipeline → auto-move on success |

`run_on_attachment.bat` menu:
```
[N]  New attachments       — v3/data/attachments/ (newest first, with sender domain)
[P]  Processed attachments — browse already-parsed PDFs
[R]  Review all pending    — operator review CLI for all staged pending documents
[Q]  Quit
```

---

## Pipeline Stages

```
PDF file
  │
  ▼
[1] Rasterizer  (extractor.py)
    pdfplumber classifies each page as "text" or "image"
    PyMuPDF rasterizes to JPEG at 150/300 DPI
    PaddleOCR pre-scans image pages
  │
  ▼
[2] Customer Extraction  (llm_extractor.py → extract_customer)
    Injects dynamic context: known customers from DB + our company exclusions
    LLM identifies the customer (buyer) from page 1
    Looks for: From, Buyer, Ordered By, Company, Bill To, Ship To
    Excludes our own company's name/ABN/email/phone
  │
  ▼
[3] Per-Customer Prompt Load  (knowledge_base.py → load_customer_prompt)
    Slugifies the identified customer name/email
    Loads v3/knowledge/customers/<slug>.md if it exists
    Customer-specific rules override general rules for step [4]
  │
  ▼
[4] Line-Item Extraction  (llm_extractor.py → extract_line_items)
    Injects dynamic context: known ERP product codes + confirmed item codes
    Injects per-customer rules from step [3] if available
    Separates our supplier SKU (sku) from customer reference codes (customer_ref)
    On POs: "Supplier Code"/"Your Code" → sku; "Our Code"/"Buyer Code" → customer_ref
  │
  ▼
[5] Math Inference  (extractor.py → infer_line_item_math)
    Derives any missing value from the other two (qty, unit_price, line_total)
    Flags math mismatches (qty × price ≠ total by > $0.02) as warnings
    Adds inferred_* fields — never overwrites extracted values
  │
  ▼
[6] Validator & Stager  (extractor.py → validate_and_stage)
    Enriches from saved customer templates (SQLite)
    SKU cross-check: if ERP codes are loaded, flags SKUs not in the ERP list
    Scores parse confidence: high / medium / low
    Writes JSON to invoice_staging/pending/ or approved/
  │
  ▼
[7] Operator Review CLI  (manual_review.py)    ← skipped with --auto
    Formatted ASCII panel with confidence, warnings, and inferred values
    SKU column shows ✓ (ERP-verified) or ? (not in ERP list)
    ~ prefix = inferred value,  ! suffix = math mismatch
    Customer ref shown indented below each line item if present
    Accept  → approved/ + save customer template + save item codes
             + regenerate v3/knowledge/PROMPT.md
    Reject  → rejected/
```

---

## Data Contract: LineItem Fields

| Field | Type | Description |
|---|---|---|
| `sku` | str \| null | **Our** supplier product code (what we want) |
| `customer_ref` | str \| null | Customer's own reference code for this product |
| `description` | str | Product description |
| `quantity` | float \| null | Order quantity |
| `uom` | str \| null | Unit of measure (Kg, EA, ctn, …) |
| `unit_price` | float \| null | Price per unit (extracted) |
| `line_total` | float \| null | Line total (extracted) |
| `confidence` | str | `high` / `medium` / `low` |
| `sku_verified` | bool | True if `sku` matched a known ERP product code |
| `inferred_unit_price` | float \| null | Derived: line_total ÷ quantity |
| `inferred_quantity` | float \| null | Derived: line_total ÷ unit_price |
| `inferred_line_total` | float \| null | Derived: quantity × unit_price |
| `math_ok` | bool | False if all three fields present but don't agree |

---

## Self-Improving Knowledge Base

Every confirmed document teaches the system:

| What is learned | Where stored | Used for |
|---|---|---|
| Customer name, email, ABN, address | `customer_templates` DB | Template enrichment + LLM customer context |
| Line item SKUs, descriptions, prices | `item_codes` DB (`source='llm'`) | LLM item-matching hints |
| ERP product codes (CSV import) | `item_codes` DB (`source='erp'`) | LLM hints + SKU verification |

After each confirmed document `v3/knowledge/PROMPT.md` is regenerated. It combines:
- **Known customers** table (from `customer_templates`, confirmed ≥ 2 times)
- **ERP product codes** table (from CSV imports)
- **Learned item codes** table (LLM-extracted, confirmed ≥ 2 times)
- **Static guidelines** from `PROMPT_BASE.md`

Use `PROMPT.md` as context when prompting an external AI to review or improve extraction quality.

---

## Per-Customer Prompts

**When to create one:** only when a customer's documents consistently produce extraction
errors that the general rules don't fix. Good candidates:
- Unusual column header names for the supplier code column
- A customer who always puts both their own and our product codes on the same row
- Documents with recurring boilerplate pages that the boilerplate filter misses
- Non-standard field labels for the buyer name/address

**When not to create one:** if the general rules work fine, adding a file adds
maintenance overhead with no benefit.

**File location:** `v3/knowledge/customers/<slug>.md` (gitignored)
**Slug:** customer's lookup key (email or name), lowercased, non-alphanumeric → `_`

```
Bavarian Bier Cafe        →  bavarian_bier_cafe.md
info@bavarianbier.com.au  →  info_bavarianbier_com_au.md
```

The per-customer prompt is loaded after customer identification on page 1 and
injected into the line-item extraction prompt as **override rules** (they take
precedence over the general instructions if there is a conflict).

See `v3/knowledge/customers/example_customer.md.example` for the full template.

---

## Math Inference

The validator derives any missing numeric field from the other two:

| Known | Missing | Action |
|---|---|---|
| qty + unit_price | line_total | `inferred_line_total = qty × unit_price` |
| qty + line_total | unit_price | `inferred_unit_price = line_total ÷ qty` |
| unit_price + line_total | qty | `inferred_quantity = line_total ÷ unit_price` |
| all three | — | checks `qty × price ≈ total`; flags mismatch if off > $0.02 |

Inferred values appear in the review CLI with `~` prefix. Mismatched rows marked with `!`.
More than one inferred value per document lowers parse confidence to ≤ medium.

---

## SKU / Product Code Rules

| Label on document | Document issuer | Maps to |
|---|---|---|
| `Supplier Code`, `Vendor Code`, `Your Code`, `Your Item` | Customer PO | `sku` (our code) |
| `Product Code`, `Code`, `Item No`, `Stock Code` | Our own documents | `sku` (our code) |
| `Our Code`, `Our Item`, `Buyer Code`, `Cust Code` | Customer PO | `customer_ref` (their code) |
| `Customer Ref`, `Cust Ref`, `Your Code` | Our own documents | `customer_ref` (their code) |

If ERP codes are loaded, every extracted `sku` is cross-checked. Codes not found in
the ERP list are flagged with `?` in the review CLI and added to `review_fields`.
