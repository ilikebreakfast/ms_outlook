"""
Extracts raw text from PDF and image files.

Strategy:
  1. Try pdfplumber (native PDF text layer).
  2. If extracted text is too short (<50 chars per page), assume scanned.
  3. Fall back to PyMuPDF -> render page as image -> Tesseract OCR.
  4. For image files (.png/.jpg/etc), go straight to Tesseract.
  5. For Excel files (.xlsx, .xls), flatten to tab-separated text.

Saves raw text to raw_text/ for audit.
"""
import logging
from pathlib import Path
from typing import Tuple

import pdfplumber
import fitz           # PyMuPDF
import pytesseract
from PIL import Image

from config.settings import (
    RAW_TEXT_DIR, TESSERACT_CMD, ATTACHMENT_EXTENSIONS
)

log = logging.getLogger(__name__)
pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tiff", ".bmp"}
PDF_EXTENSION = ".pdf"
EXCEL_EXTENSIONS = {".xlsx", ".xls"}
CSV_EXTENSIONS = {".csv"}
MIN_CHARS_PER_PAGE = 50  # below this = likely scanned


def _ocr_image(img: Image.Image) -> str:
    # Force full decode and normalise to RGB — Image.open() is lazy and
    # some JPEGs (CMYK, YCbCr, unusual encodings) crash pytesseract otherwise.
    img = img.convert("RGB")
    return pytesseract.image_to_string(img, lang="eng")


def _pdf_full_ocr(path: Path) -> str:
    """OCR every page of a PDF via PyMuPDF. Used when pdfplumber/pdfminer fails."""
    doc = fitz.open(str(path))
    pages_text = []
    mat = fitz.Matrix(2.0, 2.0)
    for page in doc:
        pix = page.get_pixmap(matrix=mat)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        pages_text.append(_ocr_image(img))
    return "\n\n".join(pages_text)


def _extract_page_text(page) -> str:
    """
    Extract text from a single pdfplumber page with table-aware cell handling.

    When a page contains tables, renders each table as tab-separated rows so
    that cells whose text wraps across multiple PDF lines (e.g. a UOM like
    "CTN (36 x\\n140g)") are collapsed to a single value rather than split
    across lines straddling adjacent rows. Non-table text (headers, totals) is
    extracted normally via extract_text() on the region outside the table bbox.
    """
    try:
        tables = page.find_tables()
    except Exception:
        tables = []

    if not tables:
        return page.extract_text() or ""

    # Non-table regions: crop away each table bbox and extract surrounding text
    non_table_page = page
    for tbl in tables:
        try:
            non_table_page = non_table_page.outside_bbox(tbl.bbox)
        except Exception:
            pass
    try:
        surrounding = non_table_page.extract_text() or ""
    except Exception:
        surrounding = ""

    # Table regions: render as tab-separated rows, collapsing wrapped cell text
    table_lines: list[str] = []
    for tbl in tables:
        try:
            rows = tbl.extract()
        except Exception:
            continue
        for row in (rows or []):
            if not row:
                continue
            cells = [(c or "").replace("\n", " ").strip() for c in row]
            line = "\t".join(cells).rstrip()
            if any(cells):
                table_lines.append(line)

    parts = [p for p in (surrounding, "\n".join(table_lines)) if p.strip()]
    return "\n".join(parts)


def _extract_pdf_native(path: Path) -> Tuple[str, bool]:
    """Returns (text, is_native). is_native=False means OCR fallback used."""
    try:
        pages_text = []
        used_ocr = False

        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                text = _extract_page_text(page)
                if len(text.strip()) >= MIN_CHARS_PER_PAGE:
                    pages_text.append(text)
                else:
                    log.debug(f"Page {page.page_number} sparse, using OCR.")
                    used_ocr = True
                    # Render via PyMuPDF for better OCR quality
                    doc = fitz.open(str(path))
                    mat = fitz.Matrix(2.0, 2.0)  # 2x scale = higher DPI
                    pix = doc[page.page_number - 1].get_pixmap(matrix=mat)
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    pages_text.append(_ocr_image(img))

        return "\n\n".join(pages_text), not used_ocr

    except Exception as e:
        # pdfminer / pdfplumber can raise on malformed or encrypted PDFs.
        # Fall back to full OCR via PyMuPDF which is more tolerant.
        log.warning(f"pdfplumber failed for {path.name} ({e}), falling back to full OCR.")
        try:
            return _pdf_full_ocr(path), False
        except Exception as e2:
            log.error(f"OCR fallback also failed for {path.name}: {e2}")
            raise


def extract_excel_text(path: Path) -> str:
    """
    Convert an Excel workbook to a flat text representation for classification
    and template field matching. Each sheet is rendered as tab-separated rows.
    Supports both .xlsx (openpyxl) and legacy .xls (xlrd).
    """
    ext = path.suffix.lower()
    if ext == ".xls":
        return _extract_xls_text(path)
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    parts = []
    for sheet in wb.worksheets:
        parts.append(f"[Sheet: {sheet.title}]")
        for row in sheet.iter_rows(values_only=True):
            cells = [str(c) if c is not None else "" for c in row]
            line = "\t".join(cells).rstrip()
            if line:
                parts.append(line)
    return "\n".join(parts)


def _extract_xls_text(path: Path) -> str:
    """Flatten a legacy .xls workbook to tab-separated text using xlrd."""
    try:
        import xlrd
    except ImportError:
        raise ImportError("xlrd is required for .xls files: pip install xlrd")
    wb = xlrd.open_workbook(str(path))
    parts = []
    for sheet in wb.sheets():
        parts.append(f"[Sheet: {sheet.name}]")
        for row_idx in range(sheet.nrows):
            cells = [str(sheet.cell_value(row_idx, col)) for col in range(sheet.ncols)]
            line = "\t".join(cells).rstrip()
            if line:
                parts.append(line)
    return "\n".join(parts)


def extract_text(attachment_path: Path) -> Tuple[str, bool]:
    """
    Main entry point. Returns (raw_text, is_native_pdf).
    Saves result to raw_text/ automatically.
    """
    ext = attachment_path.suffix.lower()

    if ext == PDF_EXTENSION:
        text, is_native = _extract_pdf_native(attachment_path)
    elif ext in IMAGE_EXTENSIONS:
        text = _ocr_image(Image.open(attachment_path))
        is_native = False
    elif ext in EXCEL_EXTENSIONS:
        text = extract_excel_text(attachment_path)
        is_native = True  # no OCR involved
    elif ext in CSV_EXTENSIONS:
        text = attachment_path.read_text(encoding="utf-8", errors="replace")
        is_native = True
    else:
        raise ValueError(f"Unsupported file type: {ext}")

    # Prepend filename so regex patterns can match dates/info embedded in the filename
    text = f"[FILENAME: {attachment_path.name}]\n\n{text}"

    # Save raw text for audit
    out_dir = RAW_TEXT_DIR / attachment_path.parent.name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (attachment_path.stem + ".txt")
    out_path.write_text(text, encoding="utf-8")
    log.info(f"Raw text saved: {out_path} ({'native' if is_native else 'OCR'})")

    return text, is_native
