"""
Management utility for the ms_outlook pipeline.

Commands:
  python manage.py test-template <name> <file> [--show-text]
  python manage.py parse-pdf <file> [--save] [--show-text]
  python manage.py parse-text <template> <txt_file>
  python manage.py list-senders [--days 30]
  python manage.py add-sender <email> [--name NAME] [--template STEM]
  python manage.py list-suggestions
  python manage.py approve-suggestion <email>
  python manage.py review-queue [--all]
  python manage.py resolve-review <queue_id> [--dismiss]
  python manage.py analyze-template <name> [--last N]
  python manage.py health

Examples:
  # Full test: extract from PDF then parse with template
  python manage.py test-template acme invoice.pdf --show-text

  # Step 1 only: extract text from a PDF (prints it, optionally saves to raw_text/)
  python manage.py parse-pdf invoice.pdf
  python manage.py parse-pdf invoice.pdf --save --show-text

  # Step 2 only: run a template against already-extracted text
  python manage.py parse-text acme raw_text/abc123/invoice.txt
  python manage.py parse-text acme raw_text/abc123/invoice.txt --show-text

  python manage.py add-sender billing@acme.com.au --name "ACME Corp" --template acme
  python manage.py list-suggestions
  python manage.py approve-suggestion billing@acme.com.au
  python manage.py review-queue
  python manage.py resolve-review 3
  python manage.py analyze-template acme --last 10
  python manage.py health
"""
import argparse
import json
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Existing commands
# ---------------------------------------------------------------------------

def cmd_test_template(args) -> int:
    template_name: str = args.template_name
    file_path = Path(args.file)

    if not file_path.exists():
        print(f"Error: file not found: {file_path}")
        return 1

    try:
        from pipeline.text_extractor import extract_text, extract_excel_text
        from pipeline.template_parser import (
            _load_template, _extract_field, _extract_line_items,
            _extract_xlsx_line_items, _find_cell_value, parse_xlsx,
        )
        from config.settings import TEMPLATES_DIR, LOW_CONFIDENCE_THRESHOLD
        import openpyxl
    except Exception as e:
        print(f"Error loading pipeline modules: {e}")
        return 1

    template_path = TEMPLATES_DIR / f"{template_name}.yaml"
    if not template_path.exists():
        print(f"Error: template not found: {template_path}")
        print(f"  Available templates: {', '.join(p.stem for p in TEMPLATES_DIR.glob('*.yaml'))}")
        return 1

    tmpl = _load_template(template_name)
    if not tmpl:
        print(f"Error: failed to load template: {template_name}")
        return 1

    is_excel = file_path.suffix.lower() == ".xlsx"

    print(f"\nExtracting text from: {file_path.name}")
    try:
        if is_excel:
            text = extract_excel_text(file_path)
            print(f"Extraction method: Excel (openpyxl) | {len(text)} chars extracted")
        else:
            text, is_native = extract_text(file_path)
            extraction_method = "native PDF" if is_native else "OCR"
            print(f"Extraction method: {extraction_method} | {len(text)} chars extracted")
    except Exception as e:
        print(f"Error extracting text: {e}")
        return 1

    if args.show_text:
        print("\n" + "─" * 60)
        print("EXTRACTED TEXT:")
        print("─" * 60)
        print(text[:3000] + ("..." if len(text) > 3000 else ""))
        print("─" * 60)

    fields = tmpl.get("fields", {})
    fields_xlsx = tmpl.get("fields_xlsx", {})
    required_fields = tmpl.get("required_fields", list(fields.keys()))

    print(f"\nTemplate: {template_name}  |  Customer: {tmpl.get('customer_name', '?')}")
    print(f"Required fields: {', '.join(required_fields)}")
    print()
    print(f"{'Field':<28} {'Status':<6} {'Value'}")
    print("─" * 70)

    results = {}
    if is_excel and fields_xlsx:
        wb = openpyxl.load_workbook(file_path, data_only=True)
        ws = wb.worksheets[0]
        for field_name, label in fields_xlsx.items():
            value = _find_cell_value(ws, label)
            results[field_name] = value
            status_icon = "OK" if value else "--"
            required_marker = " *" if field_name in required_fields else ""
            display_value = (value[:50] if value else "(no match)")
            print(f"  {field_name + required_marker:<26} {status_icon:<6} {display_value}")

    for field_name, patterns in fields.items():
        if field_name in results and results[field_name]:
            continue  # already found via fields_xlsx
        patterns_list = patterns if isinstance(patterns, list) else [patterns]
        value = _extract_field(text, patterns_list)
        results[field_name] = value
        status_icon = "OK" if value else "--"
        required_marker = " *" if field_name in required_fields else ""
        display_value = value[:50] if value else "(no match)"
        print(f"  {field_name + required_marker:<26} {status_icon:<6} {display_value}")

    if is_excel:
        xlsx_li_config = tmpl.get("line_items_xlsx")
        if xlsx_li_config:
            wb = openpyxl.load_workbook(file_path, data_only=True)
            line_items = _extract_xlsx_line_items(wb, xlsx_li_config)
        else:
            line_items = []
    else:
        line_patterns = tmpl.get("line_items_patterns") or tmpl.get("line_items_pattern", "")
        line_items = _extract_line_items(text, line_patterns) if line_patterns else []
    print(f"\n  {'line_items':<26} {'OK' if line_items else '--':<6} {len(line_items)} item(s) found")

    extracted_required = sum(1 for f in required_fields if results.get(f))
    confidence = extracted_required / len(required_fields) if required_fields else 0.0

    print()
    print("─" * 70)
    print(f"Required fields matched: {extracted_required}/{len(required_fields)}")
    print(f"Parse confidence:        {confidence:.0%}")

    if confidence < LOW_CONFIDENCE_THRESHOLD:
        print(f"Status: LOW CONFIDENCE — would be flagged for review (threshold: {LOW_CONFIDENCE_THRESHOLD:.0%})")
    else:
        print(f"Status: OK")

    print(f"\n* = required field")
    return 0


def cmd_list_senders(args) -> int:
    days = args.days

    try:
        from auth.graph_client import GraphClient
        from pipeline.email_reader import fetch_unread_with_attachments
        from config.settings import ADDRESS_BOOK_PATH, PERSONAL_EMAIL_DOMAINS
    except Exception as e:
        print(f"Error loading pipeline modules: {e}")
        return 1

    known_domains: set[str] = set()
    known_emails: set[str] = set()
    if ADDRESS_BOOK_PATH.exists():
        try:
            data = json.loads(ADDRESS_BOOK_PATH.read_text(encoding="utf-8"))
            for c in data.get("contacts", []):
                for d in c.get("domains", []):
                    known_domains.add(d.lower())
                for e in c.get("emails", []):
                    known_emails.add(e.lower())
        except Exception:
            pass

    print(f"\nConnecting to Outlook... (scanning last {days} day(s))")
    try:
        client = GraphClient()
    except Exception as e:
        print(f"Error connecting to Outlook: {e}")
        return 1

    senders: dict[str, str] = {}
    email_count = 0
    for message in fetch_unread_with_attachments(client, days=days):
        addr = message.get("from", {}).get("emailAddress", {})
        email = addr.get("address", "").lower().strip()
        name = addr.get("name", "").strip()
        if email:
            senders[email] = name
        email_count += 1

    if not senders:
        print(f"No emails with attachments found in the last {days} day(s).")
        return 0

    print(f"Scanned {email_count} email(s) — found {len(senders)} unique sender(s).\n")

    new_senders: dict[str, str] = {}
    known_senders: dict[str, str] = {}
    for email, name in sorted(senders.items()):
        domain = email.split("@")[-1]
        if email in known_emails or domain in known_domains:
            known_senders[email] = name
        else:
            new_senders[email] = name

    if known_senders:
        print(f"Already in address_book.json ({len(known_senders)}):")
        for email, name in sorted(known_senders.items()):
            label = f"{name} <{email}>" if name else email
            print(f"  [OK]  {label}")
        print()

    if not new_senders:
        print("All senders are already in your address book.")
        return 0

    print(f"New senders not yet in address_book.json ({len(new_senders)}):")
    for email, name in sorted(new_senders.items()):
        label = f"{name} <{email}>" if name else email
        print(f"  [+]  {label}")

    print()
    print("─" * 60)
    print('Add these to config/address_book.json under "contacts":')
    print('Or run: python manage.py add-sender <email> [--name NAME]')
    print("─" * 60)

    for email, name in sorted(new_senders.items()):
        domain = email.split("@")[-1]
        customer_name = name if name else email.split("@")[0]
        is_personal = domain in PERSONAL_EMAIL_DOMAINS
        entry: dict = {"name": customer_name}
        if is_personal:
            entry["emails"] = [email]
        else:
            entry["domains"] = [domain]
        entry["template"] = ""
        print(json.dumps(entry, indent=2) + ",")

    print()
    print('Tip: set "template" to the stem of a .yaml file in config/templates/')
    print("     or leave blank to extract text only until you build the template.")
    return 0


# ---------------------------------------------------------------------------
# New commands
# ---------------------------------------------------------------------------

def cmd_add_sender(args) -> int:
    """
    Atomically add a new sender to config/address_book.json.
    Determines personal vs business automatically from the email domain.
    """
    try:
        from config.settings import ADDRESS_BOOK_PATH, PERSONAL_EMAIL_DOMAINS
    except Exception as e:
        print(f"Error loading settings: {e}")
        return 1

    email = args.email.strip().lower()
    domain = email.split("@")[-1] if "@" in email else ""
    is_personal = domain in PERSONAL_EMAIL_DOMAINS
    customer_name = args.name or (email.split("@")[0].replace(".", " ").title())

    entry: dict = {"name": customer_name}
    if is_personal:
        entry["emails"] = [email]
    else:
        entry["domains"] = [domain]
    if args.template:
        entry["template"] = args.template

    if not ADDRESS_BOOK_PATH.exists():
        print(f"Error: {ADDRESS_BOOK_PATH} not found. Create it first.")
        return 1

    try:
        book = json.loads(ADDRESS_BOOK_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Error reading address_book.json: {e}")
        return 1

    contacts = book.setdefault("contacts", [])
    # Duplicate check
    for c in contacts:
        if email in [e.lower() for e in c.get("emails", [])]:
            print(f"Sender {email} is already in address_book.json (matched by email).")
            return 0
        if domain and domain in [d.lower() for d in c.get("domains", [])]:
            print(f"Domain {domain} is already in address_book.json (contact: {c.get('name')}).")
            return 0

    contacts.append(entry)
    ADDRESS_BOOK_PATH.write_text(
        json.dumps(book, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Added {customer_name} ({email}) to address_book.json.")
    if args.template:
        print(f"  Template: {args.template}.yaml (must exist in config/templates/)")
    return 0


def cmd_list_suggestions(args) -> int:
    """List all pending suggested templates with a preview of sniffed fields."""
    try:
        from config.settings import SUGGESTED_TEMPLATES_DIR
        import yaml
    except Exception as e:
        print(f"Error: {e}")
        return 1

    if not SUGGESTED_TEMPLATES_DIR.exists():
        print("No suggested_templates directory found — no suggestions yet.")
        return 0

    files = sorted(SUGGESTED_TEMPLATES_DIR.glob("*.yaml"))
    if not files:
        print("No suggested templates found.")
        return 0

    print(f"\nSuggested templates ({len(files)}):\n")
    print(f"  {'File':<40} {'Customer':<25} {'ABN':<14} {'Examples'}")
    print("─" * 100)

    for f in files:
        try:
            tmpl = yaml.safe_load(f.read_text(encoding="utf-8"))
            name = tmpl.get("customer_name", "?")
            entry = tmpl.get("_address_book_entry", {})
            abn = entry.get("abns", [""])[0] if entry.get("abns") else ""
            examples = tmpl.get("_field_examples_found_in_document", {})
            ex_str = ", ".join(f"{k}={v}" for k, v in list(examples.items())[:2]) if examples else "none"
            print(f"  {f.name:<40} {name:<25} {abn:<14} {ex_str}")
        except Exception:
            print(f"  {f.name:<40} (could not parse)")

    print(f"\nTo approve: python manage.py approve-suggestion <sender_email>")
    return 0


def cmd_approve_suggestion(args) -> int:
    """
    Approve a suggested template: copy it to config/templates/ and add the
    _address_book_entry to config/address_book.json.
    """
    try:
        from config.settings import SUGGESTED_TEMPLATES_DIR, TEMPLATES_DIR, ADDRESS_BOOK_PATH
        import yaml, re
    except Exception as e:
        print(f"Error: {e}")
        return 1

    sender_email = args.email.strip().lower()
    safe = re.sub(r"[^\w@.\-]", "_", sender_email).replace("@", "_at_")
    src = SUGGESTED_TEMPLATES_DIR / f"{safe}.yaml"

    if not src.exists():
        print(f"Error: no suggestion found for {sender_email}")
        print(f"  Expected: {src}")
        available = list(SUGGESTED_TEMPLATES_DIR.glob("*.yaml")) if SUGGESTED_TEMPLATES_DIR.exists() else []
        if available:
            print(f"  Available: {', '.join(f.name for f in available)}")
        return 1

    try:
        tmpl = yaml.safe_load(src.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Error loading suggestion: {e}")
        return 1

    # Copy to active templates
    dest = TEMPLATES_DIR / src.name
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        yaml.dump(tmpl, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
    print(f"Template copied to: {dest}")

    # Add address_book_entry
    entry = tmpl.get("_address_book_entry", {})
    if not entry:
        print("Warning: no _address_book_entry found in suggestion — skipping address book update.")
        return 0

    if not ADDRESS_BOOK_PATH.exists():
        print(f"Error: {ADDRESS_BOOK_PATH} not found.")
        return 1

    try:
        book = json.loads(ADDRESS_BOOK_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Error reading address_book.json: {e}")
        return 1

    contacts = book.setdefault("contacts", [])
    existing_names = {c.get("name") for c in contacts}
    if entry.get("name") in existing_names:
        print(f"Contact {entry.get('name')!r} already in address_book.json — skipping.")
    else:
        contacts.append(entry)
        ADDRESS_BOOK_PATH.write_text(
            json.dumps(book, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"Added {entry.get('name')!r} to address_book.json.")

    print(f"\nDone. {entry.get('name', sender_email)} is now active.")
    print(f"Next pipeline run will download, extract, and parse their attachments.")
    return 0


def cmd_review_queue(args) -> int:
    """Show documents flagged for manual review."""
    try:
        from database import db
    except Exception as e:
        print(f"Error loading database: {e}")
        return 1

    status = "all" if args.all else "pending"
    rows = db.get_review_queue(status="pending") if status == "pending" else (
        db.get_review_queue("pending") + db.get_review_queue("resolved") + db.get_review_queue("dismissed")
    )

    if not rows:
        print("No items in review queue" + (" (pending)" if not args.all else "") + ".")
        return 0

    print(f"\nReview queue ({len(rows)} item(s)):\n")
    print(f"  {'ID':<5} {'Status':<12} {'Customer':<20} {'Conf':<6} {'File':<35} {'Reason'}")
    print("─" * 100)
    for r in rows:
        conf = f"{r['confidence']:.0%}" if r['confidence'] is not None else "n/a"
        fname = Path(r['attachment_filename']).name if r['attachment_filename'] else "?"
        print(
            f"  {r['queue_id']:<5} {r['status']:<12} {(r['customer_name'] or '?'):<20} "
            f"{conf:<6} {fname:<35} {r['reason'] or ''}"
        )

    print(f"\nTo resolve: python manage.py resolve-review <ID>")
    print(f"To dismiss: python manage.py resolve-review <ID> --dismiss")
    return 0


def cmd_resolve_review(args) -> int:
    """Mark a review queue item as resolved or dismissed."""
    try:
        from database import db
    except Exception as e:
        print(f"Error loading database: {e}")
        return 1

    status = "dismissed" if args.dismiss else "resolved"
    ok = db.resolve_review(args.id, resolved_by=args.by or "", status=status)
    if ok:
        print(f"Queue item {args.id} marked as {status}.")
        return 0
    else:
        print(f"Queue item {args.id} not found.")
        return 1


def cmd_analyze_template(args) -> int:
    """Show confidence trend for a template over recent runs."""
    try:
        from database import db
    except Exception as e:
        print(f"Error loading database: {e}")
        return 1

    rows = db.get_template_stats(args.template_name, last_n=args.last)

    if not rows:
        print(f"No stats found for template: {args.template_name!r}")
        print("Stats are recorded after each pipeline run that uses this template.")
        return 0

    confidences = [r["confidence"] for r in rows]
    avg = sum(confidences) / len(confidences)
    mn = min(confidences)
    mx = max(confidences)

    print(f"\nTemplate: {args.template_name}  |  Last {len(rows)} run(s)\n")
    print(f"  Average confidence: {avg:.0%}")
    print(f"  Min: {mn:.0%}   Max: {mx:.0%}")
    print()
    print(f"  {'Run at (UTC)':<28} {'Confidence':<12} {'Fields matched'}")
    print("─" * 65)
    for r in rows:
        matched = f"{r['required_fields_matched']}/{r['required_fields_total']}"
        bar_len = int(r["confidence"] * 20)
        bar = "#" * bar_len + "-" * (20 - bar_len)
        print(f"  {r['run_at'][:19]:<28} {r['confidence']:.0%}  [{bar}]  {matched}")

    # Drift alert
    if len(rows) >= 5:
        recent_avg = sum(confidences[:5]) / 5
        baseline_avg = sum(confidences) / len(confidences)
        drift = baseline_avg - recent_avg
        if drift > 0.15:
            print(
                f"\n  WARNING: Recent average ({recent_avg:.0%}) is {drift:.0%} below "
                f"overall baseline ({baseline_avg:.0%}). "
                "The vendor may have changed their document format."
            )
    return 0


def cmd_parse_pdf(args) -> int:
    """
    Extract text from a PDF or image file and print it.
    Optionally save to raw_text/ (same as the pipeline would).

    Useful for:
      - Seeing exactly what text the pipeline extracts before writing regex patterns
      - Diagnosing OCR quality on scanned documents
      - Producing the .txt file that cmd_parse_text then reads
    """
    file_path = Path(args.file)
    if not file_path.exists():
        print(f"Error: file not found: {file_path}")
        return 1

    try:
        from pipeline.text_extractor import extract_text
    except Exception as e:
        print(f"Error loading pipeline modules: {e}")
        return 1

    print(f"Extracting text from: {file_path.name} ...")
    try:
        text, is_native = extract_text(file_path) if args.save else _extract_no_save(file_path)
    except Exception as e:
        print(f"Error: {e}")
        return 1

    method = "native PDF" if is_native else "OCR"
    print(f"Method: {method} | {len(text)} chars")

    if args.save:
        from config.settings import RAW_TEXT_DIR
        out_dir = RAW_TEXT_DIR / file_path.parent.name
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / (file_path.stem + ".txt")
        out_path.write_text(text, encoding="utf-8")
        print(f"Saved: {out_path}")

    if args.show_text or not args.save:
        limit = args.chars if hasattr(args, "chars") else 3000
        print("\n" + "─" * 60)
        print(text[:limit] + ("..." if len(text) > limit else ""))
        print("─" * 60)

    return 0


def _extract_no_save(path):
    """Extract text without writing to raw_text/ — for parse-pdf without --save."""
    import pdfplumber, fitz, pytesseract
    from PIL import Image
    from config.settings import TESSERACT_CMD
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

    ext = path.suffix.lower()
    IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tiff", ".bmp"}
    MIN_CHARS = 50

    def ocr(img):
        return pytesseract.image_to_string(img.convert("RGB"), lang="eng")

    if ext == ".pdf":
        pages = []
        used_ocr = False
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                t = page.extract_text() or ""
                if len(t.strip()) >= MIN_CHARS:
                    pages.append(t)
                else:
                    used_ocr = True
                    doc = fitz.open(str(path))
                    pix = doc[page.page_number - 1].get_pixmap(matrix=fitz.Matrix(2, 2))
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    pages.append(ocr(img))
        return "\n\n".join(pages), not used_ocr
    elif ext in IMAGE_EXTS:
        return ocr(Image.open(path)), False
    else:
        raise ValueError(f"Unsupported file type: {ext}")


def cmd_parse_text(args) -> int:
    """
    Run a YAML template's regex patterns against an already-extracted text file.
    Skips PDF/OCR entirely — reads the .txt directly.

    The text file is typically in raw_text/<message_id>/<filename>.txt
    but can be any plain-text file.
    """
    txt_path = Path(args.txt_file)
    if not txt_path.exists():
        print(f"Error: file not found: {txt_path}")
        return 1

    try:
        from pipeline.template_parser import _load_template, _extract_field, _extract_line_items
        from config.settings import TEMPLATES_DIR, LOW_CONFIDENCE_THRESHOLD
    except Exception as e:
        print(f"Error loading pipeline modules: {e}")
        return 1

    template_path = TEMPLATES_DIR / f"{args.template_name}.yaml"
    if not template_path.exists():
        print(f"Error: template not found: {template_path}")
        available = [p.stem for p in TEMPLATES_DIR.glob("*.yaml")]
        if available:
            print(f"  Available: {', '.join(sorted(available))}")
        return 1

    tmpl = _load_template(args.template_name)
    if not tmpl:
        print(f"Error: failed to load template: {args.template_name}")
        return 1

    text = txt_path.read_text(encoding="utf-8", errors="replace")
    print(f"Text file: {txt_path}  ({len(text)} chars)")

    if args.show_text:
        print("\n" + "─" * 60)
        print("TEXT:")
        print("─" * 60)
        limit = 3000
        print(text[:limit] + ("..." if len(text) > limit else ""))
        print("─" * 60)

    fields = tmpl.get("fields", {})
    required_fields = tmpl.get("required_fields", list(fields.keys()))

    print(f"\nTemplate: {args.template_name}  |  Customer: {tmpl.get('customer_name', '?')}")
    print(f"Required fields: {', '.join(required_fields) or '(none)'}")
    print()
    print(f"  {'Field':<28} {'Status':<6} Value")
    print("─" * 72)

    results = {}
    for field_name, patterns in fields.items():
        patterns_list = patterns if isinstance(patterns, list) else [patterns]
        value = _extract_field(text, patterns_list)
        results[field_name] = value
        marker = " *" if field_name in required_fields else ""
        status = "OK" if value else "--"
        display = (value[:52] + "…") if value and len(value) > 52 else (value or "(no match)")
        print(f"  {field_name + marker:<28} {status:<6} {display}")

    line_items_pattern = tmpl.get("line_items_pattern", "")
    line_items = _extract_line_items(text, line_items_pattern) if line_items_pattern else []
    print(f"  {'line_items':<28} {'OK' if line_items else '--':<6} {len(line_items)} item(s)")

    extracted = sum(1 for f in required_fields if results.get(f))
    confidence = extracted / len(required_fields) if required_fields else 0.0

    print()
    print("─" * 72)
    print(f"Required fields matched:  {extracted}/{len(required_fields)}")
    print(f"Parse confidence:         {confidence:.0%}")

    if confidence < LOW_CONFIDENCE_THRESHOLD:
        print(f"Status: LOW CONFIDENCE — would be flagged for review (threshold {LOW_CONFIDENCE_THRESHOLD:.0%})")
    else:
        print("Status: OK")

    if line_items:
        print(f"\nLine items ({len(line_items)}):")
        for i, item in enumerate(line_items[:5], 1):
            parts = "  ".join(f"{k}={v}" for k, v in item.items() if v)
            print(f"  {i}. {parts}")
        if len(line_items) > 5:
            print(f"  ... and {len(line_items) - 5} more")

    print("\n* = required field")
    return 0


def cmd_health(args) -> int:
    """Check overall pipeline health: token, DB, disk, review queue."""
    from pathlib import Path

    issues = []

    # Token health
    try:
        from auth.graph_client import check_token_health
        h = check_token_health()
        if h["valid"]:
            print(f"  [OK]  Auth token: account={h['account']}")
        else:
            print(f"  [!!]  Auth token: not cached — run interactively to authenticate")
            issues.append("no cached token")
    except Exception as e:
        print(f"  [!!]  Auth check failed: {e}")
        issues.append(str(e))

    # Database health
    try:
        from database import db
        stats = db.get_run_stats()
        print(
            f"  [OK]  Database: {stats['total_processed']} total processed, "
            f"{stats['total_errors']} errors, "
            f"{stats['pending_review']} pending review"
        )
        if stats["pending_review"] > 0:
            issues.append(f"{stats['pending_review']} documents pending review")
    except Exception as e:
        print(f"  [!!]  Database: {e}")
        issues.append(f"db error: {e}")

    # Address book
    try:
        from config.settings import ADDRESS_BOOK_PATH
        if ADDRESS_BOOK_PATH.exists():
            book = json.loads(ADDRESS_BOOK_PATH.read_text(encoding="utf-8"))
            count = len(book.get("contacts", []))
            print(f"  [OK]  Address book: {count} contact(s)")
        else:
            print(f"  [!!]  Address book: not found at {ADDRESS_BOOK_PATH}")
            issues.append("address_book.json missing")
    except Exception as e:
        print(f"  [!!]  Address book: {e}")
        issues.append(str(e))

    # Last metrics
    try:
        from pipeline.metrics import read_last_metrics
        m = read_last_metrics()
        if m:
            print(
                f"  [OK]  Last run: {m.get('run_finished_at', '?')[:19]} UTC — "
                f"{m.get('total_attachments', 0)} attachment(s), "
                f"avg confidence={m.get('avg_confidence_this_run', 'n/a')}"
            )
        else:
            print("  [--]  No previous run metrics found (pipeline has not run yet)")
    except Exception as e:
        print(f"  [!!]  Metrics: {e}")

    # Pending suggestions
    try:
        from config.settings import SUGGESTED_TEMPLATES_DIR
        if SUGGESTED_TEMPLATES_DIR.exists():
            pending = list(SUGGESTED_TEMPLATES_DIR.glob("*.yaml"))
            if pending:
                print(f"  [--]  Pending suggestions: {len(pending)} (run list-suggestions)")
                issues.append(f"{len(pending)} suggested templates need review")
            else:
                print(f"  [OK]  Suggested templates: none pending")
    except Exception:
        pass

    print()
    if issues:
        print(f"Issues ({len(issues)}):")
        for i in issues:
            print(f"  - {i}")
        return 1
    else:
        print("All checks passed.")
        return 0


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="ms_outlook pipeline management utilities",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", metavar="command")

    # test-template
    p = sub.add_parser("test-template", help="Test a YAML template against a PDF or image file")
    p.add_argument("template_name", help="Template stem e.g. acme")
    p.add_argument("file", help="Path to PDF or image file")
    p.add_argument("--show-text", action="store_true", help="Print first 3000 chars of extracted text")

    # list-senders
    p = sub.add_parser("list-senders", help="List unique senders with attachments in your mailbox")
    p.add_argument("--days", type=int, default=30, help="Days back to scan (default: 30)")

    # add-sender
    p = sub.add_parser("add-sender", help="Add a sender to address_book.json directly")
    p.add_argument("email", help="Sender email address")
    p.add_argument("--name", help="Display name (inferred from email if omitted)")
    p.add_argument("--template", help="Template stem to link (e.g. acme — must exist in config/templates/)")

    # list-suggestions
    sub.add_parser("list-suggestions", help="List pending auto-generated template suggestions")

    # approve-suggestion
    p = sub.add_parser("approve-suggestion", help="Promote a suggested template to active status")
    p.add_argument("email", help="Sender email the suggestion was generated for")

    # review-queue
    p = sub.add_parser("review-queue", help="Show documents flagged for manual review")
    p.add_argument("--all", action="store_true", help="Include resolved and dismissed items")

    # resolve-review
    p = sub.add_parser("resolve-review", help="Mark a review queue item as resolved")
    p.add_argument("id", type=int, help="Queue item ID (from review-queue)")
    p.add_argument("--dismiss", action="store_true", help="Mark as dismissed instead of resolved")
    p.add_argument("--by", help="Name or identifier of the person resolving the item")

    # analyze-template
    p = sub.add_parser("analyze-template", help="Show confidence trend for a template")
    p.add_argument("template_name", help="Template stem e.g. acme")
    p.add_argument("--last", type=int, default=20, help="Number of recent runs to show (default: 20)")

    # health
    sub.add_parser("health", help="Check token, database, address book, and last run status")

    # parse-pdf
    p = sub.add_parser("parse-pdf", help="Extract text from a PDF or image and print it")
    p.add_argument("file", help="Path to PDF or image file")
    p.add_argument("--save", action="store_true", help="Save extracted text to raw_text/")
    p.add_argument("--show-text", action="store_true", help="Print extracted text (default when --save not set)")
    p.add_argument("--chars", type=int, default=3000, help="Max chars to print (default: 3000)")

    # parse-text
    p = sub.add_parser("parse-text", help="Run a template against an already-extracted text file")
    p.add_argument("template_name", help="Template stem e.g. acme")
    p.add_argument("txt_file", help="Path to .txt file (e.g. raw_text/abc123/invoice.txt)")
    p.add_argument("--show-text", action="store_true", help="Print the text before parsing")

    args = parser.parse_args()

    dispatch = {
        "test-template":    cmd_test_template,
        "list-senders":     cmd_list_senders,
        "add-sender":       cmd_add_sender,
        "list-suggestions": cmd_list_suggestions,
        "approve-suggestion": cmd_approve_suggestion,
        "review-queue":     cmd_review_queue,
        "resolve-review":   cmd_resolve_review,
        "analyze-template": cmd_analyze_template,
        "health":           cmd_health,
        "parse-pdf":        cmd_parse_pdf,
        "parse-text":       cmd_parse_text,
    }

    fn = dispatch.get(args.command)
    if fn:
        sys.exit(fn(args))
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
