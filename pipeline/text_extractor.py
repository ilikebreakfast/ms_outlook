"""
Extracts raw text from PDF and image files.

Strategy:
  1. Try pdfplumber (native PDF text layer).
  2. If extracted text is too short (<50 chars per page), assume scanned.
  3. Fall back to PyMuPDF -> render page as image -> Tesseract OCR.
  4. For image files (.png/.jpg/etc), go straight to Tesseract.

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
MIN_CHARS_PER_PAGE = 50  # below this = likely scanned


def _ocr_image(img: Image.Image) -> str:
    # Tesseract only accepts RGB or greyscale — convert CMYK, P, RGBA, etc.
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    return pytesseract.image_to_string(img, lang="eng")


def _extract_pdf_native(path: Path) -> Tuple[str, bool]:
    """Returns (text, is_native). is_native=False means OCR fallback used."""
    pages_text = []
    used_ocr = False

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
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
    else:
        raise ValueError(f"Unsupported file type: {ext}")

    # Save raw text for audit
    out_dir = RAW_TEXT_DIR / attachment_path.parent.name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (attachment_path.stem + ".txt")
    out_path.write_text(text, encoding="utf-8")
    log.info(f"Raw text saved: {out_path} ({'native' if is_native else 'OCR'})")

    return text, is_native
