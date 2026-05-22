# v3 — Invoice Processing Pipeline

A local-LLM-powered invoice parser that reads PDF attachments, extracts
customer and line-item data via [Ollama](https://ollama.com/), stores
learned customer templates in SQLite, and routes low-confidence results
to an interactive operator review CLI.

---

## Folder structure

```
v3/
├── core/
│   └── invoices/
│       ├── extractor.py      # data contracts, SQLite store, PDF rasterizer, validator
│       ├── llm_extractor.py  # Ollama client, extraction prompts, payload mapping
│       └── manual_review.py  # operator CLI (display, correct, accept/reject)
├── tests/
│   └── invoices/
│       ├── view_templates.bat / .py   # inspect customer_templates DB table
│       ├── view_pending.bat  / .py    # list pending staged invoices
│       ├── view_approved.bat / .py    # list approved staged invoices
│       └── run_on_attachment.bat      # run the full pipeline on a PDF
├── invoice_parser.py         # main entry point (run this)
├── vendor_config.json        # private vendor exclusions (gitignored — copy from .example)
├── vendor_config.json.example
└── README.md
```

Runtime output (all gitignored):
```
v3/
├── data/
│   ├── attachments/          # drop new invoice PDFs here
│   ├── processed/            # PDFs moved here automatically after a successful parse
│   └── mock_senders.json     # maps filenames to simulated sender emails
├── invoice_memory.db         # SQLite customer template store
└── invoice_staging/
    ├── pending/              # invoices awaiting operator review
    ├── approved/             # operator-confirmed invoices
    └── rejected/             # operator-rejected invoices
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

1. Copy the vendor exclusion config and fill in your supplier details:
   ```bat
   copy v3\vendor_config.json.example v3\vendor_config.json
   ```
   Edit `vendor_config.json` to list your own company's names, ABNs, emails,
   and phone numbers so the LLM knows to treat them as the vendor, not the customer.

2. Start Ollama:
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
    Ollama qwen2.5:3b extracts customer + line items from text pages
    Ollama qwen2-vl:2b handles scanned/image pages (if available)
    Vendor exclusion rules prevent own-company details being mis-classified
  │
  ▼
[3] Validator & Stager (extractor.py → validate_and_stage)
    Cross-checks against saved customer templates (SQLite)
    Scores parse confidence: high / medium / low
    Validates line-item math
    Writes JSON to invoice_staging/pending/ or approved/
  │
  ▼
[4] Operator Review CLI (manual_review.py)  ← skipped with --auto
    Shows formatted ASCII summary
    Prompts for field corrections
    Accept → moves to approved/ + saves template
    Reject → moves to rejected/
```
