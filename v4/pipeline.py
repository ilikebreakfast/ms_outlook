"""
v4 pipeline orchestrator.

Import order matters here: v3's internal modules use bare `from core.X import`
which resolves against sys.path.  We add v3/ to sys.path BEFORE importing v3
modules, then add v4/ so v4's own `from v4.core.X import` works via the repo
root.  Both root entries are added before any domain imports run.
"""
import json
import logging
import sys
from pathlib import Path

# --- sys.path setup (must precede all domain imports) ---
_V4_ROOT   = Path(__file__).resolve().parent           # ms_outlook/v4/
_REPO_ROOT = _V4_ROOT.parent                           # ms_outlook/
_V3_ROOT   = _REPO_ROOT / "v3"

for _p in (str(_V3_ROOT), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# --- v3 imports (while v3/ is at the front so its bare `core.*` resolves) ---
from core.invoices.extractor import (           # noqa: E402  (v3/core/invoices/extractor.py)
    InvoicePayload,
    CustomerInfo,
    LineItem,
    InvoiceTotals,
    FieldValue,
    validate_and_stage,
)

# --- v4 imports ---
from v4.core.intake import run_poll, mark_processed, AttachmentBlob    # noqa: E402
from v4.core.table_extractor import (                                   # noqa: E402
    extract_tables,
    CellGrid,
    TableExtractionResult,
)
from v4.core.llm_escalation import extract_from_grid                   # noqa: E402

log = logging.getLogger(__name__)

_SUPPORTED_EXTS = {".pdf"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _grid_to_json(grids: list[CellGrid]) -> str:
    """Serialise CellGrids to the compact JSON format sent to the LLM."""
    tables = []
    for g in grids:
        tables.append({
            "source":  g.source,
            "headers": g.headers,
            "rows":    g.rows[:60],   # cap rows to avoid excessive token usage
        })
    return json.dumps(tables, ensure_ascii=False)


def _map_llm_response(llm: dict, blob: AttachmentBlob) -> InvoicePayload:
    """Map the LLM JSON response dict to v3 InvoicePayload dataclasses."""

    def fv(raw: dict) -> FieldValue:
        return FieldValue(
            value=raw.get("value"),
            confidence=raw.get("confidence", "low"),
            source="llm",
        )

    cust_raw = llm.get("customer", {})
    customer = CustomerInfo(
        name    = fv(cust_raw.get("name",    {})),
        email   = fv(cust_raw.get("email",   {})),
        phone   = fv(cust_raw.get("phone",   {})),
        abn     = fv(cust_raw.get("abn",     {})),
        address = fv(cust_raw.get("address", {})),
    )

    line_items: list[LineItem] = []
    for i, raw in enumerate(llm.get("line_items", []), start=1):
        line_items.append(LineItem(
            line_number  = raw.get("line_number", i),
            sku          = raw.get("sku"),
            customer_ref = raw.get("customer_ref"),
            description  = raw.get("description", ""),
            quantity     = raw.get("quantity"),
            uom          = raw.get("uom"),
            unit_price   = raw.get("unit_price"),
            line_total   = raw.get("line_total"),
            confidence   = raw.get("confidence", "low"),
        ))

    tot_raw = llm.get("totals", {})
    totals  = InvoiceTotals(
        subtotal = tot_raw.get("subtotal"),
        tax      = tot_raw.get("tax"),
        total    = tot_raw.get("total"),
    )

    return InvoicePayload(
        source_file  = blob.filename,
        source_type  = "pdf_text",
        customer     = customer,
        line_items   = line_items,
        totals       = totals,
    )


# ---------------------------------------------------------------------------
# Per-attachment processing
# ---------------------------------------------------------------------------

def process_attachment(blob: AttachmentBlob) -> bool:
    """
    Run the full extraction pipeline on one attachment.
    Returns True on success (even if staged with needs_review=True).
    """
    ext = Path(blob.filename).suffix.lower()
    if ext not in _SUPPORTED_EXTS:
        log.warning("Unsupported type %s (%s) — skipping", ext, blob.filename)
        mark_processed(blob.internet_message_id, status="unsupported_type")
        return False

    # --- Tiered table extraction ---
    extraction: TableExtractionResult = extract_tables(blob.content_bytes)

    for w in extraction.warnings:
        log.info("[%s] %s", blob.filename, w)

    if extraction.grids:
        grid_json  = _grid_to_json(extraction.grids)
        # Determine source type from the winning tier
        source_type = (
            "pdf_text"    if extraction.grids[0].source == "pdfplumber" else
            "pdf_scanned" if extraction.grids[0].source == "paddleocr"  else
            "pdf_mixed"
        )
    else:
        # No grid at all — send empty context; LLM will return low-confidence output
        grid_json   = json.dumps([{"source": "none", "headers": [], "rows": []}])
        source_type = "pdf_text"
        log.warning("No tables extracted from %s — LLM will work without grid", blob.filename)

    # --- LLM extraction (column labelling + field extraction) ---
    llm_result = extract_from_grid(
        grid_json    = grid_json,
        sender_email = blob.sender,
    )
    if not llm_result:
        log.error("LLM returned empty result for %s", blob.filename)
        mark_processed(blob.internet_message_id, status="llm_failed")
        return False

    # --- Map to v3 payload ---
    payload = _map_llm_response(llm_result, blob)
    payload.source_type = source_type

    # --- Validate and stage (v3 reuse) ---
    try:
        staged = validate_and_stage(payload)
        log.info(
            "Staged %s — confidence=%s, needs_review=%s, items=%d",
            blob.filename, staged.parse_confidence, staged.needs_review,
            len(staged.line_items),
        )
        mark_processed(blob.internet_message_id, status="staged")
        return True
    except Exception as e:
        log.error("validate_and_stage failed for %s: %s", blob.filename, e)
        mark_processed(blob.internet_message_id, status="stage_failed")
        return False


# ---------------------------------------------------------------------------
# Public: one full poll-and-process cycle
# ---------------------------------------------------------------------------

def run_pipeline() -> dict:
    """
    Execute one complete poll → extract → stage cycle.
    Returns a summary dict suitable for logging or webhook alerting.
    """
    poll = run_poll()

    if poll.first_run and not poll.attachments:
        log.info("First-run snapshot complete — inbox baseline established")
        return {
            "first_run": True,
            "new_messages": 0,
            "attachments_found": 0,
            "processed": 0,
            "failed": 0,
        }

    log.info(
        "Processing %d attachment(s) from %d new message(s)",
        len(poll.attachments), poll.new_messages,
    )

    processed = failed = 0
    for blob in poll.attachments:
        try:
            if process_attachment(blob):
                processed += 1
            else:
                failed += 1
        except Exception:
            log.exception("Unhandled error for %s", blob.filename)
            failed += 1

    return {
        "first_run":         poll.first_run,
        "new_messages":      poll.new_messages,
        "attachments_found": len(poll.attachments),
        "processed":         processed,
        "failed":            failed,
        "skipped_duplicate": poll.skipped_duplicate,
    }
