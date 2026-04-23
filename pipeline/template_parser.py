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


def _extract_line_items(text: str, pattern: str) -> list[dict]:
    """
    Extracts line items using a regex with named groups:
    qty, description, unit_price, total
    """
    if not pattern:
        return []
    items = []
    for match in re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE):
        items.append({k: v.strip() if v else None for k, v in match.groupdict().items()})
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

    result["line_items"] = _extract_line_items(
        text, tmpl.get("line_items_pattern", "")
    )

    # Confidence = fraction of required fields that were extracted
    extracted = sum(1 for f in required_fields if result.get(f))
    confidence = extracted / len(required_fields) if required_fields else 0.0
    result["_confidence"] = round(confidence, 2)

    log.info(f"Parsed {template_name!r}: confidence={confidence:.0%}, "
             f"{extracted}/{len(required_fields)} required fields found.")
    return result
