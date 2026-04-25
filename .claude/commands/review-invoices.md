# /review-invoices

Review parsed invoice documents that fell below a confidence threshold.
Uses your Claude Code subscription — no separate API key required.

**Usage:**
```
/review-invoices              # review all flagged (needs_review=true + extracted_only)
/review-invoices 0.8          # review all documents below 0.8 confidence
/review-invoices 0.5 --fix    # review below 0.5 and auto-write improvements to JSON
```

---

## Your role

You are a document field extraction specialist. Your job is to:
1. Find parsed invoice/PO documents that were flagged for review
2. Read their extracted text and identify what the regex templates missed
3. Re-extract the missing or incorrect fields using your own understanding
4. Report what you found and optionally update the JSON outputs

You are working with documents already processed by the pipeline. The raw text is already extracted — you do not need to re-run the pipeline.

---

## Process — follow exactly

### Step 1 — Parse the argument

- If an argument was given and it looks like a float (e.g. `0.8`), use it as the confidence threshold.
- If `--fix` is present, you will write improvements back to disk at the end.
- Otherwise default threshold = read `LOW_CONFIDENCE_THRESHOLD` from `config/settings.py` (typically 0.6).

### Step 2 — Find documents to review

Scan `parsed/` recursively for all `.json` files.
For each file, load it and collect if ANY of:
- `needs_review` is `true`
- `confidence` < threshold
- `status` == `"extracted_only"` or `"low_confidence"`

Report the count immediately: "Found N documents to review."

If none found, say so and stop.

### Step 3 — For each document, load context

For each candidate document (work through them one at a time):

1. **Load the JSON** from `parsed/`
2. **Find the raw text**: look in `raw_text/` for a folder matching the same date/domain/hash slug as the attachment path, then the `.txt` file with the same stem as the attachment filename.
   - `attachment_path` in the JSON gives you the full path — derive the raw text path:
     `raw_text/<folder>/<stem>.txt`
3. **Load the template** (if `template_name` is set in the JSON):
   - Read `config/templates/<template_name>.yaml` to know which fields are expected
4. **Load the address book entry** for this customer:
   - Read `config/address_book.json`, find the contact matching `customer_name`

### Step 4 — Re-extract missing fields

With the raw text in hand, do your own extraction for every field that is `null` or missing.

Think carefully about:
- OCR artefacts: split words, extra spaces inside numbers, merged columns
- Field labels that differ from what the template regex expects
- Fields present in the document but under different labels (e.g. "Net Amount" vs "Subtotal")
- Line items that were missed entirely

For `extracted_only` documents (no template): extract every structured field you can find
(invoice number, date, amounts, ABN, supplier, line items, etc.)

### Step 5 — Present findings

For each document, show a compact table:

```
📄 <filename> | <customer_name> | confidence: <old> → <new>
   ✅ invoice_number: "INV-20260424" (was null)
   ✅ total_amount: "1250.00" (was null)
   ⚠️  delivery_date: could not find in text
   📋 line_items: found 3 (was 0)
```

After processing ALL documents, show a summary:
```
Reviewed: N | Improved: M | Still unclear: K
```

### Step 6 — If `--fix` was passed, write improvements

For each document where you found improvements:
1. Load the existing JSON
2. Merge your findings in — **only fill null fields, do not overwrite existing non-null values**
3. Recalculate `_confidence` if you have the template's `required_fields`
4. Set `_claude_reviewed: true`
5. Update `needs_review` to `false` if new confidence >= threshold
6. Write the updated JSON back to disk (same path)
7. Log: "Updated: <path>"

### Step 7 — Template improvement suggestions

After reviewing, identify any **systematic** patterns where the regex template consistently fails.
Group by template name and suggest specific regex pattern additions:

```
💡 Template suggestion for 'evergy':
   Field 'delivery_date' — found in text as "Delivery: DD/MM/YYYY"
   Add pattern: 'Delivery:\s+(\d{1,2}/\d{2}/\d{4})'
```

If there are 3+ documents from the same sender all missing the same field, strongly recommend
running `/generate-template <sender>` to regenerate the template with the new examples.

---

## What NOT to do

- Do not modify `config/templates/*.yaml` — suggest changes, let the user run `/generate-template`
- Do not re-download or re-process emails — work only with files already on disk
- Do not remove fields that already have values — only fill nulls and add missing line items
- Do not hallucinate field values — if you genuinely cannot find a field in the raw text, leave it null

---

## File paths reference

```
parsed/                          ← JSON outputs from the pipeline
  YYYY-MM-DD_domain_hash/
    filename.json

raw_text/                        ← extracted text (one .txt per attachment)
  YYYY-MM-DD_domain_hash/
    filename.txt

config/templates/                ← YAML regex templates
config/address_book.json         ← known senders
config/settings.py               ← LOW_CONFIDENCE_THRESHOLD value
```

The `attachment_path` field inside each JSON file is the full path to the original file.
Derive the raw text path by replacing `attachments/` with `raw_text/` and changing the extension to `.txt`.
