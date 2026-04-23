"""
Management utility for the ms_outlook pipeline.

Commands:
  python manage.py test-template <template_name> <pdf_or_image_path> [--show-text]

Examples:
  python manage.py test-template evergy invoice.pdf
  python manage.py test-template bonitahua scan.png --show-text
"""
import argparse
import sys
from pathlib import Path


def cmd_test_template(args) -> int:
    template_name: str = args.template_name
    file_path = Path(args.file)

    if not file_path.exists():
        print(f"Error: file not found: {file_path}")
        return 1

    # Lazy imports so errors (missing deps, bad .env) are reported cleanly
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
    test_tmpl.add_argument("template_name", help="Template name without .yaml (e.g. evergy)")
    test_tmpl.add_argument("file", help="Path to a PDF or image file")
    test_tmpl.add_argument(
        "--show-text", action="store_true",
        help="Print the first 3000 chars of extracted text (useful for writing patterns)"
    )

    args = parser.parse_args()

    if args.command == "test-template":
        sys.exit(cmd_test_template(args))
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
