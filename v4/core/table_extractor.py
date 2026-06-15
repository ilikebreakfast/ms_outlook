"""
Tiered table extraction engine.

Tier 1 — pdfplumber text-strategy ($0, digital PDFs)
  Best for machine-generated PDFs with selectable text.  Uses coordinate-based
  column detection rather than line rules, so it handles gridless tables well.

Tier 2 — PaddleOCR PP-Structure ($0, scanned/image PDFs)
  Rasterizes each page with PyMuPDF then runs PP-Structure table recognition,
  which returns an HTML table grid.  Used when tier 1 finds no tables or when
  the math gate rejects tier 1's output.

Tier 3 — Azure Document Intelligence prebuilt-invoice (paid per page)
  Microsoft's purpose-built invoice model.  Only invoked when tiers 1+2 both
  fail the math/confidence gate.  Returns structured cells with bounding boxes.

Gate — math reconciliation (qty × price ≈ total)
  After tiers 1 or 2, the extracted rows are checked: for each row where all
  three of qty/price/total columns are present and parseable, we verify
  qty × price ≈ total within a 1% tolerance.  If the fraction of passing rows
  falls below MATH_GATE_MIN_RATIO, the tier's output is rejected and we escalate.

The LLM (llm_escalation.py) receives the winning CellGrid and labels columns —
it never reads raw PDF geometry.
"""
import io
import logging
from dataclasses import dataclass, field
from typing import Optional

from v4.core.config import MATH_GATE_MIN_RATIO, MIN_LINE_ITEMS_FOR_GATE

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------

@dataclass
class CellGrid:
    """Normalised table extracted from any tier."""
    headers:   list[str]
    rows:      list[list[str]]
    confidence: float           # 0–1 heuristic from the extraction engine
    source:    str              # "pdfplumber" | "paddleocr" | "azure_di"
    tier_used: int              # 1, 2, or 3
    raw_text:  str = ""         # full page text (contextual fallback for LLM)


@dataclass
class TableExtractionResult:
    grids:          list[CellGrid]
    gate_passed:    bool
    math_ok_ratio:  float           # fraction of checkable rows that reconcile
    warnings:       list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tier 1: pdfplumber
# ---------------------------------------------------------------------------

def _extract_pdfplumber(pdf_bytes: bytes) -> list[CellGrid]:
    try:
        import pdfplumber
    except ImportError:
        log.warning("pdfplumber not installed — skipping tier 1")
        return []

    grids: list[CellGrid] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            raw_text = page.extract_text() or ""

            # Text-strategy: infer columns from character x-positions.
            # Best for gridless digital PDFs (most supplier invoices).
            ts = {
                "vertical_strategy": "text",
                "horizontal_strategy": "text",
                "snap_tolerance": 5,
                "intersection_tolerance": 5,
            }
            tables = page.extract_tables(ts)
            if not tables:
                # Fall back to line-strategy for PDFs with ruled borders.
                tables = page.extract_tables()

            for tbl in tables:
                if not tbl or len(tbl) < 2:
                    continue
                headers = [str(c or "").strip() for c in tbl[0]]
                rows    = [[str(c or "").strip() for c in row] for row in tbl[1:]]
                total_cells  = sum(len(r) for r in rows)
                filled_cells = sum(1 for r in rows for c in r if c)
                conf = filled_cells / total_cells if total_cells else 0.0
                grids.append(CellGrid(
                    headers=headers, rows=rows,
                    confidence=conf, source="pdfplumber", tier_used=1,
                    raw_text=raw_text,
                ))
    return grids


# ---------------------------------------------------------------------------
# Tier 2: PaddleOCR PP-Structure
# ---------------------------------------------------------------------------

def _extract_paddleocr(pdf_bytes: bytes) -> list[CellGrid]:
    """Rasterize PDF pages with PyMuPDF, then run PP-Structure table detection."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        log.warning("PyMuPDF not installed — skipping tier 2 (PaddleOCR)")
        return []
    try:
        from paddleocr import PPStructure
    except ImportError:
        log.warning("PaddleOCR not installed — skipping tier 2")
        return []

    import numpy as np
    from PIL import Image

    engine = PPStructure(show_log=False, recovery=True, lang="en")
    grids: list[CellGrid] = []

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    for page in doc:
        mat = fitz.Matrix(2.0, 2.0)          # 144 DPI
        pix = page.get_pixmap(matrix=mat)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

        for region in engine(np.array(img)):
            if region.get("type", "").lower() != "table":
                continue
            html = region.get("res", {}).get("html", "")
            grid = _parse_html_table(html, source="paddleocr")
            if grid:
                grids.append(grid)

    doc.close()
    return grids


def _parse_html_table(html: str, source: str) -> Optional[CellGrid]:
    """Convert an HTML <table> string (from PP-Structure) to a CellGrid."""
    if not html:
        return None
    try:
        from html.parser import HTMLParser

        class _P(HTMLParser):
            def __init__(self):
                super().__init__()
                self.rows: list[list[str]] = []
                self._row: list[str] = []
                self._cell = ""
                self._in = False

            def handle_starttag(self, tag, attrs):
                if tag == "tr":
                    self._row = []
                elif tag in ("td", "th"):
                    self._in = True
                    self._cell = ""

            def handle_endtag(self, tag):
                if tag in ("td", "th"):
                    self._row.append(self._cell.strip())
                    self._in = False
                elif tag == "tr" and self._row:
                    self.rows.append(self._row)

            def handle_data(self, data):
                if self._in:
                    self._cell += data

        p = _P()
        p.feed(html)
        if not p.rows or len(p.rows) < 2:
            return None

        headers = p.rows[0]
        rows    = p.rows[1:]
        total   = sum(len(r) for r in rows)
        filled  = sum(1 for r in rows for c in r if c.strip())
        conf    = filled / total if total else 0.0
        return CellGrid(headers=headers, rows=rows, confidence=conf,
                        source=source, tier_used=2)
    except Exception as e:
        log.debug("HTML table parse error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Tier 3: Azure Document Intelligence
# ---------------------------------------------------------------------------

def _extract_azure_di(pdf_bytes: bytes) -> list[CellGrid]:
    from v4.core.config import AZURE_DI_ENDPOINT, AZURE_DI_KEY
    if not AZURE_DI_ENDPOINT or not AZURE_DI_KEY:
        log.warning("Azure DI not configured — tier 3 unavailable")
        return []

    try:
        from azure.ai.documentintelligence import DocumentIntelligenceClient
        from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
        from azure.core.credentials import AzureKeyCredential
    except ImportError:
        log.warning("azure-ai-documentintelligence not installed — skipping tier 3")
        return []

    client  = DocumentIntelligenceClient(
        endpoint=AZURE_DI_ENDPOINT,
        credential=AzureKeyCredential(AZURE_DI_KEY),
    )
    poller  = client.begin_analyze_document(
        "prebuilt-invoice",
        analyze_request=AnalyzeDocumentRequest(bytes_source=pdf_bytes),
    )
    result  = poller.result()

    grids: list[CellGrid] = []
    for table in (result.tables or []):
        if not table.cells:
            continue
        row_count = max(c.row_index for c in table.cells) + 1
        col_count = max(c.column_index for c in table.cells) + 1
        matrix    = [[""] * col_count for _ in range(row_count)]
        for cell in table.cells:
            matrix[cell.row_index][cell.column_index] = cell.content or ""
        if row_count < 2:
            continue
        grids.append(CellGrid(
            headers=matrix[0], rows=matrix[1:],
            confidence=0.9,      # Azure DI is high-quality; fixed high prior
            source="azure_di", tier_used=3,
        ))
    return grids


# ---------------------------------------------------------------------------
# Math / confidence gate
# ---------------------------------------------------------------------------

_QTY_KW   = {"qty", "quantity", "qnt", "units", "unit"}
_PRICE_KW = {"price", "unit price", "unit_price", "rate", "unit cost"}
_TOTAL_KW = {"total", "amount", "line total", "line_total", "ext", "extended"}


def _col_idx(headers_lower: list[str], kw_set: set[str]) -> Optional[int]:
    for i, h in enumerate(headers_lower):
        if any(k in h for k in kw_set):
            return i
    return None


def _math_gate(grids: list[CellGrid]) -> tuple[bool, float]:
    """
    Returns (gate_passed, ok_ratio).

    For each grid, locate qty/price/total columns by header keywords and verify
    qty × price ≈ total (within 1% or $0.02, whichever is larger).
    If no checkable rows exist (columns not identifiable), the gate passes
    vacuously — we cannot fail what we cannot measure.
    """
    if not grids:
        return False, 0.0

    ok_count = total_count = 0

    for grid in grids:
        hl  = [h.lower().strip() for h in grid.headers]
        qi  = _col_idx(hl, _QTY_KW)
        pi  = _col_idx(hl, _PRICE_KW)
        ti  = _col_idx(hl, _TOTAL_KW)
        if qi is None or pi is None or ti is None:
            continue

        for row in grid.rows:
            if len(row) <= max(qi, pi, ti):
                continue
            try:
                qty   = float(row[qi].replace(",", "").strip())
                price = float(row[pi].replace(",", "").replace("$", "").strip())
                total = float(row[ti].replace(",", "").replace("$", "").strip())
            except (ValueError, AttributeError):
                continue
            total_count += 1
            tol = max(0.02, abs(total) * 0.01)
            if abs(qty * price - total) <= tol:
                ok_count += 1

    if total_count < MIN_LINE_ITEMS_FOR_GATE:
        return True, 1.0    # vacuous pass

    ratio = ok_count / total_count
    return ratio >= MATH_GATE_MIN_RATIO, ratio


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_tables(pdf_bytes: bytes, is_scanned: bool = False) -> TableExtractionResult:
    """
    Run the tiered table extraction pipeline on raw PDF bytes.

    is_scanned=True skips tier 1 (pdfplumber won't find text in a scanned PDF)
    and goes directly to tier 2 (PaddleOCR PP-Structure).
    """
    warnings: list[str] = []

    # --- Tier 1 ---
    if not is_scanned:
        grids = _extract_pdfplumber(pdf_bytes)
        if grids:
            gate_passed, ratio = _math_gate(grids)
            if gate_passed:
                log.info("Tier 1 (pdfplumber) passed gate (math_ratio=%.2f)", ratio)
                return TableExtractionResult(
                    grids=grids, gate_passed=True, math_ok_ratio=ratio,
                )
            warnings.append(f"Tier 1 math gate failed (ratio={ratio:.2f}) — escalating")
            log.info("Tier 1 gate failed — trying tier 2 (PaddleOCR)")
        else:
            warnings.append("Tier 1 produced no tables")

    # --- Tier 2 ---
    grids2 = _extract_paddleocr(pdf_bytes)
    if grids2:
        gate_passed, ratio = _math_gate(grids2)
        if gate_passed:
            log.info("Tier 2 (PaddleOCR) passed gate (math_ratio=%.2f)", ratio)
            return TableExtractionResult(
                grids=grids2, gate_passed=True, math_ok_ratio=ratio, warnings=warnings,
            )
        warnings.append(f"Tier 2 math gate failed (ratio={ratio:.2f}) — escalating to Azure DI")
        log.info("Tier 2 gate failed — escalating to tier 3 (Azure DI)")
    else:
        warnings.append("Tier 2 produced no tables")

    # --- Tier 3 ---
    grids3 = _extract_azure_di(pdf_bytes)
    if grids3:
        gate_passed, ratio = _math_gate(grids3)
        log.info("Tier 3 (Azure DI) result: gate=%s, math_ratio=%.2f", gate_passed, ratio)
        return TableExtractionResult(
            grids=grids3, gate_passed=gate_passed, math_ok_ratio=ratio, warnings=warnings,
        )

    warnings.append("All tiers failed to produce a usable table")
    log.warning("Table extraction failed across all tiers")
    return TableExtractionResult(grids=[], gate_passed=False, math_ok_ratio=0.0, warnings=warnings)
