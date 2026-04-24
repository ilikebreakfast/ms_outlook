"""
Claude-based fallback reviewer for invoice field extraction.

Invoked when regex template parsing confidence is below threshold,
or when no template exists for a known sender (extracted_only status).

Strategy:
  1. Always pass extracted text to Claude (cheap, text tokens only).
  2. If text extraction was via OCR (is_native=False) and text is sparse
     (<500 chars) and the file is a PDF, additionally render up to 3 pages
     as base64 PNG images for vision input.
  3. Use claude-haiku for cost efficiency — only invoked as a fallback.
  4. System prompt is cached to amortise the cost across repeated calls.
"""
import base64
import json
import logging
from pathlib import Path
from typing import Optional

import anthropic

from config.settings import ANTHROPIC_API_KEY, CLAUDE_REVIEW_ENABLED, TEMPLATES_DIR

log = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"
_VISION_TEXT_THRESHOLD = 500   # chars — below this, add page images if available
_MAX_PDF_PAGES_FOR_VISION = 3
_MAX_TEXT_CHARS = 4000          # truncate long extractions to control token cost

_SYSTEM_PROMPT = (
    "You are a precise invoice data extractor. "
    "Given document content (text and/or page images), extract structured fields "
    "and return ONLY a valid JSON object — no explanation, no markdown fences. "
    "Omit any key you cannot find. "
    "For line_items return a list of objects with keys: "
    "product_code, description, qty, uom, unit_price, subtotal, total. "
    "Use null for missing line-item sub-fields."
)


def _render_pdf_page_images(pdf_path: Path, max_pages: int) -> list[dict]:
    """Render PDF pages as base64 PNG blocks for the Claude vision API."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(str(pdf_path))
        blocks = []
        mat = fitz.Matrix(1.5, 1.5)  # moderate DPI — quality vs token cost balance
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            pix = page.get_pixmap(matrix=mat)
            png_bytes = pix.tobytes("png")
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.standard_b64encode(png_bytes).decode(),
                },
            })
        return blocks
    except Exception as exc:
        log.warning(f"Failed to render PDF pages for vision input: {exc}")
        return []


def _load_template_fields(template_name: str) -> list[str]:
    """Return the list of expected field names from a YAML template."""
    try:
        import yaml
        tmpl_path = TEMPLATES_DIR / f"{template_name}.yaml"
        if not tmpl_path.exists():
            return []
        tmpl = yaml.safe_load(tmpl_path.read_text(encoding="utf-8"))
        return list(tmpl.get("fields", {}).keys())
    except Exception:
        return []


def _build_user_content(
    text: str,
    template_fields: list[str],
    existing_fields: dict,
    attachment_path: Path,
    is_native: bool,
) -> list[dict]:
    """Assemble the user message content blocks."""
    content: list[dict] = []

    # Vision: add page images when OCR text is too sparse to be reliable
    use_vision = (
        not is_native
        and attachment_path.suffix.lower() == ".pdf"
        and len(text.strip()) < _VISION_TEXT_THRESHOLD
    )
    if use_vision:
        images = _render_pdf_page_images(attachment_path, _MAX_PDF_PAGES_FOR_VISION)
        content.extend(images)
        if images:
            log.debug(
                f"Claude reviewer: added {len(images)} page image(s) "
                f"for sparse OCR text ({len(text.strip())} chars)."
            )

    # Build extraction prompt
    fields_hint = ""
    if template_fields:
        fields_hint = f"\nExtract these specific fields: {', '.join(template_fields)}."

    already_found = {
        k: v
        for k, v in (existing_fields or {}).items()
        if v and not k.startswith("_") and k != "line_items"
    }
    existing_hint = ""
    if already_found:
        existing_hint = f"\nAlready extracted by regex: {already_found}. Fill in the missing fields."

    prompt = (
        f"Extract invoice/document fields and return ONLY a JSON object.{fields_hint}{existing_hint}\n\n"
        "Common fields to look for (include only those present): "
        "invoice_number, order_date, abn, amount_due, total_amount, subtotal, "
        "tax_amount, supplier_name, customer_name, po_number, delivery_date, address.\n\n"
        f"Document text:\n{text[:_MAX_TEXT_CHARS]}"
    )
    content.append({"type": "text", "text": prompt})
    return content


def _parse_response_json(raw: str) -> Optional[dict]:
    """Strip markdown fences and parse JSON from Claude's response."""
    text = raw.strip()
    # Strip ```json ... ``` or ``` ... ``` fences
    if text.startswith("```"):
        lines = text.splitlines()
        # Drop first line (``` or ```json) and last ``` line
        inner = "\n".join(
            line for line in lines[1:]
            if line.strip() != "```"
        )
        text = inner.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        log.warning(f"Claude reviewer: JSON parse failed — {exc}. Raw: {raw[:200]!r}")
        return None


def review(
    text: str,
    template_name: Optional[str],
    attachment_path: Path,
    is_native: bool,
    existing_fields: Optional[dict] = None,
) -> Optional[dict]:
    """
    Use Claude Haiku to extract or fill missing invoice fields.

    Returns a dict matching template_parser.parse() shape on success, or None on
    failure. The dict includes _confidence, _required_fields_matched,
    _required_fields_total, and _claude_reviewed=True.

    Only called when CLAUDE_REVIEW_ENABLED=true.
    """
    if not CLAUDE_REVIEW_ENABLED:
        return None

    if not ANTHROPIC_API_KEY:
        log.warning("Claude reviewer: ANTHROPIC_API_KEY is not set — skipping.")
        return None

    template_fields = _load_template_fields(template_name) if template_name else []
    user_content = _build_user_content(
        text, template_fields, existing_fields or {}, attachment_path, is_native
    )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        response = client.messages.create(
            model=_MODEL,
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    # Cache the system prompt — it never changes between calls
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        )
    except anthropic.AuthenticationError:
        log.warning("Claude reviewer: invalid ANTHROPIC_API_KEY — skipping.")
        return None
    except anthropic.RateLimitError:
        log.warning("Claude reviewer: rate limited — skipping fallback review.")
        return None
    except anthropic.APIStatusError as exc:
        log.warning(f"Claude reviewer: API error {exc.status_code} — {exc.message}")
        return None
    except anthropic.APIConnectionError as exc:
        log.warning(f"Claude reviewer: connection error — {exc}")
        return None

    raw = next((b.text for b in response.content if b.type == "text"), "")
    if not raw:
        log.warning(f"Claude reviewer: empty response for {attachment_path.name}")
        return None

    result = _parse_response_json(raw)
    if result is None:
        return None

    # Merge with existing regex fields — regex takes priority on non-None values
    for k, v in (existing_fields or {}).items():
        if k.startswith("_") or k == "line_items":
            continue
        if v is not None and k not in result:
            result[k] = v

    # Compute confidence against template required fields (or all extracted fields)
    required = (
        _load_required_fields(template_name)
        if template_name
        else [f for f in result if not f.startswith("_") and f != "line_items"]
    )
    matched = sum(1 for f in required if result.get(f))
    result["_confidence"] = round(matched / len(required), 2) if required else 0.0
    result["_required_fields_matched"] = matched
    result["_required_fields_total"] = len(required)
    result["_claude_reviewed"] = True

    if "line_items" not in result:
        result["line_items"] = []

    log.info(
        f"Claude reviewer: {attachment_path.name} — "
        f"{matched}/{len(required)} required fields, "
        f"confidence={result['_confidence']:.0%}"
    )
    return result


def _load_required_fields(template_name: str) -> list[str]:
    """Return required_fields from template YAML, falling back to all fields."""
    try:
        import yaml
        tmpl_path = TEMPLATES_DIR / f"{template_name}.yaml"
        if not tmpl_path.exists():
            return []
        tmpl = yaml.safe_load(tmpl_path.read_text(encoding="utf-8"))
        required = tmpl.get("required_fields")
        if required:
            return list(required)
        return list(tmpl.get("fields", {}).keys())
    except Exception:
        return []
