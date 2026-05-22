"""
Knowledge base utilities.

Covers:
  - ERP product CSV loader (maps columns via v3/knowledge/product_mapping.json)
  - PROMPT.md generator (merges live DB knowledge + PROMPT_BASE.md guidelines)
"""

import csv
import json
from datetime import date
from pathlib import Path

from core.invoices.extractor import (
    upsert_item_code, get_known_customers, get_item_codes,
)

_V3_ROOT     = Path(__file__).resolve().parent.parent.parent
_KB_DIR      = _V3_ROOT / "knowledge"
_PRODUCTS_DIR = _KB_DIR / "products"
_MAPPING_PATH = _KB_DIR / "product_mapping.json"
_BASE_MD      = _KB_DIR / "PROMPT_BASE.md"
_PROMPT_MD    = _KB_DIR / "PROMPT.md"

# ==============================================================================
# ERP PRODUCT CSV LOADER
# ==============================================================================

_DEFAULT_MAPPING = {
    "sku_col":         None,
    "description_col": "Description",
    "unit_price_col":  None,
    "uom_col":         None,
}

def _load_mapping() -> dict:
    if _MAPPING_PATH.exists():
        try:
            with open(_MAPPING_PATH, "r", encoding="utf-8") as f:
                m = json.load(f)
            return {**_DEFAULT_MAPPING, **m}
        except Exception as e:
            print(f"[knowledge_base] Warning: could not read product_mapping.json: {e}")
    return dict(_DEFAULT_MAPPING)


def load_product_csvs() -> int:
    """Read all CSV files in v3/knowledge/products/ and upsert into item_codes table.
    Returns the number of rows loaded.
    """
    if not _PRODUCTS_DIR.exists():
        return 0

    mapping = _load_mapping()
    sku_col   = mapping.get("sku_col")
    desc_col  = mapping.get("description_col")
    price_col = mapping.get("unit_price_col")
    uom_col   = mapping.get("uom_col")

    if not desc_col:
        print("[knowledge_base] product_mapping.json missing 'description_col' — skipping CSV load.")
        return 0

    total = 0
    csv_files = list(_PRODUCTS_DIR.glob("*.csv"))
    if not csv_files:
        return 0

    for csv_path in csv_files:
        try:
            with open(csv_path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    desc = (row.get(desc_col) or "").strip()
                    if not desc:
                        continue
                    sku   = (row.get(sku_col) or "").strip() if sku_col else None
                    price_raw = (row.get(price_col) or "").strip() if price_col else None
                    uom   = (row.get(uom_col) or "").strip() if uom_col else None
                    price: float | None = None
                    if price_raw:
                        try:
                            price = float(price_raw.replace("$", "").replace(",", ""))
                        except ValueError:
                            pass
                    upsert_item_code(sku or None, desc, price, uom or None, source="erp")
                    total += 1
            print(f"[knowledge_base] Loaded {csv_path.name} ({total} rows so far)")
        except Exception as e:
            print(f"[knowledge_base] Warning: failed to load {csv_path.name}: {e}")

    return total


# ==============================================================================
# PROMPT.md GENERATOR
# ==============================================================================

def generate_prompt_md() -> None:
    """Build and write v3/knowledge/PROMPT.md from DB knowledge + PROMPT_BASE.md."""
    _KB_DIR.mkdir(parents=True, exist_ok=True)

    sections: list[str] = []

    sections.append(f"# Invoice Extraction Knowledge Base\n\n_Auto-generated {date.today().isoformat()}. Do not edit directly — regenerated after each confirmed invoice._\n")

    # Known customers
    customers = get_known_customers(min_confirmed=2)
    if customers:
        sections.append("## Known Customers\n")
        header = "| Name | Email | ABN | Address | Confirmed |\n|------|-------|-----|---------|-----------|"
        rows = []
        for c in customers:
            rows.append(
                f"| {c.get('customer_name') or ''} "
                f"| {c.get('customer_email') or ''} "
                f"| {c.get('customer_abn') or ''} "
                f"| {(c.get('customer_address') or '')[:40]} "
                f"| {c.get('confirmed_count', 0)} |"
            )
        sections.append(header + "\n" + "\n".join(rows) + "\n")
    else:
        sections.append("## Known Customers\n\n_No confirmed customers yet._\n")

    # ERP item codes
    erp_items = get_item_codes(source_filter="erp")
    if erp_items:
        sections.append("## ERP Product Codes\n")
        header = "| SKU | Description | Unit Price | UOM |\n|-----|-------------|------------|-----|"
        rows = []
        for item in erp_items[:200]:
            price_str = f"${item['unit_price']:.2f}" if item.get("unit_price") else ""
            rows.append(
                f"| {item.get('sku') or ''} "
                f"| {item.get('description', '')} "
                f"| {price_str} "
                f"| {item.get('uom') or ''} |"
            )
        sections.append(header + "\n" + "\n".join(rows) + "\n")

    # LLM-learned item codes (confirmed ≥ 2)
    conf_items = get_item_codes(min_confirmed=2)
    llm_items = [i for i in conf_items if i.get("source") != "erp"]
    if llm_items:
        sections.append("## Learned Item Codes (confirmed ≥ 2 invoices)\n")
        header = "| SKU | Description | Unit Price | UOM | Confirmed |\n|-----|-------------|------------|-----|-----------|"
        rows = []
        for item in llm_items[:100]:
            price_str = f"${item['unit_price']:.2f}" if item.get("unit_price") else ""
            rows.append(
                f"| {item.get('sku') or ''} "
                f"| {item.get('description', '')} "
                f"| {price_str} "
                f"| {item.get('uom') or ''} "
                f"| {item.get('confirmed_count', 0)} |"
            )
        sections.append(header + "\n" + "\n".join(rows) + "\n")

    # Static base guidelines
    if _BASE_MD.exists():
        base_content = _BASE_MD.read_text(encoding="utf-8").strip()
        sections.append("## Extraction Guidelines\n\n" + base_content + "\n")

    _PROMPT_MD.write_text("\n---\n\n".join(sections), encoding="utf-8")
    print(f"[knowledge_base] PROMPT.md updated ({_PROMPT_MD})")
