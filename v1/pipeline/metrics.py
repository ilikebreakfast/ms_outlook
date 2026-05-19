"""
Run metrics emitter.

After each pipeline run, call emit() to:
  1. Write logs/metrics.json with current run stats and DB aggregates.
  2. Optionally POST a summary to WEBHOOK_URL (Slack, Teams, n8n, etc.).

The JSON file is overwritten each run so it always reflects the latest state.
It can be scraped by a monitoring tool or read by `manage.py health`.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from config.settings import LOGS_DIR, WEBHOOK_URL, ALERT_WEBHOOK_URL, REVIEW_WEBHOOK_URL

log = logging.getLogger(__name__)

METRICS_PATH = LOGS_DIR / "metrics.json"


def emit(
    *,
    run_started_at: str,
    days_processed: int,
    total_attachments: int,
    blocked: int,
    moved: int,
    errors: int,
    needs_review: int,
    avg_confidence: Optional[float],
    pending_review_queue: int,
) -> None:
    """
    Write metrics.json and fire optional webhooks.
    Call once at the end of each pipeline run.
    All arguments are counters/values collected during the run.
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    run_finished_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload = {
        "run_started_at": run_started_at,
        "run_finished_at": run_finished_at,
        "days_processed": days_processed,
        "total_attachments": total_attachments,
        "blocked": blocked,
        "moved": moved,
        "errors": errors,
        "needs_review_this_run": needs_review,
        "avg_confidence_this_run": round(avg_conf, 3) if (avg_conf := avg_confidence) else None,
        "pending_review_queue_total": pending_review_queue,
    }

    # Always write the metrics file
    METRICS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log.debug(f"Metrics written: {METRICS_PATH}")

    # Webhook notification (if configured)
    if WEBHOOK_URL:
        _post_webhook(WEBHOOK_URL, payload, alert=False)

    # Alert if review queue is growing — fire on ALERT_WEBHOOK_URL if separate
    if pending_review_queue > 0 and ALERT_WEBHOOK_URL and ALERT_WEBHOOK_URL != WEBHOOK_URL:
        _post_webhook(
            ALERT_WEBHOOK_URL,
            payload,
            alert=True,
            alert_reason=f"{pending_review_queue} document(s) awaiting review",
        )


def _post_webhook(
    url: str,
    payload: dict,
    *,
    alert: bool = False,
    alert_reason: str = "",
) -> None:
    prefix = "ALERT" if alert else "Run complete"
    reason_str = f" — {alert_reason}" if alert_reason else ""
    summary = (
        f"[ms_outlook] {prefix}{reason_str}: "
        f"{payload['total_attachments']} processed, "
        f"{payload['blocked']} blocked, "
        f"{payload['needs_review_this_run']} need review, "
        f"confidence avg={payload['avg_confidence_this_run'] or 'n/a'}"
    )
    body = {"text": summary, "details": payload}
    try:
        resp = requests.post(url, json=body, timeout=8)
        resp.raise_for_status()
        log.debug(f"Webhook posted to {url}")
    except Exception as exc:
        # Webhook failure must never crash the pipeline
        log.warning(f"Webhook POST failed ({url}): {exc}")


def notify_low_confidence(
    *,
    customer_name: str,
    filename: str,
    confidence: float,
    status: str,
    sender_email: str,
    json_path: str,
    template_name: Optional[str],
) -> None:
    """
    Fire a per-document webhook when confidence falls below threshold.
    Uses REVIEW_WEBHOOK_URL if set, otherwise falls back to ALERT_WEBHOOK_URL.
    No-op if neither is configured.
    """
    url = REVIEW_WEBHOOK_URL or ALERT_WEBHOOK_URL
    if not url:
        return

    payload = {
        "event": "low_confidence_document",
        "customer_name": customer_name,
        "filename": filename,
        "confidence": round(confidence, 3),
        "status": status,
        "sender_email": sender_email,
        "json_path": json_path,
        "template_name": template_name,
        "hint": "Run /review-invoices in Claude Code to re-extract missing fields.",
    }
    text = (
        f"[ms_outlook] Low-confidence document — "
        f"{customer_name} | {filename} | "
        f"confidence: {confidence:.0%} | status: {status}"
    )
    _post_webhook(url, payload, alert=True, alert_reason=text)


def read_last_metrics() -> Optional[dict]:
    """Return the contents of the last metrics.json, or None if not found."""
    if not METRICS_PATH.exists():
        return None
    try:
        return json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
