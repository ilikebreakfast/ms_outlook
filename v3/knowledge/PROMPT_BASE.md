# Static Extraction Guidelines

These guidelines are written by the operator and merged into the auto-generated `PROMPT.md`.
Edit this file to add persistent rules that the LLM should always follow.

## Our Company Identity (Supplier / Order Recipient)

- This system belongs to the supplier company that *receives* orders from customers.
- We are the vendor/supplier on all documents — our name, ABN, email, and phone are in `v3/vendor_config.json`.
- **Never** extract our own details as the customer.
- Documents being parsed are typically **Purchase Orders sent TO us by our customers**, not invoices we issued.

## Customer Identification Rules

Documents are usually customer-issued Purchase Orders. The customer is the company that sent the document.

- On **Purchase Orders** (most common): look for `From`, `Buyer`, `Ordered By`, `Customer`, `Company`, `Purchasing Company`
- On **standard invoices** (rare): `Bill To`, `Ship To`, `Deliver To`, `Sold To`
- On Picking Slips: the delivery address is the customer's address
- General rule: if the document shows a `Vendor:` or `Supplier:` field, that field refers to US — do not extract it as the customer

## SKU / Product Code Rules

The perspective of "Our" and "Your" on a document depends on who issued it:

**On a customer-issued Purchase Order:**
- `Supplier Code`, `Vendor Code`, `Your Code`, `Your Item` → **our** product code → goes in `sku`
- `Our Code`, `Our Item`, `Buyer Code`, `Cust Code` → **their** internal code → goes in `customer_ref`

**On our own documents (picking slips, delivery dockets):**
- `Product Code`, `Code`, `Item No`, `Stock Code` → **our** product code → goes in `sku`
- `Customer Ref`, `Cust Ref`, `Your Code` → **their** code → goes in `customer_ref`

Our supplier codes are typically 4–6 digit numeric (e.g. `15335`, `48245`) or alphanumeric (e.g. `JGKITHB 1007001414`).

## Line Item Rules

- If a document has no prices (e.g. a Picking Slip), set `unit_price` and `line_total` to null — do not invent values.
- UOM examples: `Kg`, `EA`, `ctn`, `box`, `bag`, `L`, `pcs`.

## Math Rules

- `quantity × unit_price = line_total` must hold within $0.02.
- If unsure which number is the unit price vs the line total, the smaller value is usually the unit price.

## Confidence Rules

- `"high"`: value is clearly visible and unambiguous on the document.
- `"medium"`: value was inferred or is partially visible.
- `"low"`: value was guessed or is absent.
