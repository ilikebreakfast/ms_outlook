# Automated Invoice Processing Pipeline v2 — Master Architecture Plan

This document outlines the ground-up rewrite of the automated invoice processing pipeline (v2). It transitions the system from expensive, unpredictable per-document agentic LLM parsing to a **100% deterministic, layout-fingerprinted rule engine** with a one-time cost-effective LLM bootstrapping and interactive human approval flow.

---

## 1. Current State Summary (v1 Audit)

### What v1 Does
* Fetches unread emails from Microsoft Graph API.
* Checks a manual sender allowlist (`address_book.json`).
* Downloads attachments, performs multi-stage security validation (magic bytes, structural PDF scanning, optional ClamAV, prompt injection scrubbing).
* Performs text extraction (pdfplumber table-aware native $\rightarrow$ PyMuPDF + Tesseract OCR fallback).
* Attempts regex parsing using static hand-written `.yaml` templates.
* Falls back to a Claude Haiku LLM reviewer for every low-confidence document.
* Writes flat JSON records, updates a stub SQLite database, and moves processed emails.

### What Works Well (To Keep in v2)
* **Email Ingestion:** Microsoft Graph API fetching and attachment downloading is architecturally sound.
* **Layered Security Model:** Magic bytes verification, PDF structural sanitization, and Tesseract sandboxing are excellent and will be migrated to the core security layer of v2.
* **Text Extraction Stack:** The table-aware cell layout preservation in `pdfplumber` and PyMuPDF-based rendering for OCR work very well and form a solid raw text baseline.

### What is Brittle & Costly (To Drop Entirely)
* **Global Document Regexes:** Matching variables via standard document-wide regular expressions is highly fragile. Slight spacing changes, logo shifts, or OCR artifacts cause total matching failure.
* **Per-Document LLM Fallbacks:** Escalating every failed document to Claude Haiku creates unpredictable costs at scale and slows down processing times.
* **Manual JSON/YAML Configuration:** Operators must manually edit `address_book.json` and create custom YAML regex templates by hand.
* **CLI-Only Operations:** Lacks real-time dashboards for queue resolution, monitoring, or human template validation.

---

## 2. v2 Architecture Proposal

```
                                 [INCOMING ATTACHMENT]
                                           │
                                           ▼
                                 [Security Validation]
                                           │
                                           ▼
                                   [Text Extraction]
                               (Native PDF or OCR / TSV)
                                           │
                                           ▼
                               [Layout Fingerprinting]
                           (Text Sequence or Grid Hash)
                                           │
                                  ┌────────┴────────┐
                        [Known Hash]             [New Hash]
                              │                          │
                              ▼                          ▼
                   [Deterministic Engine]      [Bootstrapping Engine]
                   (Run cached database         (One-Time Cheap LLM Call
                   anchors & col mappings)     Deepseek V3 / Claude Haiku)
                              │                          │
                              │                          ▼
                              │                 [Draft Parsing Rule]
                              │                          │
                              │                          ▼
                              │                [FastAPI Web Interface]
                              │               (Human reviews, adjusts,
                              │                 and approves rule)
                              │                          │
                              │                          ▼
                              │                 [Active DB Rule Lock]
                              │                          │
                              └────────┬─────────────────┘
                                       │
                                       ▼
                             [Downstream Output]
                         (Schema-validated JSON/SQL)
```

### Ingestion and Routing
* **Unified Pipeline Entry:** Attachments enter the pipeline, pass through security scrubbing, and are converted to a standardized structural text envelope.
* **Classification Router:** The router queries the database for the document's **Layout Fingerprint**.
  * If a **matching active rule** exists: The document is routed to the **Deterministic Rule Engine**.
  * If a **matching pending rule** exists: The document is held, and the pipeline registers a duplicate wait state.
  * If the **fingerprint is unknown**: The document is routed to the **One-Time Bootstrapping Engine**.

### Establishing Supplier & Template Identity
* **PDFs & Images (Text Block Sequence Fingerprinting):** The system normalizes the document's raw text by stripping all digits, dates, email addresses, phone numbers, and potential variable values. It then extracts the sequential pattern of static headings and labels (e.g., `INVOICE`, `ABN`, `SUBTOTAL`, `DESCRIPTION`, `QTY`) and generates a unique SHA-256 hash. This represents the visual "signature" of the supplier layout.
* **Spreadsheets (Column Schema Fingerprinting):** For Excel or CSV files, the system extracts the first few non-empty rows, normalizes the column header names, and generates an ordered schema hash.

### The Parsing Stack
* **Deterministic Rule Engine (100% of production runs):** Runs local, fast database-stored rules. It utilizes **proximity anchoring** and **header column index mappings** instead of document-wide regexes.
* **One-Time LLM Bootstrapping (One-time setup per layout):** Triggered only once per new layout. It queries a low-cost API (Deepseek V3 or Claude Haiku) to identify anchors and relative values, saving the structured rules to the database.

### Drift Detection & Handling
* **Fail-Safe Halt (Strict Mode):** If a supplier modifies their layout, the incoming document's text sequence hash changes. The router instantly detects that the fingerprint does not match any active template. Because of strict compliance, the engine **halts deterministic parsing** for this document and routes the invoice to the Human-in-the-Loop review queue to prevent incorrect extraction or silent drift failures.

### Human-in-the-Loop Integration
* A modern web interface displays pending, drifted, or unapproved template suggestions. Operators review the LLM-drafted extraction rules, click a preview button, adjust selectors if necessary, and save the active template directly to the database.

---

## 3. Parsing Strategy Deep-Dive

### Layout & Structural Fingerprinting
```python
def generate_text_layout_hash(raw_text: str) -> str:
    # Normalize text: convert to lowercase, strip numbers, dates, emails, and punctuation
    text = raw_text.lower()
    text = re.sub(r'\b\d+\b', '', text)  # Strip standalone numbers
    text = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', '', text) # Strip emails
    text = re.sub(r'\b\d{2}[/.-]\d{2}[/.-]\d{4}\b', '', text) # Strip common date formats
    
    # Extract static layout words (filtering out short words and common noise)
    words = re.findall(r'\b[a-z]{4,20}\b', text)
    static_sequence = " ".join(words[:150]) # Use first 150 layout-defining words
    
    return hashlib.sha256(static_sequence.encode('utf-8')).hexdigest()
```

### Field Anchoring (Deterministic Rule Mapping)
Instead of searching globally, the deterministic parsing engine executes **Anchored Rules** stored as JSON in the database:
```json
{
  "fields": {
    "invoice_number": {
      "anchor_keyword": "invoice number",
      "search_direction": "relative_right",
      "window_characters": 120,
      "regex_pattern": "([A-Z0-9-]{4,15})"
    },
    "supplier_abn": {
      "anchor_keyword": "abn",
      "search_direction": "relative_right",
      "window_characters": 80,
      "regex_pattern": "(\\d{2}\\s?\\d{3}\\s?\\d{3}\\s?\\d{3})"
    }
  },
  "line_items": {
    "strategy": "tabular_columns",
    "columns": {
      "quantity": {"header": "qty", "col_index": 0},
      "description": {"header": "description", "col_index": 1},
      "unit_price": {"header": "price", "col_index": 2},
      "total": {"header": "total", "col_index": 3}
    }
  }
}
```

### Bootstrapping Flow & Interactive Approvals
1. **Unrecognized Hash:** Pipeline encounters a new document fingerprint.
2. **Cheap LLM Call:** Call Deepseek V3 or Claude Haiku with the raw document text. The prompt forces the LLM to output a JSON rule mapping structure rather than the extracted values themselves.
3. **Pending Database Record:** Write a pending template to the `layout_rules` table:
   ```sql
   INSERT INTO layout_rules (layout_hash, supplier_name, status, extraction_rules, sample_raw_text)
   VALUES ('a3f9e8...', 'Acme Corp', 'pending_approval', '{...rules...}', '...');
   ```
4. **Interactive Dashboard:** The FastAPI dashboard fetches pending approvals. It shows the operator a side-by-side view: the raw text and the target schema fields. It displays what the LLM's draft rule extracts in real-time.
5. **DB Active Lock:** The operator edits any weak anchors and clicks "Approve & Lock". The status transitions to `active`.

---

## 4. Phased Build Plan

This plan is structured into small, self-contained tasks that can be executed in sequence.

### Phase 1: Database Foundation & Schema Setup
* **Goal:** Create the relational core using SQLite in WAL mode to store layout templates, rules, extraction logs, and review queues.
* **Task Brief:**
  ```markdown
  Create the SQLite persistence layer at `v2/database/db.py`.
  Enable WAL mode (Write-Ahead Logging) on connection establishment.
  Implement the following tables:
  1. `layout_rules`
     - `layout_hash` TEXT PRIMARY KEY
     - `supplier_name` TEXT
     - `document_type` TEXT (pdf, excel, csv)
     - `status` TEXT (pending_approval, active, inactive)
     - `extraction_rules` TEXT (JSON schema-mapping configuration)
     - `created_at` TIMESTAMP
     - `updated_at` TIMESTAMP
  2. `extraction_history`
     - `id` INTEGER PRIMARY KEY AUTOINCREMENT
     - `message_id` TEXT
     - `filename` TEXT
     - `layout_hash` TEXT
     - `parsed_data` TEXT (JSON payload of values extracted)
     - `status` TEXT (success, drifted, failed)
     - `processed_at` TIMESTAMP
  3. `review_queue`
     - `id` INTEGER PRIMARY KEY AUTOINCREMENT
     - `extraction_history_id` INTEGER
     - `reason` TEXT
     - `status` TEXT (pending, resolved)
     - `created_at` TIMESTAMP

  Provide basic CRUD functions for fetching active templates, inserting layout templates, and inserting logs.
  Write a test script at `v2/tests/test_db.py` validating concurrent read/write and constraints.
  ```

### Phase 2: Ingestion, Security, & Text Extraction Migration
* **Goal:** Port the strong v1 Graph API ingestion, strict security scanners, and robust native/OCR/Excel text extraction blocks into the clean v2 structure.
* **Task Brief:**
  ```markdown
  Re-organize and port the following components from `v1/` into `v2/core/`:
  1. `v2/core/security.py`: Port the magic-byte scanner, strict PDF structural JS/auto-action scanner, and prompt injection scrub. Implement a standard `SecurityValidator` class.
  2. `v2/core/extractor.py`: Port the PDF native (`pdfplumber` with table aware cells) and PyMuPDF + Tesseract OCR fallbacks. Port the spreadsheet TSV flattener. Implement `DocumentTextExtractor` returning `(raw_text, is_native)`.
  3. `v2/core/ingest.py`: Port Microsoft Graph API fetching and caching logic.
  
  Write a CLI validation script `v2/run_extractor.py` that processes a local file path, validates security, extracts raw text, and prints it out.
  ```

### Phase 3: Layout Fingerprinting & Deterministic Parser Engine
* **Goal:** Build the normalized fingerprint generator and the anchored deterministic extraction rules engine.
* **Task Brief:**
  ```markdown
  Implement the routing logic at `v2/core/parser.py`:
  1. Implement `generate_layout_hash(raw_text: str, file_type: str) -> str`.
     - For PDFs/Images: Normalize raw text by removing numbers, emails, dates, and whitespace. Extract the sequence of static keywords to form a SHA-256 layout signature.
     - For spreadsheets: Extract the first 3 rows, normalize header columns, and hash the sequence.
  2. Implement `DeterministicParser`:
     - Accepts `raw_text` and the `extraction_rules` JSON block.
     - Extracts fields using visual proximity anchors: locates the `anchor_keyword`, crops a search window (defined by `window_characters`), and executes the specific field regex inside that local window.
     - For spreadsheet documents: parses rows directly by mapping mapped schema keys to column indices.
     
  Write unit tests at `v2/tests/test_parser.py` validating that the parser successfully extracts standard invoice fields using layout rule parameters.
  ```

### Phase 4: Cheap LLM One-Time Bootstrapper
* **Goal:** Wire up the Deepseek V3 or Claude Haiku one-time LLM API generator that creates the rule mappings for new templates.
* **Task Brief:**
  ```markdown
  Create the template bootstrapping engine at `v2/core/bootstrapper.py`:
  1. Define a system prompt that forces the LLM to output a JSON rule mapping (anchors and local regex patterns) instead of the document values.
  2. Build the API client wrapper supporting either Deepseek V3/r1 or Claude 3.5 Haiku.
  3. Implement `bootstrap_template(raw_text: str, document_type: str) -> dict`:
     - Passes the raw text and instructions to the LLM.
     - Validates the returned JSON rule mapping structure.
     - Saves the suggestion to the database `layout_rules` table under status `pending_approval`.
     
  Write a test script at `v2/tests/test_bootstrap.py` checking that mock raw text successfully returns a structured mapping JSON format.
  ```

### Phase 5: FastAPI Glassmorphic Web Dashboard (HITL Review)
* **Goal:** Create the modern Web UI that lets operators approve LLM-drafted rules, edit anchors, and resolve drifted invoices.
* **Task Brief:**
  ```markdown
  Create a modern single-page FastAPI web application at `v2/dashboard/`:
  1. Build an API router in FastAPI serving database metrics, pending template lists, and detailed review structures.
  2. Implement an elegant, glassmorphic dark-mode web page using Vanilla HTML, CSS, and JS (Google Fonts Outfit/Inter, smooth transitions, glowing card borders).
  3. Include a side-by-side template editor screen:
     - Left pane: Raw document text with highlighted matches.
     - Right pane: Input fields showing the LLM-drafted anchors, search directions, and local regexes.
     - Allow the operator to edit anchors and regexes, hit "Test Extraction" to dynamically preview matches, and click "Approve & Activate" to save the active rule to the database.
     
  Ensure the dashboard can be run locally via `uvicorn main:app --reload`.
  ```

### Phase 6: Orchestrator, Scheduling, and End-to-End Verification
* **Goal:** Tie the ingest, security validation, router, deterministic parser, bootstrapper, and database layers together under a single main command loop with robust scheduling.
* **Task Brief:**
  ```markdown
  Implement the main pipeline orchestrator at `v2/main.py`:
  1. Fetch unread emails.
  2. Validate security and extract raw text.
  3. Generate layout fingerprint.
  4. Query database layout status.
     - If active: Run the deterministic parser, save output to database, move email.
     - If new layout: Trigger one-time LLM bootstrapper to save draft rules as `pending_approval`, append document to the review queue, and notify the operator via webhook. Do not attempt parsing.
     - If layout is pending_approval: Halt parsing and hold document.
  5. Add command-line arguments: `--schedule INTERVAL` (e.g. 5m, 1h), `--days N` lookback, and `--check-auth` pre-flight verify.
  6. Setup a Docker Compose profile supporting the uvicorn web dashboard and the scheduled background processing service.
  
  Perform full end-to-end verification and export statistics logs.
  ```

---

## 5. Open Questions & Explicit Assumptions

### Assumptions
* **[A-01] Layout Uniformity:** It is assumed that suppliers generate PDFs programmatically, which ensures high text sequence uniformity. For scanned invoices, we assume Tesseract OCR provides sufficient structural consistency to match sequence hashes.
* **[A-02] Active API Connectivity:** One-time template generation requires live access to either Deepseek or Anthropic API endpoints. If connectivity is down, bootstrapping holds documents until connection is restored.
* **[A-03] Security Boundaries:** We assume that all inbound documents are untrusted, keeping the v1 sandbox isolation layers completely intact for v2.

### Open Questions
1. **Line Item Structure Diversity:** Some complex multi-page invoices contain tables with nested rows, descriptions spanning multiple visual lines, or page breaks. Will v2 support multi-page invoice line-item spanning out of the box, or should multi-page tabular invoices trigger manual coordinate mapping in Phase 5?
2. **Notification Channel Preference:** When a new template is bootstrapped or template drift occurs, how should the pipeline proactively notify operators? (e.g., Slack Webhook, Microsoft Teams Webhook, or simple email alerts)?

---

## 6. Agent Delegation & Parallelism Suggestions

To build v2 efficiently, work can be delegated to separate specialized agents or executed in parallel:

```
                                  [Database Core (Phase 1)]
                                              │
                                              ▼
                ┌─────────────────────────────┼─────────────────────────────┐
                │                             │                             │
                ▼                             ▼                             ▼
       [Security & Text]             [Parser & Routing]             [Dashboard UI]
          (Phase 2)                      (Phase 3)                     (Phase 5)
                │                             │                             │
                └─────────────────────────────┼─────────────────────────────┘
                                              │
                                              ▼
                                   [Bootstrapper (Phase 4)]
                                              │
                                              ▼
                                   [Orchestration (Phase 6)]
```

### Strict Sequencing Dependencies
1. **Phase 1 (Database Core)** must be completed first. All other components rely on reading and writing layout rules and extraction history records.
2. **Phase 3 (Parser & Routing)** must precede **Phase 4 (Bootstrapper)** because the bootstrapper must output rules formatted exactly for the parser.
3. **Phase 6 (Orchestration)** must be executed last, as it imports and schedules all completed components.

### Parallel Execution Tracks (Safe to run concurrently)
* **Track A (Text Extraction & Security):** An agent can independently build `v2/core/security.py` and `v2/core/extractor.py` by porting the v1 code, as these have no direct database dependencies.
* **Track B (FastAPI Dashboard UI):** An agent can design and mock the web review dashboard pages in parallel with the parser logic, as long as the API contracts (fetching pending layouts and editing JSON rules) are pre-agreed.
