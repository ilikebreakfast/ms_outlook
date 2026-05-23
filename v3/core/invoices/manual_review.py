"""
Human-in-the-loop CLI review layer.

Covers:
  - ASCII panel display (print_payload_summary)
  - Interactive field correction and accept/reject loop (review_payload)
  - Batch pending review loader (review_all_pending)
  - Deserialization helper (dict_to_payload)
"""

import json
from dataclasses import asdict
from pathlib import Path

from core.invoices.extractor import (
    InvoicePayload, CustomerInfo, FieldValue, LineItem, InvoiceTotals,
    save_template, save_item_codes,
)

from core.config import V3_ROOT, DATA_DIR

_V3_ROOT = V3_ROOT
_DATA_DIR = DATA_DIR


def _update_prompt_md() -> None:
    """Regenerate PROMPT.md after a confirmed invoice (best-effort, silent on error)."""
    try:
        from core.invoices.knowledge_base import generate_prompt_md
        generate_prompt_md()
    except Exception as e:
        print(f"[knowledge_base] PROMPT.md update skipped: {e}")

# ==============================================================================
# DISPLAY
# ==============================================================================

def print_payload_summary(payload: InvoicePayload):
    """Print a formatted ASCII summary panel for the parsed invoice."""
    print("\n" + "=" * 78)
    print(" CUSTOMER DETAILS".center(78))
    print("-" * 78)
    print(f"  Name    : {str(payload.customer.name.value).ljust(35)} [Conf: {payload.customer.name.confidence.upper()}]")
    print(f"  Email   : {str(payload.customer.email.value).ljust(35)} [Conf: {payload.customer.email.confidence.upper()}]")
    print(f"  Phone   : {str(payload.customer.phone.value).ljust(35)} [Conf: {payload.customer.phone.confidence.upper()}]")
    print(f"  ABN     : {str(payload.customer.abn.value).ljust(35)} [Conf: {payload.customer.abn.confidence.upper()}]")
    print(f"  Address : {str(payload.customer.address.value).ljust(35)} [Conf: {payload.customer.address.confidence.upper()}]")
    print("=" * 78)

    print(" LINE ITEMS".center(78))
    print("-" * 78)
    print(f"{'#'.ljust(3)} {'SKU'.ljust(12)} {'Description'.ljust(25)} {'Qty'.rjust(6)} {'UOM'.ljust(4)} {'Price'.rjust(10)} {'Total'.rjust(10)}")
    print("-" * 78)
    for item in payload.line_items:
        line_num  = str(item.line_number).ljust(3)
        verified_mark = "✓" if item.sku_verified else ("?" if item.sku else " ")
        sku_raw   = (item.sku or "N/A")
        sku       = (f"{sku_raw}{verified_mark}")[:12].ljust(12)
        desc      = (item.description or "")[:25].ljust(25)

        if item.quantity is not None:
            qty = f"{item.quantity:.2f}".rjust(6)
        elif item.inferred_quantity is not None:
            qty = f"~{item.inferred_quantity:.2f}".rjust(6)
        else:
            qty = "N/A".rjust(6)

        uom = (item.uom or "")[:4].ljust(4)

        if item.unit_price is not None:
            price_tag = "" if item.math_ok else "!"
            price = f"${item.unit_price:.2f}{price_tag}".rjust(10)
        elif item.inferred_unit_price is not None:
            price = f"~${item.inferred_unit_price:.2f}".rjust(10)
        else:
            price = "N/A".rjust(10)

        if item.line_total is not None:
            total_tag = "" if item.math_ok else "!"
            total = f"${item.line_total:.2f}{total_tag}".rjust(10)
        elif item.inferred_line_total is not None:
            total = f"~${item.inferred_line_total:.2f}".rjust(10)
        else:
            total = "N/A".rjust(10)

        print(f"{line_num} {sku} {desc} {qty} {uom} {price} {total}")
        if item.customer_ref:
            print(f"    {'':12} Cust ref: {item.customer_ref}")
    print("-" * 78)

    has_inferred = any(
        item.inferred_unit_price is not None or item.inferred_quantity is not None
        or item.inferred_line_total is not None
        for item in payload.line_items
    )
    has_mismatch  = any(not item.math_ok for item in payload.line_items)
    has_verified  = any(item.sku_verified for item in payload.line_items)
    has_unverified = any(item.sku and not item.sku_verified for item in payload.line_items)
    if has_verified or has_unverified:
        print("  SKU legend: ✓ = matched ERP code   ? = not in ERP list (check if customer ref)")
    if has_inferred:
        print("  ~ = inferred value (not on source document)")
    if has_mismatch:
        print("  ! = math mismatch (qty × price ≠ total)")

    sub = f"${payload.totals.subtotal:.2f}" if payload.totals.subtotal is not None else "N/A"
    tax = f"${payload.totals.tax:.2f}" if payload.totals.tax is not None else "N/A"
    tot = f"${payload.totals.total:.2f}" if payload.totals.total is not None else "N/A"
    print(f"{'Subtotal:'.rjust(50)} {sub.rjust(10)}")
    print(f"{'Tax (10%):'.rjust(50)} {tax.rjust(10)}")
    print(f"{'Total:'.rjust(50)} {tot.rjust(10)}")
    print("=" * 78 + "\n")

# ==============================================================================
# INTERACTIVE REVIEW
# ==============================================================================

def review_payload(payload: InvoicePayload) -> InvoicePayload:
    """Prompt the operator to correct fields and accept, re-edit, or reject the invoice."""
    source_file_name = Path(payload.source_file).name if payload.source_file else "Unknown"

    while True:
        print("\n" + "=" * 78)
        print(f" INVOICE REVIEW: {source_file_name} ".center(78, "="))
        print(f" Confidence: {payload.parse_confidence.upper()} | Needs Review: {payload.needs_review}")
        if payload.warnings:
            print(" Warnings:")
            for w in payload.warnings:
                print(f"   - {w}")
        print("=" * 78)

        # Fall back to all low-confidence / empty fields if nothing was flagged
        review_fields = list(payload.review_fields)
        if not review_fields:
            for field_name in ["name", "email", "phone", "abn", "address"]:
                f_val = getattr(payload.customer, field_name)
                if f_val.confidence == "low" or f_val.value is None:
                    review_fields.append(field_name)

        for field_name in review_fields:
            if field_name == "line_items":
                print("\n[line_items] Flagged for verification.")
                continue
            field_obj = getattr(payload.customer, field_name, None)
            if not field_obj:
                continue
            current_value = field_obj.value if field_obj.value is not None else "NULL"
            print(f"\n[{field_name.upper()}]")
            print(f"  Extracted value : \"{current_value}\"  (confidence: {field_obj.confidence.upper()})")
            correction = input("  Enter correction (or press ENTER to accept): ").strip()
            if correction:
                field_obj.value = correction
                field_obj.confidence = "high"
                field_obj.source = "human"
                print(f"  -> Updated to: \"{correction}\"")

        print_payload_summary(payload)
        decision = input("Accept this invoice? [y/n/reject]: ").strip().lower()

        if decision == 'y':
            payload.needs_review = False
            payload.parse_confidence = "high"

            approved_dir = _DATA_DIR / "invoice_staging" / "approved"
            approved_dir.mkdir(parents=True, exist_ok=True)

            old_path = Path(payload.staging_path)
            new_path = approved_dir / old_path.name
            payload.staging_path = str(new_path.resolve())
            new_path.write_text(json.dumps(asdict(payload), indent=2), encoding="utf-8")
            if old_path.exists() and old_path != new_path:
                old_path.unlink()

            print(f"Staging approved: {payload.staging_path}")
            key = (payload.customer.email.value or payload.customer.name.value or "").lower().strip()
            if key:
                save_template(key, payload.customer)
            if payload.line_items:
                save_item_codes(payload.line_items, source="llm")
            _update_prompt_md()
            return payload

        elif decision == 'reject':
            payload.needs_review = False
            payload.parse_confidence = "rejected"

            rejected_dir = _DATA_DIR / "invoice_staging" / "rejected"
            rejected_dir.mkdir(parents=True, exist_ok=True)

            old_path = Path(payload.staging_path)
            new_path = rejected_dir / old_path.name
            payload.staging_path = str(new_path.resolve())
            new_path.write_text(json.dumps(asdict(payload), indent=2), encoding="utf-8")
            if old_path.exists() and old_path != new_path:
                old_path.unlink()

            print(f"Staging rejected: {payload.staging_path}")
            return payload

        elif decision == 'n':
            print("Restarting field edits...")
            continue

# ==============================================================================
# DESERIALIZATION & BATCH LOADER
# ==============================================================================

def dict_to_payload(d: dict) -> InvoicePayload:
    """Reconstruct an InvoicePayload from a deserialized staging JSON dict."""
    cust_d = d.get("customer", {}) or {}
    customer = CustomerInfo()
    for field_name in ["name", "email", "phone", "abn", "address"]:
        f_val = cust_d.get(field_name, {}) or {}
        setattr(customer, field_name, FieldValue(
            value=f_val.get("value"),
            confidence=f_val.get("confidence", "low"),
            source=f_val.get("source", "llm"),
        ))

    line_items = []
    for item in d.get("line_items", []) or []:
        line_items.append(LineItem(
            line_number=item.get("line_number", 0),
            sku=item.get("sku"),
            customer_ref=item.get("customer_ref"),
            description=item.get("description", ""),
            quantity=item.get("quantity"),
            uom=item.get("uom"),
            unit_price=item.get("unit_price"),
            line_total=item.get("line_total"),
            confidence=item.get("confidence", "high"),
            sku_verified=item.get("sku_verified", False),
            inferred_unit_price=item.get("inferred_unit_price"),
            inferred_quantity=item.get("inferred_quantity"),
            inferred_line_total=item.get("inferred_line_total"),
            math_ok=item.get("math_ok", True),
        ))

    totals_d = d.get("totals", {}) or {}
    return InvoicePayload(
        source_file=d.get("source_file", ""),
        source_type=d.get("source_type", ""),
        customer=customer,
        line_items=line_items,
        totals=InvoiceTotals(
            subtotal=totals_d.get("subtotal"),
            tax=totals_d.get("tax"),
            total=totals_d.get("total"),
        ),
        parse_confidence=d.get("parse_confidence", "low"),
        warnings=d.get("warnings", []) or [],
        needs_review=d.get("needs_review", False),
        review_fields=d.get("review_fields", []) or [],
        staging_path=d.get("staging_path", ""),
    )

def review_all_pending(staging_base: str = "invoice_staging") -> None:
    """Load and interactively review all JSON files in the pending/ directory."""
    pending_dir = _DATA_DIR / staging_base / "pending"
    if not pending_dir.exists():
        print(f"No pending reviews directory found at: {pending_dir}")
        return

    pending_files = list(pending_dir.glob("*.json"))
    if not pending_files:
        print("No pending invoices found requiring manual review.")
        return

    print(f"Found {len(pending_files)} pending invoice(s) for review.")
    for file in pending_files:
        print(f"\nLoading invoice from pending queue: {file.name}")
        try:
            raw_data = json.loads(file.read_text(encoding="utf-8"))
            payload = dict_to_payload(raw_data)
            payload.staging_path = str(file.resolve())
            review_payload(payload)
        except Exception as e:
            print(f"Error loading {file.name}: {e}")
