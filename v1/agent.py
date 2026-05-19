#!/usr/bin/env python3
"""
Orchestrating agent for the email extraction pipeline.

Uses Claude Opus 4.7 with tool use to:
  1. Run the email extraction pipeline (fetch, parse, classify, output JSON)
  2. Find and clean up duplicate attachments on disk
  3. Improve YAML templates for low-confidence parsed results
  4. Re-parse documents with updated templates and report outcomes

Usage:
    python agent.py                      # full run: pipeline + dedup + template tuning
    python agent.py --days 3             # process last 3 days of email
    python agent.py --skip-pipeline      # skip email fetch; tune templates only
    python agent.py --dry-run            # pipeline dry-run + dedup scan; no writes
    python agent.py --no-template-tuning # skip template improvement step
"""
import argparse
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

import anthropic
import yaml

# Ensure project root is importable regardless of working directory
sys.path.insert(0, str(Path(__file__).parent))

from config.settings import (
    ANTHROPIC_API_KEY,
    ATTACHMENTS_DIR,
    PARSED_DIR,
    RAW_TEXT_DIR,
    TEMPLATES_DIR,
    LOW_CONFIDENCE_THRESHOLD,
)
from pipeline.template_parser import parse as template_parse
from utils.logger import setup_logging

setup_logging()
log = logging.getLogger(__name__)

_AGENT_MODEL = "claude-opus-4-7"
_MAX_TOKENS = 16000

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _run_pipeline(days: int, dry_run: bool) -> dict:
    """Execute one full email pipeline pass."""
    try:
        from main import run_once
        result = run_once(days=days, dry_run=dry_run, allow_all=False, interactive=False)
        return {"success": True, **result}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def _scan_duplicate_attachments() -> dict:
    """Find content-identical files in the attachments directory."""
    hash_map: dict[str, list[str]] = {}
    att_dir = Path(ATTACHMENTS_DIR)
    if not att_dir.exists():
        return {"duplicates": [], "total_files": 0, "duplicate_groups": 0}

    total = 0
    for f in att_dir.rglob("*"):
        if not f.is_file():
            continue
        total += 1
        try:
            h = hashlib.sha256(f.read_bytes()).hexdigest()
            hash_map.setdefault(h, []).append(str(f))
        except Exception:
            pass

    duplicates = []
    for h, files in hash_map.items():
        if len(files) < 2:
            continue
        try:
            wasted = Path(files[1]).stat().st_size * (len(files) - 1)
        except Exception:
            wasted = 0
        duplicates.append({
            "hash": h[:12],
            "files": sorted(files),
            "wasted_bytes": wasted,
        })

    return {
        "duplicates": duplicates,
        "total_files": total,
        "duplicate_groups": len(duplicates),
    }


def _remove_files(file_paths: list[str], dry_run: bool = False) -> dict:
    """Remove files from disk. dry_run=True lists what would be removed."""
    removed: list[str] = []
    errors: list[dict] = []
    for fp in file_paths:
        p = Path(fp)
        if not p.exists():
            errors.append({"file": fp, "error": "not found"})
            continue
        if dry_run:
            removed.append(fp)
        else:
            try:
                p.unlink()
                removed.append(fp)
            except Exception as exc:
                errors.append({"file": fp, "error": str(exc)})
    return {"removed": removed, "errors": errors, "dry_run": dry_run}


def _list_parsed_results(
    status_filter: str | None = None,
    max_confidence: float | None = None,
) -> list[dict]:
    """List parsed JSON output files with status and confidence metadata."""
    results = []
    parsed_dir = Path(PARSED_DIR)
    if not parsed_dir.exists():
        return []

    for f in sorted(parsed_dir.rglob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            status = data.get("status", "unknown")
            confidence = data.get("confidence")

            if status_filter and status != status_filter:
                continue
            # max_confidence: keep only results strictly below this threshold
            if max_confidence is not None and (
                confidence is None or confidence >= max_confidence
            ):
                continue

            results.append({
                "json_path": str(f),
                "customer_name": data.get("customer_name"),
                "status": status,
                "confidence": confidence,
                "needs_review": data.get("needs_review", False),
                "template_name": data.get("template_name"),
                "attachment_path": data.get("attachment_path"),
                "invoice_number": data.get("invoice_number"),
            })
        except Exception:
            pass
    return results


def _read_document_details(json_path: str) -> dict:
    """Read a parsed JSON file and its corresponding raw extracted text."""
    p = Path(json_path)
    if not p.exists():
        return {"error": f"File not found: {json_path}"}

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"error": str(exc)}

    # Locate the raw text file: raw_text/<att_parent>/<stem>.txt
    raw_text = None
    att_path_str = data.get("attachment_path", "")
    if att_path_str:
        att_p = Path(att_path_str)
        raw_dir = Path(RAW_TEXT_DIR) / att_p.parent.name
        candidates = list(raw_dir.glob(f"{att_p.stem}*.txt")) if raw_dir.exists() else []
        if not candidates:
            candidates = list(Path(RAW_TEXT_DIR).rglob(f"{att_p.stem}*.txt"))
        if candidates:
            raw_text = candidates[0].read_text(encoding="utf-8", errors="replace")

    return {"parsed": data, "raw_text": raw_text}


def _list_templates() -> list[dict]:
    """List all YAML parsing templates in config/templates/."""
    tmpl_dir = Path(TEMPLATES_DIR)
    if not tmpl_dir.exists():
        return []

    templates = []
    for f in sorted(tmpl_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
            templates.append({
                "name": f.stem,
                "path": str(f),
                "customer_name": data.get("customer_name"),
                "fields": list(data.get("fields", {}).keys()),
                "required_fields": data.get("required_fields", []),
            })
        except Exception:
            templates.append({"name": f.stem, "path": str(f), "error": "parse error"})
    return templates


def _read_template(template_name: str) -> dict:
    """Read a YAML template file in full."""
    tmpl_path = Path(TEMPLATES_DIR) / f"{template_name}.yaml"
    if not tmpl_path.exists():
        return {"error": f"Template not found: {template_name}.yaml"}
    raw = tmpl_path.read_text(encoding="utf-8")
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return {"error": f"YAML parse error: {exc}", "raw": raw}
    return {"name": template_name, "path": str(tmpl_path), "raw": raw, "parsed": parsed}


def _update_template(template_name: str, yaml_content: str) -> dict:
    """Validate and write updated YAML content to a template file."""
    tmpl_path = Path(TEMPLATES_DIR) / f"{template_name}.yaml"
    try:
        parsed = yaml.safe_load(yaml_content)
        if not isinstance(parsed, dict):
            return {"error": "YAML must be a mapping at the top level"}
    except yaml.YAMLError as exc:
        return {"error": f"Invalid YAML: {exc}"}

    tmpl_path.write_text(yaml_content, encoding="utf-8")
    return {
        "success": True,
        "path": str(tmpl_path),
        "fields": list(parsed.get("fields", {}).keys()),
        "required_fields": parsed.get("required_fields", []),
    }


def _test_template_on_text(template_name: str, raw_text: str) -> dict:
    """Run template_parser.parse() on provided text and return results."""
    try:
        result = template_parse(raw_text, template_name)
        return {
            "success": True,
            "confidence": result.get("_confidence"),
            "required_matched": result.get("_required_fields_matched"),
            "required_total": result.get("_required_fields_total"),
            "fields": {
                k: v
                for k, v in result.items()
                if not k.startswith("_") and k != "line_items"
            },
            "line_items_count": len(result.get("line_items", [])),
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def _get_review_queue() -> list[dict]:
    """Get documents pending manual review from the database."""
    try:
        from database import db
        items = db.get_review_queue("pending")
        return items if items else []
    except Exception as exc:
        return [{"error": str(exc)}]


# ---------------------------------------------------------------------------
# Tool schemas (Claude API format)
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "run_pipeline",
        "description": (
            "Run a full email extraction pipeline pass. Fetches unread emails from Outlook, "
            "downloads attachments, extracts text (PDF native, OCR, Excel, CSV), classifies "
            "senders against the address book, parses invoice fields using YAML templates, "
            "saves JSON output, and moves processed emails to the Processed-Pipeline folder."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "How many days of unread email to look back (default: 1)",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, classify and log but do not write files or move emails",
                },
            },
            "required": [],
        },
    },
    {
        "name": "scan_duplicate_attachments",
        "description": (
            "Scan the attachments/ directory for files with identical content (SHA-256). "
            "Returns groups of files sharing the same hash, with estimated wasted disk space."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "remove_files",
        "description": (
            "Remove a list of files from disk. Use to delete duplicate attachment copies. "
            "Always keep the first/oldest file in each duplicate group."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Absolute paths of files to delete",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, report what would be removed without deleting",
                },
            },
            "required": ["file_paths"],
        },
    },
    {
        "name": "list_parsed_results",
        "description": (
            "List parsed JSON output files with their status and confidence scores. "
            "Use max_confidence=0.8 to find documents needing template improvement."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status_filter": {
                    "type": "string",
                    "enum": ["parsed", "extracted_only", "low_confidence"],
                    "description": "Only return results with this status (omit for all)",
                },
                "max_confidence": {
                    "type": "number",
                    "description": "Only return results with confidence strictly below this value (e.g. 0.8)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "read_document_details",
        "description": (
            "Read the full parsed JSON and raw extracted text for a document. "
            "Essential for understanding what the template failed to match."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "json_path": {
                    "type": "string",
                    "description": "Absolute path to the parsed JSON file (from list_parsed_results)",
                },
            },
            "required": ["json_path"],
        },
    },
    {
        "name": "list_templates",
        "description": "List all YAML parsing templates in config/templates/ with their field names.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "read_template",
        "description": (
            "Read a YAML parsing template file in full. "
            "Returns raw YAML text and the parsed structure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "template_name": {
                    "type": "string",
                    "description": "Template file stem without .yaml extension (e.g. 'evergy')",
                },
            },
            "required": ["template_name"],
        },
    },
    {
        "name": "update_template",
        "description": (
            "Validate and write updated YAML content to a template file. "
            "Always test with test_template_on_text first before calling this."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "template_name": {
                    "type": "string",
                    "description": "Template file stem (e.g. 'evergy')",
                },
                "yaml_content": {
                    "type": "string",
                    "description": "Complete YAML content to write (must be valid YAML)",
                },
            },
            "required": ["template_name", "yaml_content"],
        },
    },
    {
        "name": "test_template_on_text",
        "description": (
            "Test a YAML template against raw extracted text. "
            "Returns confidence, matched/total required fields, and extracted values. "
            "Always call this before update_template to verify improvement."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "template_name": {
                    "type": "string",
                    "description": "Template file stem (e.g. 'evergy')",
                },
                "raw_text": {
                    "type": "string",
                    "description": "The raw extracted text to test against",
                },
            },
            "required": ["template_name", "raw_text"],
        },
    },
    {
        "name": "get_review_queue",
        "description": "Get the list of documents pending manual review from the database.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

def _dispatch(tool_name: str, tool_input: dict, dry_run: bool) -> Any:
    match tool_name:
        case "run_pipeline":
            return _run_pipeline(
                days=tool_input.get("days", 1),
                dry_run=tool_input.get("dry_run", dry_run),
            )
        case "scan_duplicate_attachments":
            return _scan_duplicate_attachments()
        case "remove_files":
            return _remove_files(
                file_paths=tool_input["file_paths"],
                dry_run=dry_run or tool_input.get("dry_run", False),
            )
        case "list_parsed_results":
            return _list_parsed_results(
                status_filter=tool_input.get("status_filter"),
                max_confidence=tool_input.get("max_confidence"),
            )
        case "read_document_details":
            return _read_document_details(tool_input["json_path"])
        case "list_templates":
            return _list_templates()
        case "read_template":
            return _read_template(tool_input["template_name"])
        case "update_template":
            if dry_run:
                preview = tool_input.get("yaml_content", "")[:200]
                return {
                    "dry_run": True,
                    "would_update": tool_input["template_name"],
                    "yaml_preview": preview,
                }
            return _update_template(tool_input["template_name"], tool_input["yaml_content"])
        case "test_template_on_text":
            return _test_template_on_text(tool_input["template_name"], tool_input["raw_text"])
        case "get_review_queue":
            return _get_review_queue()
        case _:
            return {"error": f"Unknown tool: {tool_name}"}


# ---------------------------------------------------------------------------
# Agent system prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = f"""You are an expert invoice-processing pipeline agent. You orchestrate three tasks in order:

## Task 1 — Email Extraction Pipeline
Run the pipeline to fetch unread emails from Microsoft Outlook. It downloads attachments, extracts text (PDF native, OCR fallback, Excel, CSV), classifies senders against the address book, parses invoice fields using YAML regex templates, and saves structured JSON output.

## Task 2 — Duplicate Attachment Cleanup
Scan attachments/ for files with identical content (SHA-256 hash). For each duplicate group, keep the first/oldest file and delete the rest. Report total disk space recovered.

## Task 3 — Template Improvement
Review parsed results with low confidence (below {LOW_CONFIDENCE_THRESHOLD}) or status 'extracted_only'/'low_confidence'. For each document:
  a. Read its details (parsed JSON + raw extracted text)
  b. Read the current YAML template
  c. Study the raw text carefully — find the exact format of invoice numbers, dates, amounts, ABNs, etc. in THIS document (not in the template)
  d. Write improved regex patterns that match the actual text
  e. Test the updated template against the raw text with test_template_on_text
  f. If confidence improves to ≥ {LOW_CONFIDENCE_THRESHOLD}, save the template with update_template; otherwise iterate once more

## YAML Template Rules
- **Single backslashes** in regex: `\d+` is correct, `\\d+` is wrong (YAML, not JSON)
- Each field has a list of patterns tried in order; first match wins
- Capture groups extract the value: `Invoice\s*No[.:]?\s*(\w+{{4,20}})`
- `required_fields` drives confidence: matched_required / total_required
- `line_items_pattern`: one regex with named groups `(?P<qty>...)`, `(?P<description>...)`, `(?P<unit_price>...)`, `(?P<total>...)`
- Write loose patterns — better to over-match than to miss
- Account for OCR artefacts: split words, merged numbers, stray spaces, rotated characters
- Omit a field's patterns entirely rather than leaving an empty list

## Workflow Order
1. Run the pipeline (unless --skip-pipeline)
2. Scan and clean up duplicate attachments
3. List low-confidence and extracted_only results; improve templates iteratively
4. Fetch and include the review queue in the final report

## Final Report (required)
At the end, clearly summarise:
- Pipeline run stats (emails processed, attachments, errors, moved)
- Duplicates removed (count and bytes recovered)
- Template changes (per template: fields improved, confidence before → after)
- Documents still flagged for manual review
"""


# ---------------------------------------------------------------------------
# Main agentic loop
# ---------------------------------------------------------------------------

def run_agent(
    days: int,
    skip_pipeline: bool,
    dry_run: bool,
    no_template_tuning: bool,
) -> None:
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY is not set — add it to .env to use the agent.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Build the initial user instruction
    parts: list[str] = []
    if skip_pipeline:
        parts.append("Skip the pipeline run — go straight to deduplication and template tuning.")
    else:
        parts.append(f"Run the email extraction pipeline for the last {days} day(s).")
    if dry_run:
        parts.append("This is a dry run: do not write files, delete files, or modify templates.")
    if no_template_tuning:
        parts.append("Skip template tuning — only run the pipeline and deduplication.")
    parts.append("Follow your full workflow and produce a clear final report.")

    user_message = " ".join(parts)
    messages: list[dict] = [{"role": "user", "content": user_message}]

    print(f"\nAgent starting — {user_message}", flush=True)
    print("=" * 70, flush=True)

    iteration = 0
    max_iterations = 50  # safety limit for long template-tuning sessions

    while iteration < max_iterations:
        iteration += 1

        response = client.messages.create(
            model=_AGENT_MODEL,
            max_tokens=_MAX_TOKENS,
            thinking={"type": "adaptive"},
            output_config={"effort": "xhigh"},
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    # Cache the system prompt — it is identical on every turn
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=TOOLS,
            messages=messages,
        )

        # Print any text output the agent produces
        for block in response.content:
            if getattr(block, "type", None) == "text" and block.text.strip():
                print(f"\n[Agent]\n{block.text}", flush=True)

        if response.stop_reason == "end_turn":
            print("\n[Agent] Done.", flush=True)
            break

        if response.stop_reason != "tool_use":
            print(f"\n[Agent] Unexpected stop_reason: {response.stop_reason!r}", flush=True)
            break

        # Execute every tool call in the response
        tool_results: list[dict] = []
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue

            tool_name = block.name
            tool_input = block.input

            # Print a compact summary of the call (omit large string args)
            display_input = {
                k: (v[:80] + "…" if isinstance(v, str) and len(v) > 80 else v)
                for k, v in tool_input.items()
                if k not in ("yaml_content", "raw_text")
            }
            print(f"\n[Tool] {tool_name}({json.dumps(display_input)})", flush=True)

            result = _dispatch(tool_name, tool_input, dry_run=dry_run)
            result_str = json.dumps(result, indent=2, default=str)

            preview = result_str[:500]
            if len(result_str) > 500:
                preview += f"\n… ({len(result_str):,} chars total)"
            print(f"[Result] {preview}", flush=True)

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_str,
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    if iteration >= max_iterations:
        print(f"\n[Agent] Safety limit ({max_iterations} iterations) reached — stopping.", flush=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Claude Opus 4.7 agent: email extraction, dedup, and template tuning",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--days", type=int, default=1,
        help="How many days of email to process (default: 1)",
    )
    parser.add_argument(
        "--skip-pipeline", action="store_true",
        help="Skip email fetch; jump straight to dedup + template tuning",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Pipeline dry-run; dedup scan only; no template writes",
    )
    parser.add_argument(
        "--no-template-tuning", action="store_true",
        help="Skip template improvement step",
    )
    args = parser.parse_args()

    run_agent(
        days=args.days,
        skip_pipeline=args.skip_pipeline,
        dry_run=args.dry_run,
        no_template_tuning=args.no_template_tuning,
    )


if __name__ == "__main__":
    main()
