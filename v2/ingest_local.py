import sys
import os
import shutil
import json
import logging
from pathlib import Path

# Ensure v2 root is importable
sys.path.insert(0, str(Path(__file__).parent))

from core.security import validate_attachment, scrub_prompt_injection
from core.extractor import extract_text
from core.parser import generate_layout_hash, DeterministicParser
from core.bootstrapper import bootstrap_template
from database import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ingest_local")

def ingest_local_file(file_path: str):
    path = Path(file_path)
    if not path.is_file():
        log.error(f"File not found: {file_path}")
        return False
        
    db.init_db()
    
    log.info(f"Ingesting local attachment: {path.name}")
    
    # 1. Security Gate scan
    ok, issues = validate_attachment(path)
    if not ok:
        log.warning(f"Security validation warning: {issues}")
        
    # 2. Copy file to local data/attachments if not already inside there
    attachments_dir = Path(__file__).parent / "data" / "attachments"
    attachments_dir.mkdir(parents=True, exist_ok=True)
    
    # Create target date folder similar to pipeline download structure
    import datetime
    folder_name = datetime.datetime.now().strftime("%Y%m%d") + "_local_import"
    target_dir = attachments_dir / folder_name
    target_dir.mkdir(parents=True, exist_ok=True)
    
    target_path = target_dir / path.name
    if target_path.resolve() != path.resolve():
        try:
            shutil.copy2(path, target_path)
            log.info(f"Copied file to attachments store: {target_path}")
        except Exception as e:
            log.error(f"Failed to copy file to attachments store: {e}")
            target_path = path
    else:
        log.info(f"File already located in attachments store.")
        
    # 3. Extract text
    try:
        raw_text, _ = extract_text(target_path)
        raw_text = scrub_prompt_injection(raw_text)
    except Exception as e:
        log.error(f"Text extraction failed: {e}")
        return False
    
    # 4. Save raw text cache so the dashboard text viewer can load it!
    raw_text_dir = Path(__file__).parent / "data" / "raw_text"
    raw_text_dir.mkdir(parents=True, exist_ok=True)
    txt_target = raw_text_dir / f"{target_path.stem}.txt"
    txt_target.write_text(raw_text, encoding="utf-8")
    log.info(f"Cached raw text layout representation at: {txt_target}")
    
    # 5. Generate layout fingerprint signature
    file_type = "pdf" if path.suffix.lower() == ".pdf" else ("excel" if path.suffix.lower() in (".xlsx", ".xls") else "csv")
    layout_hash = generate_layout_hash(raw_text, file_type)
    log.info(f"Established layout signature: {layout_hash}")
    
    # 6. Check classification
    rule_details = db.get_rule_details(layout_hash)
    if rule_details:
        log.info(f"Layout already has a registered status: '{rule_details['status']}'")
        # Ensure it has history log so the dashboard lists it
        db.log_extraction(
            message_id="local_import_msg",
            filename=path.name,
            layout_hash=layout_hash,
            parsed_data=None,
            status="drifted"
        )
        return True
        
    # 7. LLM Bootstrap template rules
    log.info("Triggering LLM template bootstrap rules compilation...")
    bootstrap_res = bootstrap_template(
        raw_text=raw_text,
        document_type=file_type,
        layout_hash=layout_hash
    )
    
    if bootstrap_res:
        supplier_name, rules = bootstrap_res
        log.info(f"Bootstrap compiled successfully for: '{supplier_name}'")
        
        hist_id = db.log_extraction(
            message_id="local_import_msg",
            filename=path.name,
            layout_hash=layout_hash,
            parsed_data=None,
            status="drifted"
        )
        
        # Add to visual operator review queue
        db.add_to_review_queue(
            hist_id,
            f"New layout imported locally for '{supplier_name}'"
        )
        log.info(f"Successfully loaded '{path.name}' into visual dashboard pending list!")
        return True
    else:
        # Fallback bootstrap if LLM key is absent or offline
        log.warning("LLM key missing or offline. Creating a default blank template configuration...")
        fallback_rules = {
            "required_fields": ["invoice_number", "total_amount"],
            "fields": {
                "invoice_number": {"anchor_keyword": "Invoice No:", "search_direction": "relative_right", "window_characters": 80, "regex_pattern": "([A-Z0-9-]+)"},
                "invoice_date": {"anchor_keyword": "Date:", "search_direction": "relative_right", "window_characters": 80, "regex_pattern": "([0-9/-]+)"},
                "abn": {"anchor_keyword": "ABN:", "search_direction": "relative_right", "window_characters": 80, "regex_pattern": "([\\d\\s]+)"},
                "total_amount": {"anchor_keyword": "Total:", "search_direction": "relative_right", "window_characters": 80, "regex_pattern": "([0-9.,$]+)"}
            }
        }
        db.save_layout_rule(
            layout_hash=layout_hash,
            supplier_name=path.stem.replace("_", " ").title(),
            document_type=file_type,
            status="pending_approval",
            extraction_rules=fallback_rules
        )
        hist_id = db.log_extraction(
            message_id="local_import_msg",
            filename=path.name,
            layout_hash=layout_hash,
            parsed_data=None,
            status="drifted"
        )
        db.add_to_review_queue(hist_id, "Seeded default template for manual visual mapping.")
        log.info(f"Successfully seeded '{path.name}' template for visual mapping.")
        return True

if __name__ == "__main__":
    import sys
    target = None
    if len(sys.argv) >= 2:
        target = sys.argv[1].strip().strip('"').strip("'")
    
    # If no target specified or target is empty, use the default v2 data/attachments directory
    if not target or target == "":
        target = str(Path(__file__).parent / "data" / "attachments")
        print(f"No file specified. Scanning default directory: {target}")

    path = Path(target)
    if path.is_dir():
        print(f"Scanning top-level directory for importable files: {path.resolve()}")
        supported_exts = {".pdf", ".xlsx", ".xls", ".csv"}
        importable_files = []
        
        # Ensure directory exists
        path.mkdir(parents=True, exist_ok=True)
        
        for f in path.iterdir():
            if f.is_file() and f.suffix.lower() in supported_exts:
                importable_files.append(f)
                
        if not importable_files:
            print(f"No importable documents (.pdf, .xlsx, .xls, .csv) found in: {path.resolve()}")
            print("Please drop your test files directly into that folder and press Enter to scan again.")
            sys.exit(0)
            
        print(f"Found {len(importable_files)} file(s) to ingest.")
        success_count = 0
        for f in importable_files:
            print(f"\n--- Ingesting {f.name} ---")
            if ingest_local_file(str(f)):
                success_count += 1
        print(f"\n[+] Batch ingestion completed: {success_count}/{len(importable_files)} successfully ingested.")
    else:
        if ingest_local_file(str(path)):
            print("[+] Ingestion successful.")
        else:
            print("[x] Ingestion failed.")
            sys.exit(1)
