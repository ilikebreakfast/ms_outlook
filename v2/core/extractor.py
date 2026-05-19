import logging
from pathlib import Path
from typing import Tuple

import pdfplumber
import fitz           # PyMuPDF
import pytesseract
from PIL import Image

from core.settings import RAW_TEXT_DIR, TESSERACT_CMD

log = logging.getLogger(__name__)

# Enforce tesseract command path
pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tiff", ".bmp"}
PDF_EXTENSION = ".pdf"
EXCEL_EXTENSIONS = {".xlsx", ".xls"}
CSV_EXTENSIONS = {".csv"}
MIN_CHARS_PER_PAGE = 50  # Below this threshold indicates scanned PDF


def _ocr_image(img: Image.Image) -> str:
    """Run Tesseract OCR on a PIL Image."""
    img = img.convert("RGB")
    return pytesseract.image_to_string(img, lang="eng")


def _pdf_full_ocr(path: Path) -> str:
    """OCR every page of a PDF via PyMuPDF rendering + Tesseract."""
    doc = fitz.open(str(path))
    pages_text = []
    mat = fitz.Matrix(2.0, 2.0)  # Moderate upscale for OCR accuracy
    for page in doc:
        pix = page.get_pixmap(matrix=mat)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        pages_text.append(_ocr_image(img))
    return "\n\n".join(pages_text)


def _extract_page_text(page) -> str:
    """
    Extract text from a single pdfplumber page.
    Collapses wrapped tabular fields into single tab-separated cells.
    """
    try:
        tables = page.find_tables()
    except Exception:
        tables = []

    if not tables:
        return page.extract_text() or ""

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

    table_lines = []
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
    """Extract native PDF text. Fallback to OCR page-by-page if text is sparse."""
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
                    mat = fitz.Matrix(2.0, 2.0)
                    pix = doc[page.page_number - 1].get_pixmap(matrix=mat)
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    pages_text.append(_ocr_image(img))

        return "\n\n".join(pages_text), not used_ocr

    except Exception as e:
        log.warning(f"pdfplumber failed for {path.name} ({e}), falling back to full OCR.")
        try:
            return _pdf_full_ocr(path), False
        except Exception as e2:
            log.error(f"Full OCR fallback also failed for {path.name}: {e2}")
            raise


def extract_excel_text(path: Path) -> str:
    """Convert spreadsheet to flat tab-separated sheet structures."""
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
    """Convert legacy .xls spreadsheet using xlrd."""
    try:
        import xlrd
    except ImportError:
        raise ImportError("xlrd is required for legacy .xls files: pip install xlrd")
        
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
    Main extraction interface.
    Extracts text, prefixes with filename metadata, and caches it to raw_text/ directory.
    Returns:
        (raw_text, is_native_pdf)
    """
    ext = attachment_path.suffix.lower()

    if ext == PDF_EXTENSION:
        text, is_native = _extract_pdf_native(attachment_path)
    elif ext in IMAGE_EXTENSIONS:
        text = _ocr_image(Image.open(attachment_path))
        is_native = False
    elif ext in EXCEL_EXTENSIONS:
        text = extract_excel_text(attachment_path)
        is_native = True
    elif ext in CSV_EXTENSIONS:
        text = attachment_path.read_text(encoding="utf-8", errors="replace")
        is_native = True
    else:
        raise ValueError(f"Unsupported document format: {ext}")

    # Prepend filename for context
    text = f"[FILENAME: {attachment_path.name}]\n\n{text}"

    # Cache raw text for pipeline operations
    out_dir = RAW_TEXT_DIR / attachment_path.parent.name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (attachment_path.stem + ".txt")
    out_path.write_text(text, encoding="utf-8")
    
    log.info(f"Raw text cached: {out_path} ({'native' if is_native else 'OCR'})")
    return text, is_native
