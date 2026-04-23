"""
Management utility for the ms_outlook pipeline.

Commands:
  python manage.py test-template <template_name> <pdf_or_image_path> [--show-text]
  python manage.py list-senders [--days 30]

Examples:
  python manage.py test-template acme invoice.pdf
  python manage.py test-template acme scan.png --show-text
  python manage.py list-senders
  python manage.py list-senders --days 90
"""
import argparse
import sys
from pathlib import Path

_PERSONAL_DOMAINS = {
    "gmail.com", "hotmail.com", "outlook.com", "yahoo.com",
    "live.com", "icloud.com", "bigpond.com", "optusnet.com.au",
}


def cmd_test_template(args) -> int:
    template_name: str = args.template_name
    file_path = Path(args.file)

    if not file_path.exists():
        print(f"Error: file not found: {file_path}")
        return 1

    try:
        from pipeline.text_extractor import extract_text
        from pipeline.template_parser import _load_template, _extract_field, _extract_line_items
        from config.settings import TEMPLATES_DIR, LOW_CONFIDENCE_THRESHOLD
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

    print(f"\nExtracting text from: {file_path.name}")
    try:
        text, is_native = extract_text(file_path)
    except Exception as e:
        print(f"Error extracting text: {e}")
        return 1

    extraction_method = "native PDF" if is_native else "OCR"
    print(f"Extraction method: {extraction_method} | {len(text)} chars extracted")

    if args.show_text:
        print("\n" + "─" * 60)
        print("EXTRACTED TEXT:")
        print("─" * 60)
        print(text[:3000] + ("..." if len(text) > 3000 else ""))
        print("─" * 60)

    fields = tmpl.get("fields", {})
    required_fields = tmpl.get("required_fields", list(fields.keys()))

    print(f"\nTemplate: {template_name}  |  Customer: {tmpl.get('customer_name', '?')}")
    print(f"Required fields: {', '.join(required_fields)}")
    print()
    print(f"{'Field':<28} {'Status':<6} {'Value'}")
    print("─" * 70)

    results = {}
    for field_name, patterns in fields.items():
        patterns_list = patterns if isinstance(patterns, list) else [patterns]
        value = _extract_field(text, patterns_list)
        results[field_name] = value
        status_icon = "✓" if value else "✗"
        required_marker = " *" if field_name in required_fields else ""
        display_value = value[:50] if value else "(no match)"
        print(f"  {field_name + required_marker:<26} {status_icon:<6} {display_value}")

    line_items_pattern = tmpl.get("line_items_pattern", "")
    line_items = _extract_line_items(text, line_items_pattern) if line_items_pattern else []
    print(f"\n  {'line_items':<26} {'✓' if line_items else '✗':<6} {len(line_items)} item(s) found")

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
        import json
        from auth.graph_client import GraphClient
        from pipeline.email_reader import fetch_unread_with_attachments
        from config.settings import ADDRESS_BOOK_PATH
    except Exception as e:
        print(f"Error loading pipeline modules: {e}")
        return 1

    # Load existing contacts so we can mark already-known senders
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

    # Collect unique senders from emails that have attachments
    senders: dict[str, str] = {}  # email -> display name
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
            print(f"  ✓  {label}")
        print()

    if not new_senders:
        print("All senders are already in your address book.")
        return 0

    print(f"New senders not yet in address_book.json ({len(new_senders)}):")
    for email, name in sorted(new_senders.items()):
        label = f"{name} <{email}>" if name else email
        print(f"  +  {label}")

    print()
    print("─" * 60)
    print('Add these to config/address_book.json under "contacts":')
    print("─" * 60)

    for email, name in sorted(new_senders.items()):
        domain = email.split("@")[-1]
        customer_name = name if name else email.split("@")[0]
        is_personal = domain in _PERSONAL_DOMAINS
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


def main():
    parser = argparse.ArgumentParser(
        description="ms_outlook pipeline management utilities",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    subparsers = parser.add_subparsers(dest="command", metavar="command")

    test_tmpl = subparsers.add_parser(
        "test-template",
        help="Test a YAML template against a PDF or image file",
    )
    test_tmpl.add_argument("template_name", help="Template name without .yaml (e.g. acme)")
    test_tmpl.add_argument("file", help="Path to a PDF or image file")
    test_tmpl.add_argument(
        "--show-text", action="store_true",
        help="Print the first 3000 chars of extracted text (useful for writing patterns)"
    )

    list_snd = subparsers.add_parser(
        "list-senders",
        help="List unique senders with attachments and generate address_book.json entries",
    )
    list_snd.add_argument(
        "--days", type=int, default=30,
        help="How many days back to scan (default: 30)",
    )

    args = parser.parse_args()

    if args.command == "test-template":
        sys.exit(cmd_test_template(args))
    elif args.command == "list-senders":
        sys.exit(cmd_list_senders(args))
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
