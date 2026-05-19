import re
import hashlib
import logging
from typing import Optional, Dict, Any, List, Tuple

log = logging.getLogger(__name__)

# Common static layout defining keywords to isolate structure and ignore variables
LAYOUT_VOCABULARY = {
    "invoice", "tax", "date", "abn", "number", "no", "total", "subtotal", "due",
    "gst", "vat", "payable", "qty", "quantity", "price", "unit", "description",
    "item", "amount", "bill", "ship", "to", "email", "phone", "bank", "bsb",
    "account", "payment", "terms", "method", "issue", "received", "supplied",
    "address", "company", "corporation", "ltd", "pty", "inc", "service",
    "charge", "fee", "rate", "hours", "days", "balance", "ordered", "delivered"
}


def generate_layout_hash(raw_text: str, file_type: str) -> str:
    """
    Generate a layout fingerprint SHA-256 signature for a document.
    file_type: 'pdf' | 'excel' | 'csv' | 'image'
    """
    text = raw_text.lower()
    
    if file_type in ("pdf", "image"):
        # 1. Strip all digits and punctuation
        text = re.sub(r"[^\w\s]", " ", text)
        text = re.sub(r"\d+", " ", text)
        
        # 2. Extract words
        words = re.findall(r"\b[a-z]{2,20}\b", text)
        
        # 3. Filter using Layout Vocabulary to isolate structural labels
        filtered_words = [w for w in words if w in LAYOUT_VOCABULARY]
        
        # Build layout signature from layout structural words (up to 100)
        static_sequence = " ".join(filtered_words[:100])
        return hashlib.sha256(static_sequence.encode("utf-8")).hexdigest()
        
    elif file_type in ("excel", "csv"):
        # Column headers fingerprinting: extract sheet header column names
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        
        # Scan first few rows to find tabular-looking header columns
        header_cells: List[str] = []
        for line in lines[:5]:
            if line.count("\t") >= 2:  # Found a grid-like row
                cells = [c.strip().lower() for c in line.split("\t") if c.strip()]
                # Strip out pure numbers from column header signature
                cells = [c for c in cells if not re.match(r"^\d+$", c)]
                if len(cells) >= 3:
                    header_cells = cells
                    break
                    
        # If no tab-delimited cells found, use first few trimmed lines as schema sequence
        if not header_cells:
            header_cells = [line[:50].lower() for line in lines[:3]]
            
        schema_sequence = "|".join(header_cells)
        return hashlib.sha256(schema_sequence.encode("utf-8")).hexdigest()
        
    else:
        raise ValueError(f"Unsupported file type for fingerprinting: {file_type}")


class DeterministicParser:
    """Local, ultra-fast rule engine driven by visual anchors and local window regexes."""

    def __init__(self, raw_text: str, rules: Dict[str, Any]):
        self.raw_text = raw_text
        self.rules = rules

    def extract_fields(self) -> Tuple[Dict[str, Any], float]:
        """
        Extract document fields based on anchored mapping rules.
        Returns:
            (extracted_fields, confidence_score)
        """
        extracted = {}
        required_fields = self.rules.get("required_fields", [])
        matched_required = 0
        total_required = len(required_fields)

        field_rules = self.rules.get("fields", {})
        for field_name, rule in field_rules.items():
            value = self._parse_field(field_name, rule)
            extracted[field_name] = value
            
            if field_name in required_fields:
                if value is not None:
                    matched_required += 1

        # Calculate rule matching confidence
        confidence = (matched_required / total_required) if total_required > 0 else 1.0
        
        # Handle line items if tabular configuration is active
        line_rules = self.rules.get("line_items")
        if line_rules:
            extracted["line_items"] = self._parse_line_items(line_rules)
            
        return extracted, round(confidence, 2)

    def _parse_field(self, field_name: str, rule: Dict[str, Any]) -> Optional[Any]:
        """Extract a single field using visual proximity anchors and local regex windows."""
        anchor = rule.get("anchor_keyword")
        regex = rule.get("regex_pattern")
        
        if not regex:
            return None

        # Standardise regex single-escape sequence
        try:
            compiled_re = re.compile(regex, re.IGNORECASE)
        except re.error as e:
            log.warning(f"Invalid regex rule pattern for '{field_name}': {regex}. Error: {e}")
            return None

        # 1. If no anchor keyword is defined, default to global fallback regex matching
        if not anchor:
            match = compiled_re.search(self.raw_text)
            if match:
                val = match.group(1).strip() if match.groups() else match.group(0).strip()
                return self._normalise_field_value(field_name, val)
            return None

        # 2. visual proximity anchor routing
        anchor_lower = anchor.lower()
        text_lower = self.raw_text.lower()
        
        # Locate visual anchor in document
        idx = text_lower.find(anchor_lower)
        if idx == -1:
            log.debug(f"Anchor '{anchor}' not found for field '{field_name}'.")
            return None

        direction = rule.get("search_direction", "relative_right")
        window_size = int(rule.get("window_characters", 100))

        # Crop visual search window around anchor
        if direction in ("relative_right", "local_window"):
            start = idx + len(anchor)
            search_window = self.raw_text[start : start + window_size]
        elif direction == "relative_below":
            # Search after the line containing the anchor
            start = idx + len(anchor)
            lines_after = self.raw_text[start : start + window_size]
            search_window = lines_after
        else:
            search_window = self.raw_text[idx : idx + window_size]

        # Extract value strictly inside the visual search window
        match = compiled_re.search(search_window)
        if match:
            val = match.group(1).strip() if match.groups() else match.group(0).strip()
            return self._normalise_field_value(field_name, val)

        return None

    def _parse_line_items(self, line_rules: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract line items based on tabular column indices or text-row matching."""
        strategy = line_rules.get("strategy", "tabular_columns")
        items = []
        
        if strategy == "tabular_columns":
            columns = line_rules.get("columns", {})
            if not columns:
                return []
                
            # Iterate lines containing tab delimiters
            for line in self.raw_text.splitlines():
                if line.count("\t") >= 2:
                    cells = [c.strip() for c in line.split("\t")]
                    
                    # Ensure it is a data row by validating that numerical columns contain digit characters
                    # (this filters out the column header row like "Qty", "Description", "Price")
                    quantity_col = columns.get("quantity", {})
                    price_col = columns.get("unit_price", {})
                    total_col = columns.get("total", {})
                    
                    is_header_or_noise = False
                    for col_def in (quantity_col, price_col, total_col):
                        idx = col_def.get("col_index")
                        if idx is not None and idx < len(cells) and cells[idx]:
                            # If a numeric cell has no digit characters, it is headers or labels
                            if not re.search(r"\d", cells[idx]):
                                is_header_or_noise = True
                                break
                                
                    if is_header_or_noise:
                        continue
                        
                    # Construct row dictionary based on mapped column indices
                    row_data = {}
                    has_data = False
                    for key, col_def in columns.items():
                        idx = col_def.get("col_index")
                        if idx is not None and idx < len(cells):
                            cell_val = cells[idx]
                            row_data[key] = cell_val if cell_val else None
                            if cell_val:
                                has_data = True
                                
                    if has_data and row_data.get("description"):
                        # Normalise line item values (e.g. quantity, unit_price, total)
                        if "quantity" in row_data and row_data["quantity"]:
                            row_data["quantity"] = self._clean_numeric(row_data["quantity"])
                        if "unit_price" in row_data and row_data["unit_price"]:
                            row_data["unit_price"] = self._clean_numeric(row_data["unit_price"])
                        if "total" in row_data and row_data["total"]:
                            row_data["total"] = self._clean_numeric(row_data["total"])
                        items.append(row_data)
                        
        return items

    def _normalise_field_value(self, field_name: str, value: str) -> Any:
        """Standardise outputs like ABN normalization or float cleaning."""
        cleaned = value.strip()
        
        # ABN auto-normalization (11 digits, strip all spaces/punctuation)
        if "abn" in field_name.lower():
            abn_digits = re.sub(r"\D", "", cleaned)
            if len(abn_digits) == 11:
                return abn_digits
            return cleaned
            
        # Amount/Total normalization to float
        if any(w in field_name.lower() for w in ("amount", "total", "subtotal", "tax", "gst")):
            try:
                return self._clean_numeric(cleaned)
            except ValueError:
                return cleaned
                
        return cleaned

    def _clean_numeric(self, val_str: str) -> float:
        """Strip dollar signs, currency symbols, and convert string to float."""
        clean = re.sub(r"[^\d.\-]", "", val_str)
        return float(clean) if clean else 0.0
