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

    def test_date_normalization(self):
        """Verify that various date string formats with/without timestamps normalize correctly."""
        raw_text_template = "Invoice Date: {}"
        rules = {
            "fields": {
                "invoice_date": {
                    "anchor_keyword": "invoice date:",
                    "search_direction": "relative_right",
                    "window_characters": 80,
                    "regex_pattern": r"([^\n]+)"
                }
            }
        }
        
        test_cases = [
            ("23-APR-26 14:30:00", "2026-04-23"),
            ("2026-05-19T21:41:12+10:00", "2026-05-19"),
            ("May 19, 2026 12:00 AM", "2026-05-19"),
            ("19/05/2026 AEDT", "2026-05-19"),
            ("23-Apr-26", "2026-04-23"),
            ("23-APR-2026 UTC", "2026-04-23"),
            ("19-05-2026 15:45 AEDT", "2026-05-19")
        ]
        
        for input_val, expected_val in test_cases:
            p = parser.DeterministicParser(raw_text_template.format(input_val), rules)
            extracted, _ = p.extract_fields()
            self.assertEqual(extracted["invoice_date"], expected_val, f"Failed for input: {input_val}")

    def test_use_filename_extraction(self):
        """Verify that invoice number can be extracted from filename metadata if specified."""
        raw_text = "[FILENAME: PO_7760180.pdf]\n\nSome body text here..."
        
        rules = {
            "fields": {
                "invoice_number": {
                    "use_filename": True,
                    "regex_pattern": r"([0-9]+)"
                }
            }
        }
        
        p = parser.DeterministicParser(raw_text, rules)
        extracted, _ = p.extract_fields()
        self.assertEqual(extracted["invoice_number"], "7760180")

    def test_customer_email_domain_parsing(self):
        """Verify that customer_email_domain is normalized and falls back to customer_email domain if empty."""
        raw_text = "Customer Email: joel@tcfmeat.com.au"
        rules = {
            "fields": {
                "customer_email": {
                    "anchor_keyword": "customer email:",
                    "search_direction": "relative_right",
                    "window_characters": 80,
                    "regex_pattern": r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})"
                },
                "customer_email_domain": {
                    "anchor_keyword": "",
                    "regex_pattern": ""
                }
            }
        }
        p = parser.DeterministicParser(raw_text, rules)
        extracted, _ = p.extract_fields()
        self.assertEqual(extracted["customer_email"], "joel@tcfmeat.com.au")
        self.assertEqual(extracted["customer_email_domain"], "@tcfmeat.com.au")


if __name__ == "__main__":
    unittest.main()
