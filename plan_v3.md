# MS Outlook Invoice Processing Pipeline v3: Step-by-Step Robust Roadmap

Welcome to **Version 3 (v3)** of the Invoice Processing Pipeline! This version adopts a progressive, "one-piece-at-a-time" approach to building a robust, maintainable, and highly accurate system.

---

## What Was Built — Phase 1, 2 & 2 Continuation

> [!NOTE]
> The sections below document what was **actually implemented** during Phase 1, Phase 2, and the Phase 2 Continuation. The original roadmap planned for a pure `pdfplumber` coordinate-based extractor; the final architecture instead uses a **local LLM (Ollama)** as the primary extraction engine, with `pdfplumber` and `PyMuPDF` handling document ingestion and page classification, and `PaddleOCR` as an OCR fallback layer.

### Architecture Built

```
v3/
├── core/invoices/
│   ├── extractor.py       # data contracts + SQLite store (customer_templates + item_codes)
│   │                      # + PDF rasterizer + math inference + validator/stager
│   ├── llm_extractor.py   # Ollama LLM client + dynamic context injection
│   │                      # + customer/line-item prompts + per-customer prompt loading
│   ├── manual_review.py   # operator review CLI (display, correct, accept/reject)
│   └── knowledge_base.py  # ERP CSV loader + per-customer prompt loader + PROMPT.md generator
├── invoice_parser.py      # pipeline entry point (argparse + stage orchestration)
├── tests/invoices/        # bat files + Python view scripts for DB and staging
├── vendor_config.json     # private vendor exclusion config (gitignored)
└── knowledge/
    ├── PROMPT_BASE.md                     # static extraction guidelines (operator-edited)
    ├── PROMPT.md                          # auto-generated knowledge summary (gitignored)
    ├── product_mapping.json.example       # CSV column mapping template
    ├── product_mapping.json               # your ERP column mapping (gitignored)
    ├── products/                          # ERP CSV exports (gitignored — drop files here)
    └── customers/
        ├── example_customer.md.example   # per-customer prompt template
        └── <slug>.md                     # per-customer rules (gitignored)
```

### Local LLM Integration (Ollama)

- **Text model:** `qwen2.5:3b` — used for native-text PDF pages
- **Vision model:** `qwen2.5vl` — used for scanned/image pages when available
- Dynamic model fallback at startup: queries `http://localhost:11434/api/tags` and selects the best matching available model
- Deterministic extraction (`temperature: 0.0`) with structured JSON output schema
- Vendor exclusion config (`vendor_config.json`) injected into prompts to prevent own-company details being classified as the customer
- **Dynamic context injection:** known customers and item codes queried from SQLite at extraction time and appended to prompts — no hardcoded customer or product names in code

### PDF Processing Stack

| Component | Role |
|---|---|
| `pdfplumber` | Text extraction and page classification (>80 non-whitespace chars = text page) |
| `PyMuPDF` (`fitz`) | Page rasterization to JPEG at 150 DPI (text) / 300 DPI (scanned) — no Poppler binary needed |
| `PaddleOCR` | OCR pre-scan for image/scanned pages; output passed to vision LLM as a hint |

### Template Memory Store (SQLite)

- `invoice_memory.db` → `customer_templates` table + `item_codes` table
- `customer_templates`: keyed by customer email (preferred) or name; `confirmed_count >= 2` triggers trusted template overrides and fuzzy diff comparison (SequenceMatcher) for changed fields
- `item_codes`: stores SKU, description, unit price, UOM, source (`'llm'` or `'erp'`), confirmed_count; populated from operator-confirmed invoices and ERP CSV imports

### Staging & Review System

- **`invoice_staging/pending/`** — invoices with warnings, low confidence, or missing fields
- **`invoice_staging/approved/`** — operator-confirmed or high-confidence auto-approved invoices
- **`invoice_staging/rejected/`** — operator-rejected invoices
- Each staging file is a full `InvoicePayload` JSON (source file, customer, line items, totals, confidence, warnings, review flags)
- Math inference: derives missing `qty` / `unit_price` / `line_total` from the other two without overwriting extracted values; flags mismatches > $0.02
- Human-in-the-loop CLI: field-by-field correction prompts → accept / re-edit / reject; inferred values shown with `~` prefix, math mismatches with `!`

### Data Contracts

All data flows through typed Python dataclasses (`FieldValue`, `CustomerInfo`, `LineItem`, `InvoiceTotals`, `InvoicePayload`, `PageData`, `RasterisedPDF`) defined in `core/invoices/extractor.py` and shared across all modules.

`LineItem` fields added in Phase 2 Continuation:

| Field | Description |
|---|---|
| `customer_ref` | Customer's own product reference code (separate from our supplier `sku`) |
| `sku_verified` | `True` when `sku` matched a known ERP product code |
| `inferred_unit_price` | Derived from line_total ÷ quantity |
| `inferred_quantity` | Derived from line_total ÷ unit_price |
| `inferred_line_total` | Derived from quantity × unit_price |
| `math_ok` | `False` when all three fields are present but don't agree within $0.02 |

### Business Context Clarification

The system operates as the **supplier receiving Purchase Orders from customers** — not as a buyer processing invoices it receives. Prompts and terminology were updated to reflect this:
- Documents parsed are typically **customer-issued POs sent to us**
- `Vendor:` / `Supplier:` fields on a PO refer to **us** (excluded from extraction)
- `From:` / `Buyer:` / `Ordered By:` fields are the **customer** (extracted)
- SKU label perspective on customer POs: `"Your Code"` / `"Supplier Code"` = our code → `sku`; `"Our Code"` / `"Buyer Code"` = theirs → `customer_ref`

---

## 1. Architectural Strategy: Simple & Progressive

In `v3`, we focus on high reliability, automated verification, and clean architecture. Instead of building all components concurrently, we construct the foundation first, polish it, and then layer on features.

We have brought in the local copy of the attachments from `v2` into `v3/data/attachments/` and set up a mock data layer. Each document is mapped to a randomly generated company email (e.g., `john.smith@<company-name>.com`) inside `v3/data/mock_senders.json` to simulate real-world Graph API integration without the external dependency overhead during early phases.

---

## 2. Advanced PDF Parsing Exploration: `pdfplumber` Capabilities

`pdfplumber` is an exceptionally powerful library for PDF layout extraction. Compared to basic text parsers, it exposes rich structural primitives that we can leverage in **Phase 1** to achieve near-perfect parsing accuracy:

### A. Words with Coordinates (`.extract_words()`)
Every word in the document is extracted as a dictionary detailing its exact coordinates and characters:
```python
{
    "text": "Invoice",
    "x0": 54.0,       # Distance from left edge of page
    "top": 100.0,     # Distance from top edge of page
    "x1": 98.4,       # Distance of right character edge
    "bottom": 112.0,  # Distance of bottom character edge
    "upright": True
}
```
* **Why it's powerful:** We can search for keyword anchors (e.g., "TOTAL DUE", "Invoice Date") and dynamically scan nearby coordinate ranges (e.g., to the right, or directly below) to extract values regardless of minor alignment shifts.

### B. Targeted Region Cropping (`.crop(bbox)`)
We can crop pages to specific bounding boxes `(x0, top, x1, bottom)` to isolate regions of interest (e.g., header blocks, totals blocks, or line-item sections) before extracting text or tables.
* **Why it's powerful:** It prevents characters in nearby columns from bleeding into text flows, ensuring isolated, clean extraction of regional data.

### C. Advanced Table settings (`.extract_tables(table_settings)`)
Many invoices use gridless tables (tables without explicit lines, defined only by text spacing). `pdfplumber` allows deep tuning of table extraction via a `table_settings` dictionary:
```python
settings = {
    "vertical_strategy": "text",      # Use alignment of characters to detect columns
    "horizontal_strategy": "text",    # Use vertical gaps to detect rows
    "snap_tolerance": 3,              # Group objects within 3px together
    "intersection_tolerance": 5,      # Join nearby lines
    "explicit_vertical_lines": [100, 250, 400] # Force column boundaries
}
```
* **Why it's powerful:** We can handle complex, gridless, or poorly structured invoice tables with high reliability by custom-tuning extraction rules per template.

---

## 3. Phased Implementation Roadmap

### Phase 1: High-Accuracy Attachment Parsing ✅ COMPLETE
* **Goal:** Extract clean text and tabular line items from all document formats (.pdf, .csv, .txt) with 100% correctness.
* **Key Tasks:**
  1. ✅ Set up a core Python parser harness (`core/invoices/extractor.py`, `invoice_parser.py`).
  2. ✅ PDF extraction implemented via `pdfplumber` (text classification) + `PyMuPDF` rasterizer + Ollama LLM prompts for structured extraction. Coordinate anchoring/cropping available if needed for future template refinement.
  3. ✅ OCR fallback implemented via `PaddleOCR` for scanned/image pages; output fed to Ollama vision model (`qwen2.5vl`).
  4. ✅ CLI validation tool built (`print_payload_summary`, `review_payload`) with interactive field correction and accept/reject flow.

### Phase 2: Template Rules Engine & Database Ingestion ✅ COMPLETE
* **Goal:** Build the logical engine to learn parser templates, store parsed customer and invoice records, and apply templates to new files automatically.
* **Key Tasks:**
  1. ✅ Template schema defined — customer profile stored as a SQLite row (name, email, phone, ABN, address, confirmed_count) keyed by email/name.
  2. ✅ `customer_templates` SQLite table implemented with upsert logic and confidence-based memory overrides.
  3. ✅ `item_codes` SQLite table added — stores confirmed line items (SKU, description, unit price, UOM, source, confirmed_count). Populated from operator-confirmed invoices and ERP CSV imports.
  4. ✅ Sender matching implemented: email or name is used as the lookup key; templates with `confirmed_count >= 2` are applied automatically to override low-confidence LLM fields.
  5. ✅ Full invoice data persisted as structured JSON staging files in `invoice_staging/` (pending / approved / rejected) — serves as the invoice record store until Phase 5 dashboard requires a relational table.

### Phase 2 Continuation: Self-Improving Knowledge Base ✅ COMPLETE
* **Goal:** Make the system smarter with every confirmed document — dynamically enriching LLM prompts, validating extracted data against known product codes, and generating a portable knowledge summary for external AI agents.
* **What was built:**

  **Line-item math inference**
  - `infer_line_item_math()` derives any missing value (`qty`, `unit_price`, `line_total`) from the other two without overwriting extracted values
  - Mismatches flagged as warnings and shown in review CLI with `!` marker
  - More than one inferred value per document lowers parse confidence to ≤ medium

  **Vendor SKU safety**
  - `LineItem.customer_ref` — separate field for the customer's own reference code so it never contaminates `sku`
  - `LineItem.sku_verified` — set `True` when extracted SKU matches a known ERP product code
  - ERP cross-check in `validate_and_stage()`: if ERP codes are loaded, unrecognised SKUs are flagged as possible customer reference codes
  - LLM extraction instructions explicitly separate vendor codes (`sku`) from customer codes (`customer_ref`) with document-type-aware label lists

  **ERP product CSV loader** (`knowledge_base.py`)
  - Drop `.csv` exports from your ERP into `v3/knowledge/products/`
  - Configure column names once in `product_mapping.json` (copy from `.example`)
  - Loaded on every pipeline run (idempotent upsert into `item_codes` table, `source='erp'`)

  **Dynamic LLM context injection**
  - Removed all hardcoded customer and product names from prompts
  - `_build_customer_context()` — injects confirmed customers (≥2) from DB into customer extraction prompt
  - `_build_item_context()` — injects known ERP + confirmed item codes into line-item extraction prompt
  - Both are queried fresh at extraction time, so the LLM knowledge grows with every confirmed document

  **Per-customer prompt files**
  - `v3/knowledge/customers/<slug>.md` — optional per-customer override rules (gitignored)
  - Loaded after page-1 customer identification, injected into line-item extraction as override rules
  - Slug derived from the customer's lookup key (email or name, lowercased, non-alphanumeric → `_`)
  - Only needed for customers whose document formats consistently confuse the general rules
  - Template provided at `v3/knowledge/customers/example_customer.md.example`

  **PROMPT.md auto-generation**
  - `generate_prompt_md()` regenerates `v3/knowledge/PROMPT.md` after every confirmed invoice
  - Combines: known customers table, ERP product codes, confirmed item codes, static `PROMPT_BASE.md` guidelines
  - `PROMPT_BASE.md` (tracked in git) — operator-edited static rules merged into every generated `PROMPT.md`
  - Use `PROMPT.md` as context when prompting an external AI to review or improve extraction quality

  **Prompt terminology fix**
  - Reframed all LLM prompts for supplier-receives-PO context
  - `"Vendor Name Exclusions"` → `"Our Company Names"` (clarifies these are our own details to skip)
  - Customer extraction: added `From`, `Buyer`, `Ordered By`, `Purchasing Company` as PO-specific labels
  - SKU labels split by document issuer (customer PO vs our own documents) to handle `"Our Code"` / `"Your Code"` perspective inversion

### Phase 3: Interactive Template Builder Frontend
* **Goal:** A user-friendly web interface allowing operators to visually construct parsing templates for new customers.
* **Key Tasks:**
  1. Develop a simple interactive view rendering the PDF page (using PDF.js or rendered page images).
  2. Provide a point-and-click or drag-to-draw interface to define bounding boxes (`bbox`) for specific fields.
  3. Save these interactive rules back to the database as new JSON templates.

### Phase 4: MS Graph API Integration & Security
* **Goal:** Connect to live Microsoft Outlook mailboxes, poll for new invoices, and secure the application.
* **Key Tasks:**
  1. Port the MSAL (Microsoft Authentication Library) Graph API authorization flow.
  2. Implement background mailbox polling with duplicate detection and error containment.
  3. Integrate security measures (e.g., input sanitization, database parameterization, token encryption, and administrative authentication).

### Phase 5: Production Operator Dashboard
* **Goal:** Provide a comprehensive frontend console to manage the system.
* **Key Tasks:**
  1. List and search processed invoices, customers, and templates.
  2. Implement alert/flag interfaces for invoices requiring manual correction (e.g. confidence scores below threshold).
  3. Display system health metrics and email pipeline history logs.

---

## 4. Open Questions for User Review

Please review the following design and technical questions before we begin Phase 1:

> [!IMPORTANT]
> **Question 1: Strategy for Custom Fields**
> Different companies format totals and dates in wild ways (e.g. currency symbols, tax breakdowns, multiple dates). Do you want the initial Phase 1 parser to extract only a fixed set of fields (e.g., Invoice Number, Date, Subtotal, Tax, Total, and Line Items) or do you want dynamic support for custom key-value pairs?
>
> *Recommended Approach:* Let's start with a robust, fixed set of core fields for Phase 1/2 to keep the database and templates simple, and extend to custom fields later if needed.

> [!IMPORTANT]
> **Question 2: OCR Fallback Dependency**
> Some PDFs might be scanned images. In `v2`, PyMuPDF and Tesseract OCR were utilized. Do you want to preserve this OCR fallback path in `v3` Phase 1, or should we keep the initial script 100% native Python (e.g., pure `pdfplumber`) to avoid requiring a local Tesseract binary installation during early development?
>
> *Recommended Approach:* Keep Tesseract OCR as a decoupled option (or modular fallback) so that the core pipeline remains easily portable without complex binary setup, but include hooks for it.

> [!IMPORTANT]
> **Question 3: PDF Visualization Format**
> For the visual template builder in Phase 3, do you prefer rendering PDF pages to high-resolution PNGs on the backend (using PyMuPDF) and loading them in standard HTML canvas, or using client-side PDF.js rendering?
>
> *Recommended Approach:* Backend PNG rendering is extremely simple, highly portable, and guarantees that the operator sees exactly what coordinate-based Python parses.
