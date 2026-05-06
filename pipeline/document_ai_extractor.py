"""
Optional pre-built document AI layer for standard invoice field extraction.

Runs BEFORE regex template parsing (and before the Claude reviewer) to
pre-populate standard invoice fields without requiring any custom templates.

Supported providers
-------------------
azure
    Azure Document Intelligence — prebuilt-invoice model.
    Extracts ~20 standard fields (invoice number, date, totals, line items,
    vendor name/address, ABN/tax ID, etc.) with no template authoring.
    Requires:
      pip install azure-ai-documentintelligence
      DOCUMENT_AI_KEY=<your-key>
      DOCUMENT_AI_ENDPOINT=https://<resource>.cognitiveservices.azure.com/

Enable in .env
--------------
    DOCUMENT_AI_ENABLED=true
    DOCUMENT_AI_PROVIDER=azure   # (only option currently)
    DOCUMENT_AI_KEY=...
    DOCUMENT_AI_ENDPOINT=...

Integration with the pipeline
------------------------------
Results are a flat dict in the same shape as template_parser.parse().
In main.py the dict is used to pre-fill fields before template regex runs;
regex matches take priority over document AI values so custom patterns still
override when present.

For senders with no template (extracted_only), document AI results are used
directly, avoiding a Claude reviewer call for standard invoice formats.
"""
import logging
import re
from pathlib import Path
from typing import Optional

from config.settings import (
    DOCUMENT_AI_ENABLED,
    DOCUMENT_AI_PROVIDER,
    DOCUMENT_AI_KEY,
    DOCUMENT_AI_ENDPOINT,
)

log = logging.getLogger(__name__)

# Azure Document Intelligence field name → pipeline field name
_AZURE_FIELD_MAP: dict[str, str] = {
    "VendorName":    "supplier_name",
    "VendorAddress": "vendor_address",
    "VendorTaxId":   "company_abn",
    "CustomerName":  "company_name",
    "CustomerId":    "customer_id",
    "InvoiceId":     "invoice_number",
    "InvoiceDate":   "order_date",
    "DueDate":       "delivery_date",
    "SubTotal":      "subtotal",
    "TotalTax":      "tax_amount",
    "InvoiceTotal":  "total_amount",
    "AmountDue":     "amount_due",
    "PurchaseOrder": "po_number",
    "BillingAddress": "billing_address",
    "ShippingAddress": "shipping_address",
}

_AZURE_LINE_ITEM_MAP: dict[str, str] = {
    "Description": "description",
    "Quantity":    "qty",
    "Unit":        "uom",
    "UnitPrice":   "unit_price",
    "Amount":      "total",
    "ProductCode": "product_code",
    "Tax":         "subtotal",
}

# ABN: 11-digit Australian Business Number
_ABN_DIGITS_RE = re.compile(r"\d{11}")


def extract_document_fields(attachment_path: Path) -> dict:
    """
    Extract standard invoice fields using the configured document AI provider.

    Returns a flat dict (field_name → str value) plus a 'line_items' list.
    Returns {} if the provider is disabled, unavailable, or extraction fails.
    """
    if not DOCUMENT_AI_ENABLED:
        return {}
    provider = (DOCUMENT_AI_PROVIDER or "azure").lower()
    if provider == "azure":
        return _extract_azure(attachment_path)
    log.warning(f"document_ai_extractor: unknown DOCUMENT_AI_PROVIDER={provider!r} — skipping.")
    return {}


def _extract_azure(attachment_path: Path) -> dict:
    try:
        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
        from azure.core.credentials import AzureKeyCredential
    except ImportError:
        log.warning(
            "document_ai_extractor: azure-ai-documentintelligence not installed. "
            "Run: pip install azure-ai-documentintelligence"
        )
        return {}

    if not DOCUMENT_AI_KEY or not DOCUMENT_AI_ENDPOINT:
        log.warning(
            "document_ai_extractor: DOCUMENT_AI_KEY or DOCUMENT_AI_ENDPOINT not set — skipping."
        )
        return {}

    try:
        client = DocumentIntelligenceClient(
            endpoint=DOCUMENT_AI_ENDPOINT,
            credential=AzureKeyCredential(DOCUMENT_AI_KEY),
        )
        with open(attachment_path, "rb") as fh:
            poller = client.begin_analyze_document(
                "prebuilt-invoice",
                analyze_request=fh,
                content_type="application/octet-stream",
            )
        result = poller.result()
    except Exception as exc:
        log.warning(f"document_ai_extractor: Azure analysis failed for {attachment_path.name} — {exc}")
        return {}

    if not result.documents:
        log.debug(f"document_ai_extractor: no documents found in {attachment_path.name}")
        return {}

    doc = result.documents[0]
    fields: dict = {}

    for azure_key, pipeline_key in _AZURE_FIELD_MAP.items():
        field = (doc.fields or {}).get(azure_key)
        if field is None:
            continue
        val = _format_azure_value(field)
        if val:
            fields[pipeline_key] = val

    # Normalise ABN: strip spaces from VendorTaxId if it looks like an 11-digit ABN
    if "company_abn" in fields:
        digits_only = re.sub(r"\D", "", fields["company_abn"])
        if len(digits_only) == 11:
            fields["company_abn"] = digits_only

    # Line items
    items_field = (doc.fields or {}).get("Items")
    if items_field and getattr(items_field, "value", None):
        line_items = []
        for item in items_field.value:
            li: dict = {}
            item_fields = getattr(item, "value", None) or {}
            for azure_key, pipeline_key in _AZURE_LINE_ITEM_MAP.items():
                sub_field = item_fields.get(azure_key)
                if sub_field is None:
                    continue
                val = _format_azure_value(sub_field)
                if val:
                    li[pipeline_key] = val
            if li:
                for k in ("product_code", "description", "qty", "uom", "unit_price", "subtotal", "total"):
                    li.setdefault(k, None)
                line_items.append(li)
        if line_items:
            fields["line_items"] = line_items

    log.info(
        f"document_ai_extractor: {attachment_path.name} — "
        f"extracted {len(fields)} fields via Azure Document Intelligence"
    )
    return fields


def _format_azure_value(field) -> Optional[str]:
    """Convert an Azure DocumentField to a plain string."""
    if field is None:
        return None
    val = getattr(field, "value", None)
    if val is None:
        return None
    # CurrencyValue has .amount
    amount = getattr(val, "amount", None)
    if amount is not None:
        return str(amount)
    # date / datetime have .isoformat()
    isoformat = getattr(val, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    # AddressValue has .street_address, .city, etc. — join non-empty parts
    if hasattr(val, "street_address"):
        parts = [
            getattr(val, "street_address", None),
            getattr(val, "city", None),
            getattr(val, "state", None),
            getattr(val, "postal_code", None),
            getattr(val, "country_region", None),
        ]
        joined = ", ".join(p for p in parts if p)
        return joined or None
    return str(val).strip() or None
