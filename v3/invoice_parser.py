"""
Invoice Parser v3 — pipeline entry point.

Usage:
  python invoice_parser.py <path/to/invoice.pdf> [--email body.txt] [--auto]
  python invoice_parser.py --review-pending
"""

import sys
import argparse
from pathlib import Path

# Ensure v3/ root is on sys.path so `core.*` imports resolve correctly
sys.path.insert(0, str(Path(__file__).parent))

from core.invoices.extractor import rasterise, validate_and_stage, save_template
from core.invoices.llm_extractor import extract_from_pdf
from core.invoices.manual_review import review_payload, review_all_pending

_V3_ROOT = Path(__file__).parent

# ==============================================================================
# PIPELINE ORCHESTRATOR
# ==============================================================================

def run_pipeline(
    pdf_path: str,
    email_context: str | None = None,
    auto_review: bool = False,
):
    """Wire all stages: rasterise → LLM extract → validate/stage → HITL review."""
    # Ensure staging directories exist
    base_staging = _V3_ROOT / "invoice_staging"
    for subdir in ("pending", "approved", "rejected", "dummy"):
        (base_staging / subdir).mkdir(parents=True, exist_ok=True)

    # Stage 1: PDF classification and rasterization
    rasterised = rasterise(pdf_path)

    # Stage 2: Local LLM extraction (Ollama)
    payload = extract_from_pdf(rasterised, email_context=email_context)
    payload.source_file = pdf_path
    payload.source_type = rasterised.source_type

    # Stage 3: Template enrichment, confidence scoring, staging
    payload = validate_and_stage(payload)

    # Stage 4: Human-in-the-loop review (skipped in auto mode)
    if payload.needs_review and not auto_review:
        payload = review_payload(payload)
        if payload.parse_confidence != "rejected":
            key = (payload.customer.email.value or payload.customer.name.value or "")
            if key:
                save_template(key.lower().strip(), payload.customer)

    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Invoice Parser v3")
    parser.add_argument("pdf", nargs="?", help="Path to invoice PDF")
    parser.add_argument("--email", help="Path to email body .txt file for context")
    parser.add_argument("--auto", action="store_true", help="Skip CLI review prompts (batch mode)")
    parser.add_argument("--review-pending", action="store_true", help="Review all pending staged invoices")
    args = parser.parse_args()

    if args.review_pending:
        review_all_pending()
    else:
        if not args.pdf:
            parser.error("pdf argument is required unless --review-pending is set")

        email_text = None
        if args.email:
            email_text = Path(args.email).read_text(encoding="utf-8")

        result = run_pipeline(args.pdf, email_context=email_text, auto_review=args.auto)

        print(f"\nResult      : {result.parse_confidence.upper()}")
        print(f"Staged path : {result.staging_path}")
        if result.warnings:
            print("Warnings:")
            for w in result.warnings:
                print(f"  - {w}")
