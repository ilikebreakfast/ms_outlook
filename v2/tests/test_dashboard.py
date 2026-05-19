import sys
import unittest
import json
from pathlib import Path
from fastapi.testclient import TestClient

# Ensure v2 root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from dashboard.main import app
from database import db


class TestDashboardModule(unittest.TestCase):

    def setUp(self):
        """Redirect database path to temporary file for clean testing."""
        import tempfile
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp_dir.name) / "test_pipeline.db"
        db.DB_DIR = Path(self.temp_dir.name)
        db.init_db()
        self.client = TestClient(app)

    def tearDown(self):
        """Close db handles and remove temp files."""
        self.temp_dir.cleanup()

    def test_dashboard_homepage_routing(self):
        """Verify the main entry homepage endpoint serves html content successfully."""
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["content-type"])
        self.assertIn("Invoice Flow v2", resp.text)

    def test_api_metrics_retrieval(self):
        """Verify metrics aggregation counts total, active, and pending records."""
        # Seed test rules
        db.save_layout_rule(
            layout_hash="hash_a",
            supplier_name="Supplier A",
            document_type="pdf",
            status="pending_approval",
            extraction_rules={"fields": {}}
        )
        db.save_layout_rule(
            layout_hash="hash_b",
            supplier_name="Supplier B",
            document_type="excel",
            status="active",
            extraction_rules={"fields": {}}
        )
        
        # Seed some dummy history runs
        db.log_extraction(
            message_id="msg_1",
            filename="inv1.pdf",
            layout_hash="hash_b",
            parsed_data={"val": 120.0},
            status="success"
        )
        
        resp = self.client.get("/api/metrics")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        
        self.assertEqual(data["total_processed"], 1)
        self.assertEqual(data["success_rate"], "100.0%")
        self.assertEqual(data["pending_reviews"], 1)
        self.assertEqual(data["active_rules"], 1)

    def test_live_trial_extraction_api(self):
        """Verify the trial parser verification dynamically parses custom regex config configurations."""
        raw_text = "Invoice Number: INV-9901\nTotal: $1,250.00"
        rules = {
            "required_fields": ["invoice_number", "total"],
            "fields": {
                "invoice_number": {
                    "anchor_keyword": "Invoice Number:",
                    "search_direction": "relative_right",
                    "window_characters": 80,
                    "regex_pattern": "([A-Z0-9-]+)"
                },
                "total": {
                    "anchor_keyword": "Total:",
                    "search_direction": "relative_right",
                    "window_characters": 80,
                    "regex_pattern": "([0-9.,$]+)"
                }
            }
        }
        
        payload = {
            "raw_text": raw_text,
            "rules": rules
        }
        
        resp = self.client.post("/api/test-extraction", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        
        self.assertTrue(data["success"])
        self.assertEqual(data["extracted"]["invoice_number"], "INV-9901")
        self.assertEqual(data["extracted"]["total"], 1250.00)
        self.assertEqual(data["confidence"], 1.0)

    def test_approve_and_activate_template_api(self):
        """Verify endpoint approves draft templates and updates SQLite active lock status."""
        layout_hash = "globex_hash"
        
        # Store draft rule initially
        db.save_layout_rule(
            layout_hash=layout_hash,
            supplier_name="Globex",
            document_type="pdf",
            status="pending_approval",
            extraction_rules={"fields": {}}
        )
        
        approve_payload = {
            "supplier_name": "Globex Corp",
            "document_type": "pdf",
            "rules": {
                "required_fields": ["invoice_number"],
                "fields": {
                    "invoice_number": {
                        "anchor_keyword": "INV:",
                        "search_direction": "relative_right",
                        "window_characters": 80,
                        "regex_pattern": "(\\d+)"
                    }
                }
            }
        }
        
        resp = self.client.post(f"/api/approve/{layout_hash}", json=approve_payload)
        self.assertEqual(resp.status_code, 200)
        
        # Verify stored rule in SQLite transitioned to active
        details = db.get_rule_details(layout_hash)
        self.assertEqual(details["status"], "active")
        self.assertEqual(details["supplier_name"], "Globex Corp")
        
        rules = json.loads(details["extraction_rules"])
        self.assertEqual(rules["fields"]["invoice_number"]["anchor_keyword"], "INV:")


if __name__ == "__main__":
    unittest.main()
