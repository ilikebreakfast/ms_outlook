import sys
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

# Ensure v2 root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from core import bootstrapper
from database import db


class TestBootstrapperModule(unittest.TestCase):

    def setUp(self):
        """Redirect database path to temporary file for clean testing."""
        import tempfile
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp_dir.name) / "test_pipeline.db"
        db.DB_DIR = Path(self.temp_dir.name)
        db.init_db()

    def tearDown(self):
        """Close db handles and remove temp files."""
        self.temp_dir.cleanup()

    @patch("core.bootstrapper._query_deepseek")
    def test_bootstrap_template_success(self, mock_query):
        """Verify successful template rule generation, Markdown cleaning, and SQL serialization."""
        
        # Mock returns Markdown-wrapped JSON response from LLM
        mock_query.return_value = """
        ```json
        {
          "supplier_name": "Globex Corp",
          "document_type": "pdf",
          "rules": {
            "required_fields": ["invoice_number", "total_amount"],
            "fields": {
              "invoice_number": {
                "anchor_keyword": "Invoice #:",
                "search_direction": "relative_right",
                "window_characters": 80,
                "regex_pattern": "([A-Z0-9-]+)"
              },
              "total_amount": {
                "anchor_keyword": "Total Paid:",
                "search_direction": "relative_right",
                "window_characters": 60,
                "regex_pattern": "([0-9.,]+)"
              }
            }
          }
        }
        ```
        """
        
        # Temporarily enable a fake key so query executes
        with patch("core.bootstrapper.DEEPSEEK_API_KEY", "fake_api_key"):
            result = bootstrapper.bootstrap_template(
                raw_text="Mock invoice text from Globex Corp",
                document_type="pdf",
                layout_hash="globex123hash"
            )
            
            self.assertIsNotNone(result)
            supplier_name, rules = result
            self.assertEqual(supplier_name, "Globex Corp")
            self.assertIn("invoice_number", rules["fields"])
            
            # Verify record was stored in sqlite db under pending_approval status
            details = db.get_rule_details("globex123hash")
            self.assertIsNotNone(details)
            self.assertEqual(details["supplier_name"], "Globex Corp")
            self.assertEqual(details["status"], "pending_approval")


if __name__ == "__main__":
    unittest.main()
