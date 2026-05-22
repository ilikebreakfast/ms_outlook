# v3 — Invoice Processing Pipeline

A local-LLM-powered invoice parser that reads PDF attachments, extracts
customer and line-item data via [Ollama](https://ollama.com/), stores
learned customer templates and item codes in SQLite, and routes low-confidence
results to an interactive operator review CLI.

---

## Folder structure

```
v3/
├── core/
│   └── invoices/
│       ├── extractor.py       # data contracts, SQLite store, PDF rasterizer,
│       │                      # math inference, validator/stager
│       ├── llm_extractor.py   # Ollama client, dynamic context injection,
│       │                      # extraction prompts, payload mapping
│       ├── manual_review.py   # operator CLI (display, correct, accept/reject)
│       └── knowledge_base.py  # ERP CSV loader, PROMPT.md generator
├── knowledge/
│   ├── PROMPT_BASE.md         # static extraction guidelines (edit this)
│   ├── PROMPT.md              # auto-generated knowledge summary (gitignored)
│   ├── product_mapping.json.example
│   ├── product_mapping.json   # your column mapping (gitignored — copy from .example)
│   └── products/              # drop ERP CSV exports here (gitignored)
├── tests/
│   └── invoices/
│       ├── view_templates.bat / .py   # inspect customer_templates DB table
│       ├── view_pending.bat  / .py    # list pending staged invoices
│       ├── view_approved.bat / .py    # list approved staged invoices
│       └── run_on_attachment.bat      # run the full pipeline on a PDF
├── invoice_parser.py          # main entry point (run this)
├── vendor_config.json         # private vendor exclusions (gitignored — copy from .example)
├── vendor_config.json.example
└── README.md
```

Runtime output (all gitignored):
```
v3/
├── invoice_memory.db          # SQLite store: customer_templates + item_codes
└── invoice_staging/
    ├── pending/               # invoices awaiting operator review
    ├── approved/              # operator-confirmed invoices
    └── rejected/              # operator-rejected invoices
```

---

## Prerequisites

| Dependency | Purpose |
|---|---|
| Python 3.11+ | Runtime |
| [Ollama](https://ollama.com/) | Local LLM server |
| `qwen2.5:3b` | Text invoice extraction model |
| `qwen2-vl:2b` | Vision model for scanned pages (optional) |
| `pdfplumber` | PDF text extraction and page classification |
| `PyMuPDF` (`fitz`) | PDF rasterization (no Poppler required) |
| `PaddleOCR` | OCR fallback for scanned/image pages |
| `Pillow`, `numpy` | Image processing for rasterized pages |
| `requests` | Ollama API client |

Install Python deps (using the shared v2 venv):
```bat
v2\venv\Scripts\pip install pdfplumber pymupdf paddleocr pillow numpy requests
```

Pull Ollama models:
```bash
ollama pull qwen2.5:3b
ollama pull qwen2-vl:2b   # optional — only needed for scanned PDFs
```

---

## Setup

### 1. Vendor exclusion config
Copy the vendor config and fill in your own company's details so the LLM
never mistakes them for a customer:
```bat
copy v3\vendor_config.json.example v3\vendor_config.json
```

### 2. (Optional) ERP product codes
Copy the product mapping template and fill in your CSV column names:
```bat
copy v3\knowledge\product_mapping.json.example v3\knowledge\product_mapping.json
```
Edit `product_mapping.json` to match your ERP export headers, then drop your
`.csv` files into `v3/knowledge/products/`. The pipeline loads them automatically
on each run (idempotent upsert).

### 3. (Optional) Static extraction guidelines
Edit `v3/knowledge/PROMPT_BASE.md` to add any persistent rules you want the
LLM to follow. This file is tracked in git; it is merged into the
auto-generated `PROMPT.md` after each confirmed invoice.

### 4. Start Ollama
```bash
ollama serve
```

---

## How to run

All commands are run from the **repository root**.

### Parse a single invoice
```bat
"v2\venv\Scripts\python.exe" v3\invoice_parser.py path\to\invoice.pdf
```

### Parse with email body for context
```bat
"v2\venv\Scripts\python.exe" v3\invoice_parser.py path\to\invoice.pdf --email path\to\email_body.txt
```

### Batch / automated mode (skip operator prompts)
```bat
"v2\venv\Scripts\python.exe" v3\invoice_parser.py path\to\invoice.pdf --auto
```

### Review all pending invoices interactively
```bat
"v2\venv\Scripts\python.exe" v3\invoice_parser.py --review-pending
```

---

## Test scripts

Open any bat file directly from Explorer or run from the command line.

| Script | What it does |
|---|---|
| `tests\invoices\view_templates.bat` | Show all saved customer templates in the DB |
| `tests\invoices\view_pending.bat` | List all invoices waiting for review |
| `tests\invoices\view_approved.bat` | List all operator-approved invoices |
| `tests\invoices\run_on_attachment.bat` | Interactive menu: browse new/processed attachments by date + domain, run pipeline, auto-move to processed/ on success |

`run_on_attachment.bat` menu options:

```
[N]  New attachments        — lists v3/data/attachments/ (newest first, with sender domain)
[P]  Processed attachments  — browse already-parsed PDFs
[R]  Review all pending     — launches the operator review CLI for all staged pending invoices
[Q]  Quit
```

After selecting a file from **N**, the pipeline runs interactively. On success the PDF is
moved automatically from `data/attachments/` → `data/processed/`.

---

## Pipeline stages

```
PDF file
  │
  ▼
[1] Rasterizer (extractor.py)
    pdfplumber classifies each page as "text" or "image"
    PyMuPDF rasterizes to JPEG at 150/300 DPI
    PaddleOCR pre-scans image pages
  │
  ▼
[2] LLM Extractor (llm_extractor.py)
    Loads known customers + item codes from DB (dynamic context injection)
    Ollama qwen2.5:3b extracts customer + line items from text pages
    Ollama qwen2-vl:2b handles scanned/image pages (if available)
    Vendor exclusion rules prevent own-company details being mis-classified
  │
  ▼
[3] Math Inference (extractor.py → infer_line_item_math)
    Derives missing qty / unit_price / line_total from the other two
    Flags math mismatches as warnings (does NOT overwrite extracted values)
    Sets inferred_* fields on each LineItem for display
  │
  ▼
[4] Validator & Stager (extractor.py → validate_and_stage)
    Cross-checks against saved customer templates (SQLite)
    Scores parse confidence: high / medium / low
    Writes JSON to invoice_staging/pending/ or approved/
  │
  ▼
[5] Operator Review CLI (manual_review.py)  ← skipped with --auto
    Shows formatted ASCII summary
    ~ prefix = inferred value   ! suffix = math mismatch
    Prompts for field corrections
    Accept → moves to approved/ + saves customer template + item codes
             + regenerates v3/knowledge/PROMPT.md
    Reject → moves to rejected/
```

---

## Self-improving knowledge base

Every confirmed invoice teaches the system:

| What is learned | Where stored | Used for |
|---|---|---|
| Customer name, email, ABN, address | `customer_templates` DB table | Template enrichment + LLM context |
| Line item SKUs, descriptions, prices | `item_codes` DB table | LLM item-matching hints |
| ERP product codes (from CSV) | `item_codes` (source=erp) | LLM item-matching hints |

After each confirmed invoice `v3/knowledge/PROMPT.md` is regenerated with the
latest known customers, item codes, and the static guidelines from `PROMPT_BASE.md`.
Use this file as context when prompting an external AI to review or improve extraction quality.

---

## Line-item math inference

The validator computes missing numeric fields without touching extracted values:

| Known | Missing | Action |
|---|---|---|
| qty + unit_price | line_total | `inferred_line_total = qty × unit_price` |
| qty + line_total | unit_price | `inferred_unit_price = line_total ÷ qty` |
| unit_price + line_total | qty | `inferred_quantity = line_total ÷ unit_price` |
| all three present | — | checks `qty × price ≈ total`; flags mismatch if off by > $0.02 |

Inferred values appear in the review CLI with a `~` prefix. Mismatched rows are marked with `!`.
