# /review-invoices

Review parsed invoice documents that fell below a confidence threshold.
Uses your Claude Code subscription — no separate API key required.

## Usage

```bash
/review-invoices
/review-invoices 0.8
/review-invoices 0.5 --fix
```

---

## Purpose

This command performs a deterministic second-pass review of invoice extraction results.

It is designed to:
- recover missing fields
- correct obvious extraction mistakes
- validate line items mathematically
- improve confidence scoring
- identify template weaknesses

This command must behave conservatively:
- prefer null over guessing
- validate before updating
- never overwrite trusted values
- use invoice structure rather than assumptions

---

## Your role

You are a structured invoice extraction reviewer.

Your task is to:
1. Load low-confidence parsed invoices
2. Read the associated raw extracted text
3. Re-check missing or suspicious fields
4. Validate extracted values
5. Optionally write safe corrections back to JSON
6. Suggest template improvements when patterns repeat

---

## Deterministic review rules

Always follow these rules.

### Rule 1 — Never guess

If a value is not clearly supported by the document:
- leave it null
- mark it as unclear

### Rule 2 — Prefer structure over proximity

Use:
- labels
- column alignment
- invoice layout
- arithmetic consistency

Do not rely only on nearest text.

### Rule 3 — Never overwrite trusted values

Only update:
- null fields
- empty strings
- clearly invalid values

Existing valid values must remain unchanged.

### Rule 4 — Validate before accepting

For amounts and line items:
- confirm values using arithmetic
- reject values that do not reconcile

---

## Step 1 — Parse arguments

If a numeric argument exists:
- use it as the confidence threshold

If `--fix` exists:
- write safe improvements back to disk

Otherwise, read `LOW_CONFIDENCE_THRESHOLD` from `config/settings.py`.

Default: `0.6`

---

## Step 2 — Find review candidates

Scan `parsed/**/*.json`. Select files where **any** apply:
- `needs_review == true`
- `confidence < threshold`
- `status == "extracted_only"`
- `status == "low_confidence"`
- any line item has `qty`, `unit_price`, and `total` all present but `qty × unit_price` differs from `total` by more than `0.02` (arithmetic failure — catches wrong column captures even on high-confidence docs)
- `delivery_date` is null and the attachment filename contains a parseable date (filename fallback available)

Immediately report:

```
Found N documents to review.
```

If none are found, stop.

---

## Step 3 — Load document context

For each candidate, load:
1. parsed JSON
2. raw text file
3. template YAML if available
4. address book match if available

Derive raw text path by replacing `attachments/.../file.pdf` with `raw_text/.../file.txt` — same folder and filename stem.

Also record:
- original filename
- attachment filename
- template name
- customer name

The filename may later be used as a fallback source for delivery date.

---

## Step 4 — Identify fields needing review

Review only fields that are:
- null
- missing
- empty
- internally inconsistent

Suspicious values include:
- quantity equals row number
- subtotal greater than total
- line totals do not reconcile
- invalid dates
- duplicate invoice fragments

Mark only these fields for re-evaluation.

---

## Step 5 — Deterministic field extraction

Extract using explicit rules.

### Invoice number

Look near labels: `Invoice No`, `Invoice #`, `Invoice Number`, `Tax Invoice`

Reject values that:
- look like page numbers
- match customer PO unless clearly labelled

### Invoice date

Look near: `Invoice Date`, `Date`, `Tax Date`

Accept:
- `DD/MM/YYYY`
- `DD-MM-YYYY`
- `YYYY-MM-DD`
- `DD MMM YYYY`

Reject impossible dates.

### Delivery date

Look near: `Delivery Date`, `Delivered`, `Delivery`, `Despatch Date`, `Shipment Date`

Accept the same date formats as invoice date.

**Fallback rule:** If no delivery date exists in the document text, and the attachment filename contains a valid date, use that date as the delivery date.

Examples:
- `delivery_2026-04-25.pdf`
- `INV_25-04-2026.xlsx`
- `20260425_supplier_invoice.pdf`

Only use filename date if:
- delivery date is missing
- the filename contains exactly one clear valid date
- the extracted date is plausible

Do not override an existing delivery date from the document.

### Supplier

Prefer (in order):
1. template supplier
2. address book match
3. ABN-linked business name
4. document header

### ABN

Accept 11-digit Australian ABN. Normalize by removing spaces.

Example: `12 345 678 901` → `12345678901`

### Totals

Extract: `subtotal`, `GST`, `total`

Validate: `subtotal + GST ≈ total` (tolerance: `±0.02`)

If values do not reconcile, mark as suspicious.

---

## Step 6 — Deterministic line item extraction

Each line item should contain:
- `description`
- `quantity`
- `uom`
- `unit_price`
- `line_total`

Expected structure: `description | qty | uom | unit_price | line_total`

### Quantity rules

Quantity should:
- be numeric
- usually near UOM
- usually before unit price
- support decimals (e.g. `1`, `2`, `12`, `1.5`)

Never use: row number, item code, page number, sheet position.

If a numeric column increments sequentially (`1`, `2`, `3`, `4`), treat it as a **line number only** — never quantity.

### UOM rules

Common UOM values: `EA`, `EACH`, `BOX`, `BX`, `CTN`, `PK`, `PACK`, `KG`, `G`, `L`, `ML`, `ROLL`, `BAG`, `CASE`

UOM usually:
- follows quantity
- precedes unit price
- appears as a short uppercase token

If unclear, leave null. Never invent a UOM.

### OCR merged values

Split merged values such as:
- `24EA` → `quantity=24`, `uom=EA`
- `2BOX` → `quantity=2`, `uom=BOX`
- `1.5KG` → `quantity=1.5`, `uom=KG`

### Arithmetic validation

Validate each row: `quantity × unit_price ≈ line_total` (tolerance: `±0.02`)

If invalid, re-check the quantity candidate. Prefer the value that makes the row mathematically correct.

**Example:**

| Field | Wrong | Correct |
|-------|-------|---------|
| qty | `1` | `2` |
| unit_price | `24.50` | `24.50` |
| line_total | `49.00` | `49.00` |

Because `2 × 24.50 = 49.00`.

---

## Step 7 — Confidence recalculation

**Increase** confidence when:
- arithmetic validates
- required fields found
- line items reconcile
- filename fallback successfully fills missing delivery date

**Reduce** confidence when:
- values ambiguous
- totals mismatch
- quantity uncertain
- OCR corruption severe

Confidence range: `0.00` to `1.00`

---

## Step 8 — Output review summary

For each document show:

```
📄 invoice123.json | ABC Pty Ltd | confidence 0.42 → 0.83
✅ invoice_number: INV-10455
✅ delivery_date: 2026-04-25 (from filename)
✅ total_amount: 425.80
✅ line_items: corrected quantity on 2 rows
⚠ UOM missing on 1 row
```

After all documents show:

```
Reviewed: N
Improved: M
Unclear: K
```

---

## Step 9 — If `--fix` supplied

Only for improved documents:
1. Load JSON
2. Fill null fields only
3. Correct clearly invalid line items
4. Set `_claude_reviewed: true`
5. Update `needs_review`
6. Write back to same file
7. Log: `Updated: path/to/file.json`

---

## Step 10 — Template improvement suggestions

Group repeated failures by template.

**Example:**

```
💡 Template suggestion for supplier_x

Field: quantity
Issue: first numeric column captured as quantity
Suggestion: ignore leading sequence column

Field: delivery_date
Issue: date often only present in filename
Suggestion: fallback to filename date when field missing

Field: uom
Issue: merged OCR values like 24EA
Suggestion: ([0-9.]+)(EA|BOX|KG|PK)
```

If the same issue appears in 3 or more files, recommend:

```bash
/generate-template <supplier>
```

---

## What NOT to do

Never:
- overwrite valid fields
- modify templates directly
- fabricate values
- invent UOM
- inflate confidence without evidence

When uncertain, leave null.

---

## File paths

```
parsed/
raw_text/
config/templates/
config/address_book.json
config/settings.py
```

Use `attachment_path` to derive `raw_text` path.
