"""
SQLite persistence layer for the ms_outlook pipeline.

Four tables:
  processed_documents — one row per attachment per run; primary dedup key is
                        (message_id, attachment_filename). All pipeline metadata
                        is stored here for audit and reporting.
  review_queue        — rows inserted when needs_review=True or status=low_confidence.
                        Operators clear items via `manage.py review-queue`.
  template_stats      — one row per template per run; used to detect confidence
                        drift over time via `manage.py analyze-template`.
  parsed_invoices     — one row per parsed JSON; stores all extracted fields.
                        Columns are added automatically when new fields are seen.
                        line_items is stored as JSON text.

Usage (same interface as the original stub so main.py needs no changes):
    from database import db
    if db.already_processed(message_id, filename):
        ...
    db.record(message_id=..., attachment_filename=..., ...)
"""
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config.settings import DB_PATH

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_documents (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id           TEXT    NOT NULL,
    attachment_filename  TEXT    NOT NULL,
    sender_email         TEXT,
    customer_name        TEXT,
    invoice_number       TEXT,
    confidence           REAL,
    needs_review         INTEGER DEFAULT 0,   -- boolean: 0/1
    status               TEXT,
    json_path            TEXT,
    attachment_path      TEXT,
    received_at          TEXT,
    processed_at         TEXT    NOT NULL,
    error                TEXT,
    template_name        TEXT,
    UNIQUE(message_id, attachment_filename)
);

CREATE TABLE IF NOT EXISTS review_queue (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    processed_doc_id     INTEGER NOT NULL REFERENCES processed_documents(id),
    status               TEXT    NOT NULL DEFAULT 'pending',  -- pending | resolved | dismissed
    reason               TEXT,
    created_at           TEXT    NOT NULL,
    resolved_at          TEXT,
    resolved_by          TEXT
);

CREATE TABLE IF NOT EXISTS template_stats (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    template_name           TEXT    NOT NULL,
    run_at                  TEXT    NOT NULL,
    confidence              REAL    NOT NULL,
    required_fields_matched INTEGER NOT NULL DEFAULT 0,
    required_fields_total   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_pd_message    ON processed_documents(message_id);
CREATE INDEX IF NOT EXISTS idx_pd_customer   ON processed_documents(customer_name);
CREATE INDEX IF NOT EXISTS idx_pd_review     ON processed_documents(needs_review);
CREATE INDEX IF NOT EXISTS idx_rq_status     ON review_queue(status);
CREATE INDEX IF NOT EXISTS idx_ts_template   ON template_stats(template_name);
"""

_PARSED_INVOICES_SCHEMA = """
CREATE TABLE IF NOT EXISTS parsed_invoices (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file          TEXT    NOT NULL UNIQUE,
    synced_at            TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pi_source ON parsed_invoices(source_file);
"""

_INVOICE_LINES_SCHEMA = """
CREATE TABLE IF NOT EXISTS invoice_lines (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file          TEXT    NOT NULL,
    line_num             INTEGER NOT NULL,
    -- invoice-level fields (repeated per line)
    message_id           TEXT,
    sender_email         TEXT,
    customer_name        TEXT,
    company_name         TEXT,
    company_abn          TEXT,
    po_number            TEXT,
    invoice_number       TEXT,
    order_date           TEXT,
    delivery_date        TEXT,
    invoice_subtotal     TEXT,
    invoice_tax_amount   TEXT,
    invoice_total        TEXT,
    confidence           REAL,
    status               TEXT,
    received_at          TEXT,
    processed_at         TEXT,
    -- line item fields
    product_code         TEXT,
    description          TEXT,
    qty                  TEXT,
    uom                  TEXT,
    unit_price           TEXT,
    line_subtotal        TEXT,
    line_total           TEXT,
    UNIQUE(source_file, line_num)
);
CREATE INDEX IF NOT EXISTS idx_il_source   ON invoice_lines(source_file);
CREATE INDEX IF NOT EXISTS idx_il_product  ON invoice_lines(product_code);
CREATE INDEX IF NOT EXISTS idx_il_company  ON invoice_lines(company_name);
"""


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def _ensure_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def _connect():
    _ensure_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # safe for concurrent readers
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init() -> None:
    """Create tables if they don't exist. Safe to call on every startup."""
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        conn.executescript(_PARSED_INVOICES_SCHEMA)
        conn.executescript(_INVOICE_LINES_SCHEMA)
        # Migrations for columns added after initial schema deployment
        for col, definition in [
            ("status", "TEXT"),
            ("template_name", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE processed_documents ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError:
                pass  # column already exists
    log.debug(f"Database ready: {DB_PATH}")


# ---------------------------------------------------------------------------
# Public API — matches the stub interface used in main.py
# ---------------------------------------------------------------------------

def already_processed(message_id: str, attachment_filename: str) -> bool:
    """
    Returns True if this (message_id, filename) pair has already been recorded
    without an error — i.e. was successfully processed previously.
    """
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id FROM processed_documents
            WHERE message_id = ? AND attachment_filename = ? AND error IS NULL
            LIMIT 1
            """,
            (message_id, attachment_filename),
        ).fetchone()
    return row is not None


def record(
    *,
    message_id: str,
    attachment_filename: str,
    sender_email: str = "",
    received_at: str = "",
    processed_at: str = "",
    customer_name: Optional[str] = None,
    invoice_number: Optional[str] = None,
    confidence: Optional[float] = None,
    needs_review: bool = False,
    status: Optional[str] = None,
    json_path: Optional[str] = None,
    attachment_path: Optional[str] = None,
    error: Optional[str] = None,
    template_name: Optional[str] = None,
) -> int:
    """
    Insert or replace a processed-document record.
    Returns the row id.
    If a row already exists for (message_id, attachment_filename) it is
    replaced — this handles re-runs after a previous error.
    """
    if not processed_at:
        processed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM processed_documents WHERE message_id = ? AND attachment_filename = ?",
            (message_id, attachment_filename),
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE processed_documents SET
                    customer_name   = ?, invoice_number = ?, confidence    = ?,
                    needs_review    = ?, status         = ?, json_path     = ?,
                    attachment_path = ?, processed_at   = ?, error         = ?,
                    template_name   = ?
                WHERE id = ?
                """,
                (
                    customer_name, invoice_number, confidence, int(needs_review),
                    status, json_path, attachment_path, processed_at, error,
                    template_name, existing["id"],
                ),
            )
            doc_id = existing["id"]
        else:
            cursor = conn.execute(
                """
                INSERT INTO processed_documents
                    (message_id, attachment_filename, sender_email, customer_name,
                     invoice_number, confidence, needs_review, status,
                     json_path, attachment_path, received_at, processed_at,
                     error, template_name)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    message_id, attachment_filename, sender_email, customer_name,
                    invoice_number, confidence, int(needs_review), status,
                    json_path, attachment_path, received_at, processed_at,
                    error, template_name,
                ),
            )
            doc_id = cursor.lastrowid

        # If this document needs review, add it to the review queue
        # (only if not already queued to avoid duplicates on re-runs)
        if needs_review and doc_id and not error:
            existing = conn.execute(
                "SELECT id FROM review_queue WHERE processed_doc_id = ? AND status = 'pending'",
                (doc_id,),
            ).fetchone()
            if not existing:
                conn.execute(
                    """
                    INSERT INTO review_queue (processed_doc_id, status, reason, created_at)
                    VALUES (?, 'pending', ?, ?)
                    """,
                    (doc_id, status or "low_confidence", processed_at),
                )

    return doc_id or 0


def record_template_stat(
    *,
    template_name: str,
    confidence: float,
    required_fields_matched: int = 0,
    required_fields_total: int = 0,
) -> None:
    """Record a single template parse result for trend analysis."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO template_stats
                (template_name, run_at, confidence, required_fields_matched, required_fields_total)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                template_name,
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                confidence,
                required_fields_matched,
                required_fields_total,
            ),
        )


# ---------------------------------------------------------------------------
# parsed_invoices — dynamic-column extracted-field store
# ---------------------------------------------------------------------------

# Fields that are always present as base columns (never added via ALTER TABLE)
_PI_BASE_COLS = {"id", "source_file", "synced_at"}

# Fields to skip storing (internal review metadata, not invoice data)
_PI_SKIP_COLS = {"_claude_reviewed", "_reviewed_at", "_review_notes"}


def _ensure_parsed_invoice_columns(conn: sqlite3.Connection, keys: list[str]) -> None:
    """Add any missing columns to parsed_invoices for the given field names."""
    existing = {
        row[1] for row in conn.execute("PRAGMA table_info(parsed_invoices)").fetchall()
    }
    for key in keys:
        if key not in existing and key not in _PI_BASE_COLS:
            try:
                conn.execute(f"ALTER TABLE parsed_invoices ADD COLUMN [{key}] TEXT")
            except sqlite3.OperationalError:
                pass  # race or already exists


def record_parsed_invoice(data: dict) -> None:
    """
    Upsert one parsed invoice JSON dict into parsed_invoices.
    - source_file is the dedup key (UNIQUE).
    - Any key in data becomes a column; new keys trigger ALTER TABLE ADD COLUMN.
    - list/dict values (e.g. line_items) are serialized as JSON text.
    - Skips internal review metadata keys.
    """
    source_file = data.get("source_file", "")
    if not source_file:
        return

    synced_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # Flatten: serialize complex values, skip internal keys
    flat: dict[str, object] = {}
    for k, v in data.items():
        if k in _PI_SKIP_COLS or k in _PI_BASE_COLS:
            continue
        if isinstance(v, (list, dict)):
            flat[k] = json.dumps(v, ensure_ascii=False)
        else:
            flat[k] = v

    with _connect() as conn:
        _ensure_parsed_invoice_columns(conn, list(flat.keys()))

        cols = ["source_file", "synced_at"] + list(flat.keys())
        vals = [source_file, synced_at] + list(flat.values())
        placeholders = ", ".join("?" * len(cols))
        col_expr = ", ".join(f"[{c}]" for c in cols)
        update_expr = ", ".join(
            f"[{c}] = excluded.[{c}]" for c in cols if c not in ("source_file",)
        )
        conn.execute(
            f"""
            INSERT INTO parsed_invoices ({col_expr}) VALUES ({placeholders})
            ON CONFLICT([source_file]) DO UPDATE SET {update_expr}
            """,
            vals,
        )

    record_invoice_lines(data)


# ---------------------------------------------------------------------------
# invoice_lines — one row per line item, invoice fields repeated
# ---------------------------------------------------------------------------

def record_invoice_lines(data: dict) -> None:
    """
    Upsert line items from a parsed invoice JSON dict into invoice_lines.
    Each line item becomes one row. Invoice-level fields are repeated on every row.
    Dedup key is (source_file, line_num).
    """
    source_file = data.get("source_file", "")
    line_items = data.get("line_items") or []
    if not source_file or not line_items:
        return

    invoice_fields = {
        "message_id":         data.get("message_id"),
        "sender_email":       data.get("sender_email"),
        "customer_name":      data.get("customer_name"),
        "company_name":       data.get("company_name"),
        "company_abn":        data.get("company_abn"),
        "po_number":          data.get("po_number"),
        "invoice_number":     data.get("invoice_number"),
        "order_date":         data.get("order_date"),
        "delivery_date":      data.get("delivery_date"),
        "invoice_subtotal":   data.get("subtotal"),
        "invoice_tax_amount": data.get("tax_amount"),
        "invoice_total":      data.get("total_amount"),
        "confidence":         data.get("confidence"),
        "status":             data.get("status"),
        "received_at":        data.get("received_at"),
        "processed_at":       data.get("processed_at"),
    }

    with _connect() as conn:
        for i, item in enumerate(line_items, start=1):
            conn.execute(
                """
                INSERT INTO invoice_lines (
                    source_file, line_num,
                    message_id, sender_email, customer_name, company_name, company_abn,
                    po_number, invoice_number, order_date, delivery_date,
                    invoice_subtotal, invoice_tax_amount, invoice_total,
                    confidence, status, received_at, processed_at,
                    product_code, description, qty, uom, unit_price, line_subtotal, line_total
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_file, line_num) DO UPDATE SET
                    message_id=excluded.message_id, sender_email=excluded.sender_email,
                    customer_name=excluded.customer_name, company_name=excluded.company_name,
                    company_abn=excluded.company_abn, po_number=excluded.po_number,
                    invoice_number=excluded.invoice_number, order_date=excluded.order_date,
                    delivery_date=excluded.delivery_date,
                    invoice_subtotal=excluded.invoice_subtotal,
                    invoice_tax_amount=excluded.invoice_tax_amount,
                    invoice_total=excluded.invoice_total,
                    confidence=excluded.confidence, status=excluded.status,
                    received_at=excluded.received_at, processed_at=excluded.processed_at,
                    product_code=excluded.product_code, description=excluded.description,
                    qty=excluded.qty, uom=excluded.uom, unit_price=excluded.unit_price,
                    line_subtotal=excluded.line_subtotal, line_total=excluded.line_total
                """,
                (
                    source_file, i,
                    invoice_fields["message_id"], invoice_fields["sender_email"],
                    invoice_fields["customer_name"], invoice_fields["company_name"],
                    invoice_fields["company_abn"], invoice_fields["po_number"],
                    invoice_fields["invoice_number"], invoice_fields["order_date"],
                    invoice_fields["delivery_date"], invoice_fields["invoice_subtotal"],
                    invoice_fields["invoice_tax_amount"], invoice_fields["invoice_total"],
                    invoice_fields["confidence"], invoice_fields["status"],
                    invoice_fields["received_at"], invoice_fields["processed_at"],
                    item.get("product_code"), item.get("description"),
                    item.get("qty"), item.get("uom"), item.get("unit_price"),
                    item.get("subtotal"), item.get("total"),
                ),
            )


# ---------------------------------------------------------------------------
# Query helpers — used by manage.py commands
# ---------------------------------------------------------------------------

def get_review_queue(status: str = "pending") -> list[dict]:
    """Return review queue items, joined with document metadata."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                rq.id          AS queue_id,
                rq.status,
                rq.reason,
                rq.created_at,
                pd.attachment_filename,
                pd.customer_name,
                pd.confidence,
                pd.sender_email,
                pd.json_path,
                pd.template_name
            FROM review_queue rq
            JOIN processed_documents pd ON pd.id = rq.processed_doc_id
            WHERE rq.status = ?
            ORDER BY rq.created_at DESC
            """,
            (status,),
        ).fetchall()
    return [dict(r) for r in rows]


def resolve_review(queue_id: int, resolved_by: str = "", status: str = "resolved") -> bool:
    """Mark a review queue item resolved or dismissed."""
    resolved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE review_queue
            SET status = ?, resolved_at = ?, resolved_by = ?
            WHERE id = ?
            """,
            (status, resolved_at, resolved_by, queue_id),
        )
    return cursor.rowcount > 0


def get_template_stats(template_name: str, last_n: int = 20) -> list[dict]:
    """Return the last N parse results for a template, newest first."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT run_at, confidence, required_fields_matched, required_fields_total
            FROM template_stats
            WHERE template_name = ?
            ORDER BY run_at DESC
            LIMIT ?
            """,
            (template_name, last_n),
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_runs(limit: int = 20) -> list[dict]:
    """Return the most recent processed documents for a run summary."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT message_id, attachment_filename, customer_name,
                   confidence, status, needs_review, processed_at, error
            FROM processed_documents
            ORDER BY processed_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_run_stats() -> dict:
    """Aggregate statistics for the metrics module."""
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM processed_documents").fetchone()[0]
        errors = conn.execute(
            "SELECT COUNT(*) FROM processed_documents WHERE error IS NOT NULL"
        ).fetchone()[0]
        pending_review = conn.execute(
            "SELECT COUNT(*) FROM review_queue WHERE status = 'pending'"
        ).fetchone()[0]
        avg_conf = conn.execute(
            "SELECT AVG(confidence) FROM processed_documents WHERE confidence IS NOT NULL AND error IS NULL"
        ).fetchone()[0]
    return {
        "total_processed": total,
        "total_errors": errors,
        "pending_review": pending_review,
        "avg_confidence": round(avg_conf, 3) if avg_conf else None,
    }


# Initialise on import so tables always exist before the first record() call.
try:
    init()
except Exception as _e:
    log.warning(f"Database init failed: {_e} — pipeline will continue without persistence.")
