"""
v4 pipeline orchestrator.

Import order matters here: v3's internal modules use bare `from core.X import`
which resolves against sys.path.  We add v3/ to sys.path BEFORE importing v3
modules, then the repo root so `from v4.core.X import` resolves via the
package namespace.  Both insertions must happen before any domain imports.
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
from core.invoices.extractor import (           # noqa: E402
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
    """
    Serialise CellGrids to the compact JSON format sent to the LLM.
    Cell values are included as-is; injection scrubbing is applied inside
    llm_escalation._build_user_content() before they reach the prompt.
    """
    tables = []
    for g in grids:
        tables.append({
            "source":  g.source,
            "headers": g.headers,
            "rows":    g.rows[:60],   # cap rows to avoid excessive token use
        })
    return json.dumps(tables, ensure_ascii=False)


def _source_type(grids: list[CellGrid]) -> str:
    """Derive source_type from the winning extraction tier."""
    if not grids:
        return "pdf_mixed"   # unknown — extraction failed
    src = grids[0].source
    if src == "pdfplumber":
        return "pdf_text"
    if src == "paddleocr":
        return "pdf_scanned"
    return "pdf_mixed"       # azure_di or other


def _map_llm_response(llm: dict, blob: AttachmentBlob, source_type: str) -> InvoicePayload:
    """Map the LLM JSON response dict to v3 InvoicePayload dataclasses."""

    def fv(raw: dict) -> FieldValue:
        return FieldValue(
            value=raw.get("value"),
            # Use the LLM's stated confidence; default to "high" so documents
            # where the model omits confidence (confident outputs) don't
            # spuriously get demoted to medium by validate_and_stage.
            confidence=raw.get("confidence", "high"),
            source="llm",
        )

    cust_raw  = llm.get("customer", {})
    llm_email = cust_raw.get("email", {}).get("value")

    customer = CustomerInfo(
        name    = fv(cust_raw.get("name",    {})),
        # Prefer the LLM-extracted email; fall back to the envelope sender so
        # validate_and_stage can find the customer's trusted template by email.
        email   = FieldValue(
            value      = llm_email or blob.sender,
            confidence = cust_raw.get("email", {}).get("confidence", "medium")
                         if llm_email else "medium",
            source     = "llm" if llm_email else "email",
        ),
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
            confidence   = raw.get("confidence", "high"),
        ))

    tot_raw = llm.get("totals", {})
    totals  = InvoiceTotals(
        subtotal = tot_raw.get("subtotal"),
        tax      = tot_raw.get("tax"),
        total    = tot_raw.get("total"),
    )

    return InvoicePayload(
        source_file = blob.filename,
        source_type = source_type,
        customer    = customer,
        line_items  = line_items,
        totals      = totals,
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

    stype = _source_type(extraction.grids)

    if extraction.grids:
        grid_json = _grid_to_json(extraction.grids)
    else:
        # No grid — send minimal context; LLM will produce low-confidence output
        grid_json = json.dumps([{"source": "none", "headers": [], "rows": []}])
        log.warning("[%s] No tables extracted — LLM will work without a grid", blob.filename)

    # --- LLM extraction (column labelling + field extraction) ---
    llm_result = extract_from_grid(
        grid_json    = grid_json,
        sender_email = blob.sender,
    )
    if not llm_result:
        log.error("[%s] LLM returned empty result", blob.filename)
        mark_processed(blob.internet_message_id, status="llm_failed")
        return False

    # --- Map to v3 payload ---
    payload = _map_llm_response(llm_result, blob, stype)

    # --- Validate and stage (v3 reuse) ---
    try:
        staged = validate_and_stage(payload)
        log.info(
            "[%s] Staged — tier=%s, confidence=%s, items=%d, needs_review=%s",
            blob.filename, stype, staged.parse_confidence,
            len(staged.line_items), staged.needs_review,
        )
        mark_processed(blob.internet_message_id, status="staged")
        return True
    except Exception as e:
        log.error("[%s] validate_and_stage failed: %s", blob.filename, e)
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
        log.info(
            "First-run snapshot complete — inbox baseline established.\n"
            "  New messages will be processed on the next poll.\n"
            "  Run with FIRST_RUN_PROCESS=1 to process the existing inbox on first run."
        )
        return {
            "first_run":           True,
            "new_messages":        0,
            "attachments_found":   0,
            "processed":           0,
            "failed":              0,
            "skipped_blocked":     poll.skipped_blocked,
        }

    if not poll.attachments:
        log.info("No new attachments to process.")
        return {
            "first_run":           poll.first_run,
            "new_messages":        poll.new_messages,
            "attachments_found":   0,
            "processed":           0,
            "failed":              0,
            "skipped_blocked":     poll.skipped_blocked,
        }

    log.info(
        "Processing %d attachment(s) from %d new message(s)…",
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
            log.exception("Unhandled error processing %s", blob.filename)
            failed += 1

    log.info(
        "Cycle complete — processed=%d, failed=%d, blocked=%d",
        processed, failed, poll.skipped_blocked,
    )

    return {
        "first_run":           poll.first_run,
        "new_messages":        poll.new_messages,
        "attachments_found":   len(poll.attachments),
        "processed":           processed,
        "failed":              failed,
        "skipped_duplicate":   poll.skipped_duplicate,
        "skipped_blocked":     poll.skipped_blocked,
    }
