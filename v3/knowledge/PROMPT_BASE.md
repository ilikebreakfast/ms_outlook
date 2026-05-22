# Static Extraction Guidelines

These guidelines are written by the operator and merged into the auto-generated `PROMPT.md`.
Edit this file to add persistent rules that the LLM should always follow.

## Vendor / Issuer Identity

- This system belongs to a single company (the invoice *issuer*).
- Their name, ABN, email, and phone are defined in `v3/vendor_config.json` (gitignored).
- **Never** extract the issuer's details as the customer.

## Customer Identification Rules

- Look for labels: `Bill To`, `Ship To`, `Deliver To`, `Sold To`, or the second party listed after `Supplier:`.
- On Picking Slips, the delivery address is the customer address.
- On Purchase Orders, the `Buyer` or `Ordered By` field is the customer.

## Line Item Rules

- SKUs are typically 4–8 digit numeric codes (e.g. `15335`, `48245`) or alphanumeric (e.g. `JGKITHB 1007001414`).
- If a document has no prices (e.g. a Picking Slip), set `unit_price` and `line_total` to null — do not invent values.
- UOM examples: `Kg`, `EA`, `ctn`, `box`, `bag`, `L`, `pcs`.

## Math Rules

- `quantity × unit_price = line_total` must hold within $0.02.
- If unsure which number is the unit price vs the line total, the smaller value is usually the unit price.

## Confidence Rules

- `"high"`: value is clearly visible and unambiguous on the document.
- `"medium"`: value was inferred or is partially visible.
- `"low"`: value was guessed or is absent.
