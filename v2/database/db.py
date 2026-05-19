import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Any, Dict, List

# Target database file path
DB_DIR = Path(__file__).parent.parent / "data"
DB_PATH = DB_DIR / "pipeline.db"


def get_db_connection() -> sqlite3.Connection:
    """Connect to SQLite database, enable WAL mode, and enforce foreign keys."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    # Enable WAL mode for parallel read/write performance
    conn.execute("PRAGMA journal_mode=WAL;")
    # Enforce foreign key constraints
    conn.execute("PRAGMA foreign_keys=ON;")
    
    return conn


def init_db() -> None:
    """Initialise SQLite database schema."""
    conn = get_db_connection()
    try:
        # 1. layout_rules table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS layout_rules (
                layout_hash TEXT PRIMARY KEY,
                supplier_name TEXT NOT NULL,
                document_type TEXT NOT NULL CHECK(document_type IN ('pdf', 'excel', 'csv')),
                status TEXT NOT NULL CHECK(status IN ('pending_approval', 'active', 'inactive')),
                extraction_rules TEXT NOT NULL, -- JSON string of anchors and proximity regexes
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        
        # 2. extraction_history table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS extraction_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                layout_hash TEXT NOT NULL,
                parsed_data TEXT,                -- JSON string of extracted values
                status TEXT NOT NULL CHECK(status IN ('success', 'drifted', 'failed')),
                processed_at TEXT NOT NULL
            );
        """)
        
        # 3. review_queue table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS review_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                extraction_history_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'resolved')),
                created_at TEXT NOT NULL,
                FOREIGN KEY (extraction_history_id) REFERENCES extraction_history (id) ON DELETE CASCADE
            );
        """)
        
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CRUD & Pipeline operations helper functions
# ---------------------------------------------------------------------------

def get_active_rule(layout_hash: str) -> Optional[Dict[str, Any]]:
    """Fetch active extraction rules JSON for a layout fingerprint."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT extraction_rules FROM layout_rules WHERE layout_hash = ? AND status = 'active';",
            (layout_hash,)
        ).fetchone()
        
        if row:
            try:
                return json.loads(row["extraction_rules"])
            except json.JSONDecodeError:
                return None
        return None
    finally:
        conn.close()


def get_rule_details(layout_hash: str) -> Optional[Dict[str, Any]]:
    """Fetch complete details of any rule by its layout hash."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT * FROM layout_rules WHERE layout_hash = ?;",
            (layout_hash,)
        ).fetchone()
        
        if row:
            return dict(row)
        return None
    finally:
        conn.close()


def save_layout_rule(
    layout_hash: str,
    supplier_name: str,
    document_type: str,
    status: str,
    extraction_rules: Dict[str, Any]
) -> None:
    """Insert or update a layout template rule mapping in database."""
    now = datetime.now(timezone.utc).isoformat()
    rules_json = json.dumps(extraction_rules)
    
    conn = get_db_connection()
    try:
        conn.execute("""
            INSERT INTO layout_rules (layout_hash, supplier_name, document_type, status, extraction_rules, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(layout_hash) DO UPDATE SET
                supplier_name = excluded.supplier_name,
                document_type = excluded.document_type,
                status = excluded.status,
                extraction_rules = excluded.extraction_rules,
                updated_at = excluded.updated_at;
        """, (layout_hash, supplier_name, document_type, status, rules_json, now, now))
        conn.commit()
    finally:
        conn.close()


def log_extraction(
    message_id: str,
    filename: str,
    layout_hash: str,
    parsed_data: Optional[Dict[str, Any]],
    status: str
) -> int:
    """Insert an extraction log and return its inserted ID."""
    now = datetime.now(timezone.utc).isoformat()
    data_json = json.dumps(parsed_data) if parsed_data is not None else None
    
    conn = get_db_connection()
    try:
        cursor = conn.execute("""
            INSERT INTO extraction_history (message_id, filename, layout_hash, parsed_data, status, processed_at)
            VALUES (?, ?, ?, ?, ?, ?);
        """, (message_id, filename, layout_hash, data_json, status, now))
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def add_to_review_queue(extraction_history_id: int, reason: str) -> int:
    """Append a flagged extraction record to the manual review queue."""
    now = datetime.now(timezone.utc).isoformat()
    
    conn = get_db_connection()
    try:
        cursor = conn.execute("""
            INSERT INTO review_queue (extraction_history_id, reason, status, created_at)
            VALUES (?, ?, 'pending', ?);
        """, (extraction_history_id, reason, now))
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_review_queue(status: str = "pending") -> List[Dict[str, Any]]:
    """Retrieve items from the human-in-the-loop review queue."""
    conn = get_db_connection()
    try:
        rows = conn.execute("""
            SELECT q.id as queue_id, q.reason, q.status as queue_status, q.created_at,
                   h.id as history_id, h.message_id, h.filename, h.layout_hash, h.parsed_data, h.status as parse_status
            FROM review_queue q
            JOIN extraction_history h ON q.extraction_history_id = h.id
            WHERE q.status = ?
            ORDER BY q.created_at DESC;
        """, (status,)).fetchall()
        
        return [dict(r) for r in rows]
    finally:
        conn.close()


def resolve_review(queue_id: int, resolve_status: str = "resolved") -> None:
    """Mark a review queue item resolved."""
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE review_queue SET status = ? WHERE id = ?;",
            (resolve_status, queue_id)
        )
        conn.commit()
    finally:
        conn.close()

# Initialize tables on import
init_db()
