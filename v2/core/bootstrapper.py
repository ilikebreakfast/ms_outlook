import json
import logging
import requests
from typing import Dict, Any, Tuple, Optional

from core.settings import (
    DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, ANTHROPIC_API_KEY
)
from database import db

log = logging.getLogger(__name__)

# Master prompt defining technical rule compilation schema
BOOTSTRAP_SYSTEM_PROMPT = """
You are an expert Document Structure Compiler. Your task is to analyze the provided raw text extraction of an invoice and output a strict JSON layout-parsing rule configuration.

You must compile rules for a deterministic anchored-proximity regex engine:
1. Identify the Supplier's Name from the text.
2. For each field (invoice_number, abn, invoice_date, total_amount):
   - Choose a unique 'anchor_keyword' (e.g. "Invoice Number:", "ABN:", "Total Due:") that is visually positioned immediately to the left or above the actual value.
   - Define a 'search_direction' (e.g. "relative_right", "relative_below").
   - Define a small local 'window_characters' size (usually 60 to 100) to crop around the anchor.
   - Write a high-accuracy 'regex_pattern' with a capture group "()" that matches the target value strictly within that local window. Avoid broad wildcards; use character classes (e.g. [A-Z0-9-]{4,15} for invoice numbers, [0-9.,$]+ for amounts, \\d{2}\\s?\\d{3}\\s?\\d{3}\\s?\\d{3} for ABNs).
3. If sheet grid lines are present (e.g. lines with tab '\\t' delimiters containing line items):
   - Map column indices (0-indexed) for: quantity, description, unit_price, total under tabular columns configuration.

You MUST respond with a single, raw, unadorned JSON block. Do not include markdown code block blocks (like ```json), commentary, or leading/trailing text.

Target schema output format:
{
  "supplier_name": "Supplier Company Name",
  "document_type": "pdf", // "pdf" or "excel" or "csv"
  "rules": {
    "required_fields": ["invoice_number", "total_amount"],
    "fields": {
      "invoice_number": {
        "anchor_keyword": "Invoice No:",
        "search_direction": "relative_right",
        "window_characters": 80,
        "regex_pattern": "([A-Za-z0-9-]+)"
      },
      "invoice_date": {
        "anchor_keyword": "Date:",
        "search_direction": "relative_right",
        "window_characters": 80,
        "regex_pattern": "(\\\\d{2}[/.-]\\\\d{2}[/.-]\\\\d{4})"
      },
      "abn": {
        "anchor_keyword": "ABN:",
        "search_direction": "relative_right",
        "window_characters": 80,
        "regex_pattern": "(\\\\d{2}\\\\s?\\\\d{3}\\\\s?\\\\d{3}\\\\s?\\\\d{3})"
      },
      "total_amount": {
        "anchor_keyword": "Total Due:",
        "search_direction": "relative_right",
        "window_characters": 60,
        "regex_pattern": "([0-9.,$]+)"
      }
    },
    "line_items": {
      "strategy": "tabular_columns",
      "columns": {
        "quantity": {"col_index": 0},
        "description": {"col_index": 1},
        "unit_price": {"col_index": 2},
        "total": {"col_index": 3}
      }
    }
  }
}
"""


def _query_deepseek(raw_text: str) -> str:
    """Invoke Deepseek V3 API via raw HTTP call."""
    if not DEEPSEEK_API_KEY:
        raise ValueError("DEEPSEEK_API_KEY is not configured in settings.")
        
    url = f"{DEEPSEEK_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": BOOTSTRAP_SYSTEM_PROMPT},
            {"role": "user", "content": f"Here is the raw document text:\n\n{raw_text}"}
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"}
    }
    
    resp = requests.post(url, headers=headers, json=payload, timeout=45)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _query_anthropic(raw_text: str) -> str:
    """Invoke Claude 3.5 Haiku API via raw HTTP call."""
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY is not configured in settings.")
        
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json"
    }
    
    user_prompt = f"Extract extraction rules for this raw document text:\n\n{raw_text}"
    
    payload = {
        "model": "claude-3-5-haiku-20241022",
        "max_tokens": 1500,
        "temperature": 0.1,
        "system": BOOTSTRAP_SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": user_prompt}
        ]
    }
    
    resp = requests.post(url, headers=headers, json=payload, timeout=45)
    resp.raise_for_status()
    data = resp.json()
    return data["content"][0]["text"]


def bootstrap_template(raw_text: str, document_type: str, layout_hash: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """
    Perform one-time bootstrapping for a new layout fingerprint.
    Queries the selected API, validates the rule schema, and saves to database under pending_approval.
    Returns:
        (supplier_name, rules_dict) or None if bootstrapping fails.
    """
    log.info(f"Triggering one-time LLM bootstrapping for layout fingerprint '{layout_hash}'.")
    
    response_text = ""
    # Try Deepseek first if configured, otherwise fall back to Anthropic
    if DEEPSEEK_API_KEY:
        try:
            response_text = _query_deepseek(raw_text)
        except Exception as e:
            log.warning(f"Deepseek bootstrapping failed: {e}. Trying Anthropic fallback...")
            
    if not response_text and ANTHROPIC_API_KEY:
        try:
            response_text = _query_anthropic(raw_text)
        except Exception as e:
            log.error(f"Anthropic bootstrapping failed: {e}.")
            
    if not response_text:
        log.error("Failed to query bootstrapping LLM APIs. Both Deepseek and Anthropic are either unconfigured or unreachable.")
        return None

    # Parse and validate structural JSON response
    try:
        # Strip potential markdown wrapping if present
        clean_json = response_text.strip()
        if clean_json.startswith("```json"):
            clean_json = clean_json.split("```json", 1)[1]
        if clean_json.endswith("```"):
            clean_json = clean_json.rsplit("```", 1)[0]
        clean_json = clean_json.strip()

        data = json.loads(clean_json)
        supplier_name = data.get("supplier_name", "Unknown Supplier")
        doc_type = data.get("document_type", document_type)
        rules = data.get("rules", {})
        
        # Enforce basic schema presence
        if "fields" not in rules:
            log.error("Bootstrapped rule output is missing core 'fields' key.")
            return None

        # Cache the suggested rules in layout_rules database under pending_approval status
        db.save_layout_rule(
            layout_hash=layout_hash,
            supplier_name=supplier_name,
            document_type=doc_type,
            status="pending_approval",
            extraction_rules=rules
        )
        
        log.info(f"Successfully bootstrapped new template rule for '{supplier_name}' under 'pending_approval' (Hash: {layout_hash}).")
        return supplier_name, rules
        
    except Exception as e:
        log.error(f"Failed to parse bootstrapped template JSON payload. Error: {e}. Raw response: {response_text}")
        return None
