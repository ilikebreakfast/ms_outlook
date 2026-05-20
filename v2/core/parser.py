import re
import hashlib
import logging
from datetime import datetime
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

    def __init__(self, raw_text: str, rules: Dict[str, Any], pdf_path: Optional[str] = None):
        self.raw_text = raw_text
        self.rules = rules
        if pdf_path:
            from pathlib import Path
            self.pdf_path = Path(pdf_path)
        else:
            self.pdf_path = None

    def extract_fields(self) -> Tuple[Dict[str, Any], float]:
        """
        Extract document fields based on anchored mapping rules.
        Returns:
            (extracted_fields, confidence_score)
        """
        extracted = {}
        field_rules = self.rules.get("fields", {})
        
        total_weight = 0.0
        weighted_score = 0.0

        for field_name, rule in field_rules.items():
            # Skip email domain rule processing if present to avoid manual config conflict
            if field_name == "customer_email_domain":
                continue
                
            value = self._parse_field(field_name, rule)
            extracted[field_name] = value
            
            # Confidence weighting calculation
            # Standard weight is 1.0 if not specified
            weight = float(rule.get("confidence_weight", 1.0))
            if weight > 0:
                total_weight += weight
                if value is not None and str(value).strip() != "":
                    weighted_score += weight

        # Always automatically derive customer_email_domain from customer_email (no manual selection required)
        if extracted.get("customer_email"):
            email = str(extracted["customer_email"]).strip()
            if "@" in email:
                extracted["customer_email_domain"] = email[email.find("@"):].strip().lower()
            else:
                extracted["customer_email_domain"] = ""
        else:
            extracted["customer_email_domain"] = ""

        # Calculate weighted confidence
        confidence = (weighted_score / total_weight) if total_weight > 0.0 else 1.0
        
        # Handle line items if tabular configuration is active
        line_rules = self.rules.get("line_items")
        if line_rules:
            extracted["line_items"] = self._parse_line_items(line_rules)
            
        return extracted, round(confidence, 2)

    def _parse_coordinate_field(self, field_name: str, rule: Dict[str, Any]) -> Optional[Any]:
        """Extract a single field utilizing visual drag-and-draw coordinate cropping bounding boxes."""
        if not self.pdf_path or not self.pdf_path.exists():
            log.warning(f"No physical PDF file available for coordinate-based extraction of field '{field_name}'.")
            return None
            
        page_num = int(rule.get("page", 1))
        box = rule.get("box")  # [x_pct, y_pct, w_pct, h_pct]
        if not box:
            return None
            
        x_pct, y_pct, w_pct, h_pct = box
        
        # 1. Extract from native PDF using pdfplumber
        try:
            import pdfplumber
            with pdfplumber.open(self.pdf_path) as pdf:
                if page_num <= len(pdf.pages):
                    page = pdf.pages[page_num - 1]
                    width = page.width
                    height = page.height
                    
                    x0 = x_pct * width
                    y0 = y_pct * height
                    x1 = (x_pct + w_pct) * width
                    y1 = (y_pct + h_pct) * height
                    
                    cropped = page.crop((x0, y0, x1, y1))
                    text = cropped.extract_text()
                    if text:
                        text = text.strip()
                        regex = rule.get("regex_pattern")
                        if regex:
                            try:
                                compiled_re = re.compile(regex, re.IGNORECASE)
                                match = compiled_re.search(text)
                                if match:
                                    text = match.group(1).strip() if match.groups() else match.group(0).strip()
                            except Exception:
                                pass
                        return self._normalise_field_value(field_name, text)
        except Exception as e:
            log.error(f"pdfplumber coordinate field extraction failed for '{field_name}': {e}")
            
        # 2. Extract from scanned image fallback using PyMuPDF and Tesseract OCR
        try:
            import fitz
            from PIL import Image
            import pytesseract
            
            doc = fitz.open(str(self.pdf_path))
            if page_num <= len(doc):
                page = doc[page_num - 1]
                width = page.rect.width
                height = page.rect.height
                
                x0 = x_pct * width
                y0 = y_pct * height
                x1 = (x_pct + w_pct) * width
                y1 = (y_pct + h_pct) * height
                
                clip_rect = fitz.Rect(x0, y0, x1, y1)
                mat = fitz.Matrix(3.0, 3.0)
                pix = page.get_pixmap(matrix=mat, clip=clip_rect)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                
                text = pytesseract.image_to_string(img, lang="eng").strip()
                if text:
                    regex = rule.get("regex_pattern")
                    if regex:
                        try:
                            compiled_re = re.compile(regex, re.IGNORECASE)
                            match = compiled_re.search(text)
                            if match:
                                text = match.group(1).strip() if match.groups() else match.group(0).strip()
                        except Exception:
                            pass
                    return self._normalise_field_value(field_name, text)
        except Exception as e:
            log.error(f"PyMuPDF OCR coordinate field extraction fallback failed for '{field_name}': {e}")
            
        return None

    def _parse_field(self, field_name: str, rule: Dict[str, Any]) -> Optional[Any]:
        """Extract a single field using visual proximity anchors and local regex windows."""
        # Route to coordinate strategy if active
        if rule.get("strategy") == "coordinate":
            return self._parse_coordinate_field(field_name, rule)

        # Check if we should use filename as the value source
        if rule.get("use_filename"):
            fn_match = re.match(r"^\[FILENAME:\s*(.*?)\]", self.raw_text)
            if fn_match:
                from pathlib import Path
                filename = fn_match.group(1).strip()
                val = Path(filename).stem
                
                # Apply regex pattern to carve out value if provided
                regex = rule.get("regex_pattern")
                if regex:
                    try:
                        compiled_re = re.compile(regex, re.IGNORECASE)
                        match = compiled_re.search(val)
                        if match:
                            val = match.group(1).strip() if match.groups() else match.group(0).strip()
                    except Exception as e:
                        log.warning(f"Invalid regex for filename match on '{field_name}': {regex}. Error: {e}")
                return self._normalise_field_value(field_name, val)
            return None

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
                        idx = col_def.get("col_index") if isinstance(col_def, dict) else col_def
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
                        idx = col_def.get("col_index") if isinstance(col_def, dict) else col_def
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
                        
        elif strategy == "coordinate_table":
            if not self.pdf_path or not self.pdf_path.exists():
                log.warning("No physical PDF file available for coordinate-based table extraction.")
                return []
                
            page_num = int(line_rules.get("page", 1))
            box = line_rules.get("box")  # [x_pct, y_pct, w_pct, h_pct]
            if not box:
                return []
                
            x_pct, y_pct, w_pct, h_pct = box
            columns = line_rules.get("columns", {})
            if not columns:
                return []
                
            rows = []
            
            # 1. Native table cropping via pdfplumber
            try:
                import pdfplumber
                with pdfplumber.open(self.pdf_path) as pdf:
                    if page_num <= len(pdf.pages):
                        page = pdf.pages[page_num - 1]
                        width = page.width
                        height = page.height
                        
                        x0 = x_pct * width
                        y0 = y_pct * height
                        x1 = (x_pct + w_pct) * width
                        y1 = (y_pct + h_pct) * height
                        
                        cropped = page.crop((x0, y0, x1, y1))
                        
                        # 1a. Try structured table layout first
                        tbls = cropped.extract_tables()
                        if tbls:
                            for tbl in tbls:
                                for r in tbl:
                                    if any(r):
                                        cells = [str(c).strip() if c is not None else "" for c in r]
                                        rows.append(cells)
                                        
                        # 1b. Fallback to coordinate horizontal gap-spacing words clustering (great for vertical lines absence)
                        if not rows or all(len(r) <= 1 for r in rows):
                            # Clear single-column/failed table parses first
                            rows = []
                            words = cropped.extract_words()
                            if words:
                                rows = self._extract_rows_from_words(words)
                            else:
                                text_lines = cropped.extract_text(layout=True)
                                if text_lines:
                                    for line in text_lines.splitlines():
                                        if line.strip():
                                            cells = re.split(r'\t|\s{2,}', line.strip())
                                            rows.append(cells)
            except Exception as e:
                log.error(f"pdfplumber table extraction failed: {e}")
                
            # 2. Scanned OCR fallback table cropping via PyMuPDF and Tesseract
            if not rows:
                try:
                    import fitz
                    from PIL import Image
                    import pytesseract
                    
                    doc = fitz.open(str(self.pdf_path))
                    if page_num <= len(doc):
                        page = doc[page_num - 1]
                        width = page.rect.width
                        height = page.rect.height
                        
                        x0 = x_pct * width
                        y0 = y_pct * height
                        x1 = (x_pct + w_pct) * width
                        y1 = (y_pct + h_pct) * height
                        
                        clip_rect = fitz.Rect(x0, y0, x1, y1)
                        mat = fitz.Matrix(3.0, 3.0)
                        pix = page.get_pixmap(matrix=mat, clip=clip_rect)
                        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                        
                        text = pytesseract.image_to_string(img, lang="eng").strip()
                        if text:
                            for line in text.splitlines():
                                if line.strip():
                                    cells = re.split(r'\t|\s{2,}', line.strip())
                                    rows.append(cells)
                except Exception as e:
                    log.error(f"PyMuPDF OCR table fallback failed: {e}")
                    
            # 3. Process, clean, and map tabular rows to standard columns
            for cells in rows:
                is_header_or_noise = False
                for key in ("quantity", "price", "unit_price", "subtotal", "tax", "total"):
                    col_def = columns.get(key)
                    if col_def is not None:
                        idx = col_def.get("col_index") if isinstance(col_def, dict) else col_def
                        if idx is not None and idx >= 0 and idx < len(cells) and cells[idx]:
                            if not re.search(r"\d", cells[idx]):
                                is_header_or_noise = True
                                break
                if is_header_or_noise:
                    continue
                    
                row_data = {}
                has_data = False
                for key, col_def in columns.items():
                    idx = col_def.get("col_index") if isinstance(col_def, dict) else col_def
                    if idx is not None and idx >= 0 and idx < len(cells):
                        cell_val = cells[idx].strip()
                        row_data[key] = cell_val if cell_val else None
                        if cell_val:
                            has_data = True
                    else:
                        row_data[key] = None
                        
                if has_data and (row_data.get("product_name") or row_data.get("description") or row_data.get("product_code")):
                    # Ensure standard backward-compatible aliases exist
                    if "description" not in row_data or not row_data["description"]:
                        row_data["description"] = row_data.get("product_name")
                    if "unit_price" not in row_data or not row_data["unit_price"]:
                        row_data["unit_price"] = row_data.get("price")
                        
                    # Clean numerical inputs
                    for k in ("quantity", "price", "unit_price", "subtotal", "tax", "total"):
                        if k in row_data and row_data[k]:
                            try:
                                row_data[k] = self._clean_numeric(row_data[k])
                            except Exception:
                                row_data[k] = 0.0
                                
                    items.append(row_data)
                    
        return items

    def _extract_rows_from_words(self, words: List[Dict[str, Any]], gap_threshold: float = 10.0) -> List[List[str]]:
        """
        Group words into visual rows and columns by physical spatial alignment coordinates.
        This provides perfect column parsing when vertical separator lines are absent.
        """
        if not words:
            return []
            
        # Group words vertically if their top coordinates are close (within 4 points threshold)
        lines = []
        sorted_words = sorted(words, key=lambda w: (w["top"], w["x0"]))
        
        for w in sorted_words:
            added = False
            for line in lines:
                line_avg_top = sum(item["top"] for item in line) / len(line)
                if abs(w["top"] - line_avg_top) < 4.0:
                    line.append(w)
                    added = True
                    break
            if not added:
                lines.append([w])
                
        # Sort lines top-to-bottom
        lines = sorted(lines, key=lambda line: sum(item["top"] for item in line) / len(line))
        
        table_rows = []
        for line in lines:
            # Sort words horizontally left-to-right
            line_words = sorted(line, key=lambda w: w["x0"])
            if not line_words:
                continue
                
            cells = []
            current_cell_words = [line_words[0]]
            
            for next_word in line_words[1:]:
                prev_word = current_cell_words[-1]
                # If horizontal gap is larger than threshold, split column!
                if next_word["x0"] - prev_word["x1"] >= gap_threshold:
                    cell_text = " ".join(w["text"] for w in current_cell_words).strip()
                    cells.append(cell_text)
                    current_cell_words = [next_word]
                else:
                    current_cell_words.append(next_word)
                    
            if current_cell_words:
                cell_text = " ".join(w["text"] for w in current_cell_words).strip()
                cells.append(cell_text)
                
            if any(cells):
                table_rows.append(cells)
                
        return table_rows

        return items

    def _normalise_date(self, val: str) -> str:
        """Strip trailing timezone AEDT/UTC and timestamps, normalize to YYYY-MM-DD."""
        cleaned = re.sub(r'\b(UTC|GMT|AEST|AEDT|EST|EDT|PST|PDT)\b', '', val, flags=re.IGNORECASE)
        cleaned = re.sub(r'T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?', ' ', cleaned)
        cleaned = re.sub(r'\b\d{1,2}:\d{2}(:\d{2})?\s*(AM|PM)?\b', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'\s+[+-]\d{4}\b', '', cleaned)
        
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        cleaned = cleaned.strip(',.-/')
        if not cleaned:
            return val
            
        formats = [
            "%Y-%m-%d", "%Y/%m/%d",
            "%d-%m-%Y", "%d/%m/%Y",
            "%d-%b-%Y", "%d-%B-%Y",
            "%b %d, %Y", "%B %d, %Y",
            "%d %b %Y", "%d %B %Y",
            "%d-%b-%y", "%d-%B-%y",
            "%y-%m-%d", "%y/%m/%d",
            "%d-%m-%y", "%d/%m/%y"
        ]
        for fmt in formats:
            try:
                dt = datetime.strptime(cleaned, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
        return cleaned

    def _normalise_field_value(self, field_name: str, value: str) -> Any:
        """Standardise outputs like ABN normalization or float cleaning."""
        cleaned = value.strip()
        
        # Date normalization
        if "date" in field_name.lower():
            return self._normalise_date(cleaned)
            
        # ABN auto-normalization (11 digits, strip all spaces/punctuation)
        if "abn" in field_name.lower():
            abn_digits = re.sub(r"\D", "", cleaned)
            if len(abn_digits) == 11:
                return abn_digits
            return cleaned
            
        # Email domain normalization (starting from @ inclusive)
        if field_name == "customer_email_domain":
            if "@" in cleaned:
                cleaned = cleaned[cleaned.find("@"):].strip()
            return cleaned.lower()
            
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
