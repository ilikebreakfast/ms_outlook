"""
Validates and writes the final structured JSON output.
Uses Pydantic for schema validation.
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, field_validator

from config.settings import PARSED_DIR, LOW_CONFIDENCE_THRESHOLD

log = logging.getLogger(__name__)


class LineItem(BaseModel):
    qty: Optional[str] = None
    description: Optional[str] = None
    unit_price: Optional[str] = None
    total: Optional[str] = None


class ParsedDocument(BaseModel):
    customer_name: Optional[str] = None
    abn: Optional[str] = None
    address: Optional[str] = None
    order_date: Optional[str] = None
    requested_delivery_date: Optional[str] = None
    invoice_number: Optional[str] = None
    line_items: List[LineItem] = []

    # Pipeline metadata
    source_file: str = ""
    message_id: str = ""
    sender_email: str = ""
    received_at: str = ""
    confidence: float = 0.0
    needs_review: bool = False
    processed_at: str = ""

    @field_validator("needs_review", mode="before")
    @classmethod
    def _set_needs_review(cls, v):
        return v  # set explicitly by caller


def build_output(
    parsed: dict,
    customer_name: Optional[str],
    classification_confidence: float,
    message: dict,
    attachment_path: Path,
) -> ParsedDocument:
    line_items = [
        LineItem(**item) for item in parsed.get("line_items", [])
        if isinstance(item, dict)
    ]

    parse_confidence = parsed.get("_confidence", 0.0)
    combined_confidence = round((classification_confidence + parse_confidence) / 2, 2)

    doc = ParsedDocument(
        customer_name=customer_name or parsed.get("customer_name"),
        abn=parsed.get("abn"),
        address=parsed.get("address"),
        order_date=parsed.get("order_date"),
        requested_delivery_date=parsed.get("requested_delivery_date"),
        invoice_number=parsed.get("invoice_number"),
        line_items=line_items,
        source_file=str(attachment_path),
        message_id=message.get("id", ""),
        sender_email=message.get("from", {}).get("emailAddress", {}).get("address", ""),
        received_at=message.get("receivedDateTime", ""),
        confidence=combined_confidence,
        needs_review=combined_confidence < LOW_CONFIDENCE_THRESHOLD,
        processed_at=datetime.utcnow().isoformat() + "Z",
    )
    return doc


def save_json(doc: ParsedDocument, attachment_path: Path) -> Path:
    out_dir = PARSED_DIR / attachment_path.parent.name
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (attachment_path.stem + ".json")
    out_path.write_text(
        doc.model_dump_json(indent=2), encoding="utf-8"
    )
    log.info(f"JSON saved: {out_path}")
    return out_path
