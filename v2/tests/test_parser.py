import sys
import unittest
from pathlib import Path

# Ensure v2 root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from core import parser


class TestParserModule(unittest.TestCase):

    def test_pdf_layout_fingerprinting(self):
        """Verify that layout fingerprint hashes ignore dynamic variables and match structural templates."""
        raw_text_1 = """
        TAX INVOICE
        Acme Energy Corporation
        Date: 2026-05-19
        Invoice Number: INV-8827931
        ABN: 11 222 333 444
        Bill To: Joel Wood
        
        Subtotal: $450.00
        GST: $45.00
        Total Due: $495.00
        Email: billing@acmeenergy.com
        """
        
        raw_text_2 = """
        TAX INVOICE
        Acme Energy Corporation
        Date: 2026-06-12
        Invoice Number: INV-9938812
        ABN: 99 888 777 666
        Bill To: Bob Vance
        
        Subtotal: $1,250.00
        GST: $125.00
        Total Due: $1,375.00
        Email: customer.care@acmeenergy.com
        """
        
        hash_1 = parser.generate_layout_hash(raw_text_1, "pdf")
        hash_2 = parser.generate_layout_hash(raw_text_2, "pdf")
        
        # Hashing static word sequences should produce identical results despite different dates, ABNs, and emails
        self.assertEqual(hash_1, hash_2)

    def test_spreadsheet_layout_fingerprinting(self):
        """Verify header columns fingerprint generation for sheets."""
        sheet_text = """
        [Sheet: Sheet1]
        Invoice No\tDate\tCustomer Name\tTotal Amount\tStatus
        INV-101\t2026-01-01\tAcme Corp\t$100.00\tPaid
        INV-102\t2026-01-02\tGlobex Inc\t$250.00\tUnpaid
        """
        
        sheet_text_diff_values = """
        [Sheet: Sheet1]
        Invoice No\tDate\tCustomer Name\tTotal Amount\tStatus
        INV-999\t2026-12-31\tVance Refrig\t$5,000.00\tOverdue
        """
        
        hash_1 = parser.generate_layout_hash(sheet_text, "excel")
        hash_2 = parser.generate_layout_hash(sheet_text_diff_values, "excel")
        
        self.assertEqual(hash_1, hash_2)

    def test_proximity_anchored_field_parsing(self):
        """Verify visual proximity anchored matching confines regex execution and normalises output values."""
        raw_text = """
        ACME CORPORATION INVOICE
        Phone: (02) 9988 7766  ABN: 55 123 456 789
        
        Invoice Number: INV-2026-990812
        Date of Issue: 2026-05-19
        
        Total Amount Paid: $1,540.50
        """
        
        rules = {
            "required_fields": ["invoice_number", "total_amount"],
            "fields": {
                "invoice_number": {
                    "anchor_keyword": "invoice number:",
                    "search_direction": "relative_right",
                    "window_characters": 80,
                    "regex_pattern": r"([A-Z0-9-]+)"
                },
                "abn": {
                    "anchor_keyword": "abn:",
                    "search_direction": "relative_right",
                    "window_characters": 80,
                    "regex_pattern": r"([\d\s]+)"
                },
                "total_amount": {
                    "anchor_keyword": "total amount paid:",
                    "search_direction": "relative_right",
                    "window_characters": 80,
                    "regex_pattern": r"([0-9.,$]+)"
                }
            }
        }
        
        p = parser.DeterministicParser(raw_text, rules)
        extracted, confidence = p.extract_fields()
        
        # Confidence score should be 1.0 since all required fields match
        self.assertEqual(confidence, 1.0)
        
        # Verify invoice number value matching
        self.assertEqual(extracted["invoice_number"], "INV-2026-990812")
        
        # Verify ABN is correctly normalized (stripping spaces)
        self.assertEqual(extracted["abn"], "55123456789")
        
        # Verify total amount paid is normalized to a clean float
        self.assertEqual(extracted["total_amount"], 1540.50)

    def test_spreadsheet_tabular_line_items_parsing(self):
        """Verify tab-separated grid parsing for line items."""
        sheet_text = """
        Qty\tDescription\tUnit Price\tTotal Due
        2\tIndustrial widgets type A\t$150.00\t$300.00
        5\tEco subassemblies\t$40.00\t$200.00
        """
        
        rules = {
            "fields": {},
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
        
        p = parser.DeterministicParser(sheet_text, rules)
        extracted, _ = p.extract_fields()
        
        self.assertIn("line_items", extracted)
        line_items = extracted["line_items"]
        
        self.assertEqual(len(line_items), 2)
        
        self.assertEqual(line_items[0]["quantity"], 2.0)
        self.assertEqual(line_items[0]["description"], "Industrial widgets type A")
        self.assertEqual(line_items[0]["unit_price"], 150.00)
        self.assertEqual(line_items[0]["total"], 300.00)
        
        self.assertEqual(line_items[1]["quantity"], 5.0)
        self.assertEqual(line_items[1]["description"], "Eco subassemblies")
        self.assertEqual(line_items[1]["unit_price"], 40.00)
        self.assertEqual(line_items[1]["total"], 200.00)


if __name__ == "__main__":
    unittest.main()
