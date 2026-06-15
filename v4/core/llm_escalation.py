"""
LLM escalation layer — Claude Haiku 4.5 via the Anthropic API.

Cost strategy:
  - Prompt caching: the large stable system prompt is sent as a cache-creation
    block.  Subsequent calls within the cache TTL pay ~0.1× the read cost.
    Keep the stable prefix free of run-specific data (no timestamps, no IDs).
  - Batch API (BatchQueue): groups multiple documents into one Batches API
    request for a 50% cost reduction.  Use for non-real-time workloads
    (nightly sweeps, backfill runs).  The default run_pipeline() path uses
    inline (real-time) extraction for simplicity; switch to BatchQueue when
    latency is not a constraint.

The LLM receives a structured grid (from table_extractor.py) and labels
columns — it never receives raw PDF geometry or unstructured page images.
"""
import json
import logging
import time
from typing import Optional

import anthropic

from v4.core.config import ANTHROPIC_API_KEY, CLAUDE_MODEL

log = logging.getLogger(__name__)

_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


# ---------------------------------------------------------------------------
# System prompt — stable prefix (maximise cache hit rate)
# Do NOT include timestamps, run IDs, or any per-call data in this block.
# ---------------------------------------------------------------------------

_SYSTEM_TEXT = """\
You are an invoice data extraction assistant for a B2B supplier pipeline.

Your input is a structured table grid extracted from a PDF invoice or purchase order.
You will receive:
1. The table headers and rows as JSON (key "tables").
2. Context: the sender's email address and any known customer/ERP data.

Your task:
- Identify which column contains each of: SKU/product code, customer reference, \
description, quantity, unit of measure (UOM), unit price, line total.
- Extract all line items into the structured JSON format below.
- Extract customer info if visible (name, ABN, address, email, phone).
- Extract invoice totals (subtotal, GST/tax, grand total).
- If a field is absent, set its value to null.
- Never hallucinate or invent values.  When uncertain, set confidence to "low".

Respond ONLY with a valid JSON object using this exact schema (no markdown fences):
{
  "customer": {
    "name":    {"value": "...", "confidence": "high|medium|low"},
    "abn":     {"value": "...", "confidence": "high|medium|low"},
    "email":   {"value": "...", "confidence": "high|medium|low"},
    "phone":   {"value": "...", "confidence": "high|medium|low"},
    "address": {"value": "...", "confidence": "high|medium|low"}
  },
  "line_items": [
    {
      "line_number":  1,
      "sku":          "...",
      "customer_ref": "...",
      "description":  "...",
      "quantity":     1.0,
      "uom":          "EA",
      "unit_price":   10.00,
      "line_total":   10.00,
      "confidence":   "high|medium|low"
    }
  ],
  "totals": {
    "subtotal": 0.00,
    "tax":      0.00,
    "total":    0.00
  }
}"""

# Cache-creation block — MSAL will cache this until the TTL expires.
# anthropic-beta header is not required for prompt caching on Claude 3.5+/4.x.
_SYSTEM_BLOCKS = [
    {
        "type": "text",
        "text": _SYSTEM_TEXT,
        "cache_control": {"type": "ephemeral"},
    }
]


# ---------------------------------------------------------------------------
# JSON cleaning
# ---------------------------------------------------------------------------

def _clean_json(raw: str) -> str:
    """Strip markdown code fences if the model wrapped the JSON."""
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        # parts[1] is "json\n{...}" or just "{...}"
        inner = parts[1]
        if inner.startswith("json"):
            inner = inner[4:]
        return inner.strip()
    return raw


# ---------------------------------------------------------------------------
# Inline extraction (real-time, one document per call)
# ---------------------------------------------------------------------------

def extract_from_grid(
    grid_json: str,
    sender_email: str = "",
    customer_context: str = "",
) -> dict:
    """
    Send a serialised CellGrid to Claude Haiku for column-labelling and field
    extraction.  Returns the parsed JSON response as a dict.

    grid_json: JSON string produced by pipeline._grid_to_json().
    customer_context: optional known-customer data from the v3 memory store.
    """
    user_parts = [f"Sender: {sender_email}"]
    if customer_context:
        user_parts.append(f"Known customer context:\n{customer_context}")
    user_parts.append(f"Tables (JSON):\n{grid_json}")
    user_content = "\n\n".join(user_parts)

    resp = _get_client().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        system=_SYSTEM_BLOCKS,
        messages=[{"role": "user", "content": user_content}],
    )

    raw = resp.content[0].text if resp.content else "{}"
    try:
        return json.loads(_clean_json(raw))
    except json.JSONDecodeError as e:
        log.warning("LLM response is not valid JSON (%s) — raw: %.300s", e, raw)
        return {}


# ---------------------------------------------------------------------------
# Batch support (50% cost reduction via Anthropic Batches API)
# ---------------------------------------------------------------------------

class BatchQueue:
    """
    Accumulates extraction requests, then submits them as a single Batches API
    call for a 50% cost reduction.  Best for nightly / non-real-time workloads.

    Usage:
        q = BatchQueue()
        q.add("msg-001", grid_json, sender="a@b.com")
        q.add("msg-002", grid_json2, sender="c@d.com")
        results = q.flush()   # blocks until the batch completes
        # results: dict[custom_id -> parsed extraction dict]

    Note: flush() polls the batch status every 30 s and blocks the calling
    thread.  Run in a background thread or async task for long batches.
    """

    def __init__(self) -> None:
        self._requests: list[dict] = []

    def add(
        self,
        custom_id: str,
        grid_json: str,
        sender_email: str = "",
        customer_context: str = "",
    ) -> None:
        user_parts = [f"Sender: {sender_email}"]
        if customer_context:
            user_parts.append(f"Known customer context:\n{customer_context}")
        user_parts.append(f"Tables (JSON):\n{grid_json}")
        user_content = "\n\n".join(user_parts)

        self._requests.append({
            "custom_id": custom_id,
            "params": {
                "model": CLAUDE_MODEL,
                "max_tokens": 2048,
                "system": _SYSTEM_BLOCKS,
                "messages": [{"role": "user", "content": user_content}],
            },
        })

    def flush(self) -> dict[str, dict]:
        """Submit all queued requests and block until the batch completes."""
        if not self._requests:
            return {}

        client  = _get_client()
        batch   = client.beta.messages.batches.create(requests=self._requests)
        log.info("Batch %s submitted (%d requests)", batch.id, len(self._requests))

        while batch.processing_status == "in_progress":
            time.sleep(30)
            batch = client.beta.messages.batches.retrieve(batch.id)
            log.debug("Batch %s: %s", batch.id, batch.processing_status)

        results: dict[str, dict] = {}
        for item in client.beta.messages.batches.results(batch.id):
            cid = item.custom_id
            if item.result.type == "succeeded":
                raw = (
                    item.result.message.content[0].text
                    if item.result.message.content
                    else "{}"
                )
                try:
                    results[cid] = json.loads(_clean_json(raw))
                except json.JSONDecodeError:
                    log.warning("Batch item %s: invalid JSON in response", cid)
                    results[cid] = {}
            else:
                log.warning("Batch item %s failed: %s", cid, item.result.type)
                results[cid] = {}

        self._requests.clear()
        return results
