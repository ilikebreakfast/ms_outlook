# MS Outlook Invoice Processing Pipeline v3: Step-by-Step Robust Roadmap

Welcome to **Version 3 (v3)** of the Invoice Processing Pipeline! This version adopts a progressive, "one-piece-at-a-time" approach to building a robust, maintainable, and highly accurate system.

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

### Phase 1: High-Accuracy Attachment Parsing (Current Focus)
* **Goal:** Extract clean text and tabular line items from all document formats (.pdf, .csv, .txt) with 100% correctness.
* **Key Tasks:**
  1. Set up a core Python parser harness that processes files in `v3/data/attachments/`.
  2. Implement an advanced `pdfplumber` extractor utilizing coordinate anchoring, region cropping, and fine-tuned `table_settings`.
  3. Support fallback mechanisms (e.g. OCR) for scanned PDFs if native text extraction yields sparse characters.
  4. Build a CLI validation tool to verify and review the extracted layout and raw text for each local file.

### Phase 2: Template Rules Engine & Database Ingestion
* **Goal:** Build the logical engine to learn parser templates, store parsed customer and invoice records, and apply templates to new files automatically.
* **Key Tasks:**
  1. Define a JSON template schema (e.g. key coordinates, regex anchors, or table extraction strategies per sender).
  2. Design and implement a robust SQLite database schema to house:
     * `customers`: Profile details and associated template definitions.
     * `invoices`: Parsed invoices with fields (Invoice #, Date, Total, Tax) and line items.
  3. Create matching logic that identifies the sender (using `mock_senders.json` / incoming email), loads their template, and processes the attachment.

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
