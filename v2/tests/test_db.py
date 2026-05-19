import os
import sys
import unittest
import tempfile
import json
from pathlib import Path

# Ensure v2 root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from database import db

class TestDatabaseModule(unittest.TestCase):
    
    def setUp(self):
        """Redirect database path to a temporary file for clean isolated testing."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_db_path = Path(self.temp_dir.name) / "test_pipeline.db"
        
        # Override module database location
        db.DB_PATH = self.temp_db_path
        db.DB_DIR = Path(self.temp_dir.name)
        
        # Run initialization
        db.init_db()
        
    def tearDown(self):
        """Clean up temporary directory and database file."""
        self.temp_dir.cleanup()

    def test_wal_mode_enabled(self):
        """Verify that Write-Ahead Logging (WAL) mode is active."""
        conn = db.get_db_connection()
        row = conn.execute("PRAGMA journal_mode;").fetchone()
        self.assertEqual(row[0].lower(), "wal")
        conn.close()

    def test_save_and_get_layout_rule(self):
        """Verify that layout rules are correctly saved, updated, and retrieved."""
        layout_hash = "abc123hash"
        rules = {
            "fields": {
                "invoice_number": {
                    "anchor": "invoice",
                    "regex": r"(\d+)"
                }
            }
        }
        
        # Test saving layout rule (pending status)
        db.save_layout_rule(
            layout_hash=layout_hash,
            supplier_name="Test Supplier Inc",
            document_type="pdf",
            status="pending_approval",
            extraction_rules=rules
        )
        
        # Getting active rule should return None since status is 'pending_approval'
        active_rule = db.get_active_rule(layout_hash)
        self.assertIsNone(active_rule)
        
        # Details should still return complete row
        details = db.get_rule_details(layout_hash)
        self.assertIsNotNone(details)
        self.assertEqual(details["supplier_name"], "Test Supplier Inc")
        self.assertEqual(details["status"], "pending_approval")
        
        # Activate rule
        db.save_layout_rule(
            layout_hash=layout_hash,
            supplier_name="Test Supplier Inc",
            document_type="pdf",
            status="active",
            extraction_rules=rules
        )
        
        # Active rule should now load JSON correctly
        active_rule = db.get_active_rule(layout_hash)
        self.assertIsNotNone(active_rule)
        self.assertEqual(active_rule["fields"]["invoice_number"]["anchor"], "invoice")

    def test_extraction_history_and_review_queue(self):
        """Verify logging, queueing, and resolve transitions in HITL flow."""
        # 1. Log extraction
        layout_hash = "xyz987hash"
        parsed_data = {"invoice_number": "INV-1002", "total_amount": 550.00}
        
        history_id = db.log_extraction(
            message_id="msg_test_001",
            filename="invoice.pdf",
            layout_hash=layout_hash,
            parsed_data=parsed_data,
            status="drifted"
        )
        
        self.assertGreater(history_id, 0)
        
        # 2. Add to review queue
        queue_id = db.add_to_review_queue(
            extraction_history_id=history_id,
            reason="Layout fingerprint mismatch"
        )
        
        self.assertGreater(queue_id, 0)
        
        # 3. Read pending queue
        pending_items = db.get_review_queue(status="pending")
        self.assertEqual(len(pending_items), 1)
        self.assertEqual(pending_items[0]["reason"], "Layout fingerprint mismatch")
        self.assertEqual(pending_items[0]["filename"], "invoice.pdf")
        
        # Parse dynamic JSON content
        data = json.loads(pending_items[0]["parsed_data"])
        self.assertEqual(data["invoice_number"], "INV-1002")
        
        # 4. Resolve item
        db.resolve_review(queue_id)
        
        # Pending should now be empty
        self.assertEqual(len(db.get_review_queue(status="pending")), 0)
        # Resolved list should contain 1 item
        self.assertEqual(len(db.get_review_queue(status="resolved")), 1)


if __name__ == "__main__":
    unittest.main()
