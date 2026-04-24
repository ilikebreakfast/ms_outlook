"""
Extracts structured data from raw text using a YAML customer template.

Each template defines regex patterns for each field. The parser tries
each pattern and returns the first match. Fields with no match return None.
"""
import logging
import re
from pathlib import Path
from typing import Optional

import yaml

from config.settings import TEMPLATES_DIR

log = logging.getLogger(__name__)


def _load_template(template_name: str) -> Optional[dict]:
    template_path = TEMPLATES_DIR / f"{template_name}.yaml"
    if not template_path.exists():
        return None
    try:
        return yaml.safe_load(template_path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"Failed to load template {template_name}.yaml: {e}")
        return None


def _extract_field(text: str, patterns: list[str]) -> Optional[str]:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            # Return first capture group if present, else full match
            return (match.group(1) if match.lastindex else match.group(0)).strip()
    return None


def _extract_line_items(text: str, pattern_or_patterns) -> list[dict]:
    """
    Extracts line items using one or more regex patterns with named groups.
    Supports both a single pattern string and a list of patterns (line_items_patterns).
    Each pattern should use named groups: product_code, description, qty, uom,
    unit_price, subtotal, total — any subset is fine.
    Results from all patterns are merged; duplicate rows are deduplicated.
    """
    patterns = (
        pattern_or_patterns
        if isinstance(pattern_or_patterns, list)
        else [pattern_or_patterns]
    )
    seen: set = set()
    items: list[dict] = []
    for pattern in patterns:
        if not pattern:
            continue
        for match in re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE):
            item = {k: v.strip() if v else None for k, v in match.groupdict().items()}
            key = frozenset(item.items())
            if key not in seen:
                seen.add(key)
                items.append(item)
    return items


def parse(text: str, template_name: str) -> dict:
    """
    Returns a dict of extracted fields. Missing fields are None.
    Confidence is a simple ratio of non-null required fields.
    template_name is the YAML file stem (e.g. "evergy", not "evergy.yaml").
    """
    tmpl = _load_template(template_name)
    if not tmpl:
        log.warning(f"No template found: {template_name!r}")
        return {"error": "no_template", "_confidence": 0.0}

    fields = tmpl.get("fields", {})
    required_fields = tmpl.get("required_fields", list(fields.keys()))

    result = {}
    for field_name, patterns in fields.items():
        result[field_name] = _extract_field(text, patterns if isinstance(patterns, list) else [patterns])

    # Support both line_items_patterns (list) and legacy line_items_pattern (string)
    line_patterns = tmpl.get("line_items_patterns") or tmpl.get("line_items_pattern", "")
    result["line_items"] = _extract_line_items(text, line_patterns)

    # Confidence = fraction of required fields that were extracted
    extracted = sum(1 for f in required_fields if result.get(f))
    confidence = extracted / len(required_fields) if required_fields else 0.0
    result["_confidence"] = round(confidence, 2)
    result["_required_fields_matched"] = extracted
    result["_required_fields_total"] = len(required_fields)

    log.info(f"Parsed {template_name!r}: confidence={confidence:.0%}, "
             f"{extracted}/{len(required_fields)} required fields found.")

    # Record stat for trend analysis — best-effort, never fatal
    try:
        from database import db
        db.record_template_stat(
            template_name=template_name,
            confidence=round(confidence, 4),
            required_fields_matched=extracted,
            required_fields_total=len(required_fields),
        )
    except Exception:
        pass

    return result
