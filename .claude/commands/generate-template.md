# /generate-template

You are a regex pattern specialist. Your job is to generate a production-ready YAML extraction
template for the ms_outlook_review pipeline and wire it into `config/address_book.json`.

**Usage (three modes):**

```
/generate-template jane@hotmail.com          # personal email — matched by exact address
/generate-template topcut.com.au             # org domain — matched by domain
/generate-template topcut                    # contact already in address_book, no template yet
```

### Determining the mode

1. **Arg contains `@`** → personal email mode.
   - Template stem: `firstname_lastname` derived from the display name, or `user_at_domain` if unknown
   - address_book match key: `"emails": ["jane@hotmail.com"]`

2. **Arg contains `.` but no `@`** → org domain mode.
   - Template stem: domain stem (e.g. `topcut` from `topcut.com.au`)
   - address_book match key: `"domains": ["topcut.com.au"]`

3. **Arg is a plain word** → lookup mode.
   - Read `config/address_book.json`, find the contact whose `name` or existing `template` stem
     matches the arg (case-insensitive). Use their `emails`/`domains` to find raw text files.
   - If `"template"` is empty or missing, generate one. If it already exists in `config/templates/`,
     ask the user whether they want to regenerate or just re-test.

**In all modes:** after determining the contact, proceed with Steps 1–8 below.

---

## Your role

You extract structured invoice/PO data using regex patterns. The pipeline reads raw text from
PDFs (via pdfplumber/Tesseract OCR) and matches it against YAML templates. You must write
patterns that account for OCR artefacts (split words, inconsistent spacing, merged columns).

**Fields to extract** (get as many as the document contains):

| Field | What to look for |
|---|---|
| `po_number` | `Order Number`, `PO Number`, `Order:`, `REF:`, standalone alphanumeric on its own line |
| `order_date` | `Order date`, `Date`, `Created On` — handles dots (23.04.2026), text months (23/Apr/26), hyphens (23-APR-26) |
| `delivery_date` | `Date of delivery`, `Delivery Date`, `Delivery:` |
| `company_name` | `Company Name`, `Customer:`, `Ship To:`, `STOCK <name>`, first meaningful line of doc |
| `company_abn` | `ABN`, `Tax registration number` — may have OCR spaces inside number |
| `subtotal` | `Amount(net)`, `Sub-Total` |
| `tax_amount` | `Tax`, `TOTAL GST` |
| `total_amount` | `Amount(gross)`, `Total Amount Incl. Tax`, `TOTAL INCLUSIVE OF GST` |

**Line item named groups** (use any subset that the document contains):
`(?P<product_code>...)`, `(?P<description>...)`, `(?P<qty>...)`, `(?P<uom>...)`,
`(?P<unit_price>...)`, `(?P<subtotal>...)`, `(?P<total>...)`

---

## Process — follow exactly, iterate up to 3 times

### Step 1 — Discover raw text

Find folders in `raw_text/` whose name contains the sender's domain or a matching slug.
Read ALL `.txt` files found. If none exist, check `attachments/` for PDF or Excel files and note
that you'll need to run the pipeline first to generate raw text.

For Excel attachments (`.xlsx`), raw text is a tab-separated flat dump of every sheet.
Open the actual `.xlsx` file (or read the flat text) to see real column headers — these are
used for `fields_xlsx` and `line_items_xlsx` instead of regex patterns.

### Step 2 — Analyse each document

For each file, identify:
- **Document type**: PO, invoice, picking slip, stock order, etc.
- **File format**: PDF/image (use regex) vs Excel `.xlsx` (use `fields_xlsx` + `line_items_xlsx`)
- **Column layout of line items**: pipe-delimited (`|`) vs space-aligned columns vs positional (PDF);
  or exact column header strings (Excel)
- **Header field locations**: exact surrounding text or label cells for each field
- **OCR artefacts** (PDF only): split words, extra spaces inside numbers, merged columns

### Step 3 — Determine template name

Use the domain stem: `topcut.com.au` → `topcut`, `evergy.com.au` → `evergy`.
For personal emails (`gmail.com`, `hotmail.com`), use `firstname_lastname`.

### Step 4 — Write the template

Create `config/templates/<name>.yaml`:

```yaml
required_fields: [po_number, delivery_date]

fields:
  po_number:
    - 'Order\s+Number\s+([A-Z0-9]+)'
    - 'Order\s*:\s*(F\d+)'
  delivery_date:
    - 'Delivery\s+Date\s*:\s*([\d/]+)'
    - 'Date\s+of\s+delivery\s+(\d{1,2}\.\d{2}\.\d{4})'
  # ... all other fields

line_items_patterns:
  # One entry per distinct document format. Add comments explaining the format.
  - '^(?P<qty>\d+)\s+(?P<uom>Each|EA|KG)\s+(?P<product_code>\d{4,6})\s+(?P<description>.+?)$'
  - '^(?P<product_code>\d{5,6})\s+(?P<description>.+?)\s+(?P<qty>\d+\.\d{3})\s+(?P<unit_price>[\d]+\.\d{2})\s+(?P<subtotal>[\d,]+\.\d{2})\s+[\d.]+\s+(?P<total>[\d,]+\.\d{2})$'
```

**YAML pattern rules:**
- Single backslashes (`\s`, `\d`, `\n`) — NOT double as in JSON
- Use `^`/`$` with `re.MULTILINE` (already set by the parser) to anchor line-start patterns
- Lazy quantifiers (`.+?`) before known fixed-format columns prevent over-matching
- For OCR-split words, allow `\s*` or `\s+` in the middle: `Amount\s*\(g\s*ross\)`

### Step 5 — Test every document

Run for each PDF in `attachments/`:
```bash
python manage.py test-template <name> "<pdf_path>"
python manage.py test-template <name> "<pdf_path>" --show-text
```

Check the output for:
- `po_number`: not null ✓
- `delivery_date`: not null ✓
- `line_items`: list is non-empty ✓
- confidence > 0.60 ✓

### Step 6 — Iterate on failures (up to 3 attempts total)

For each null field or empty line_items list:
1. Use `--show-text` to see the exact extracted text
2. Find the exact text surrounding the field
3. Adjust the regex pattern — tighten anchors, fix OCR variants, add alternates
4. Re-run the test

Common fixes:
- `Amount(gross)` with OCR space → `Amount\s*\(g(?:ross|r\s*oss)\)`
- Date with dots → add `\d{1,2}\.\d{2}\.\d{4}` alternate
- Description truncated mid-line (PDF column) → use `\s{3,}` to match the large gap before next column
- Qty has 3 decimal places → use `\d+\.\d{3}` instead of `[\d.]+` to avoid matching description numbers

### Step 7 — Update address_book.json

Find the matching contact in `config/address_book.json`. Set `"template": "<name>"`.
If the contact doesn't exist yet, add it:
```json
{
  "name": "Supplier Name",
  "domains": ["domain.com.au"],
  "keywords": ["keyword1", "keyword2"],
  "template": "<name>"
}
```

### Step 8 — Report

Tell the user:
- Which fields were successfully extracted from each document
- Which fields are still null and why (e.g. "not present in any document", "OCR too fragmented")
- How many line items were extracted per document
- Final confidence score for each document

---

## Template format reference

See `config/templates/topcut.yaml` for a real worked example covering 5 document formats.
See `CLAUDE.md` → "YAML Template Format" for the full spec.
