"""
Interactive attachment selector for the invoice pipeline.

  N — new attachments (v3/data/attachments/)   → moved to processed/ after success
  P — processed attachments (v3/tests/data/processed/)
  R — review all pending staged invoices
  Q — quit
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_V3_ROOT       = Path(__file__).resolve().parent.parent.parent.parent
_DATA_DIR      = _V3_ROOT / "tests" / "data" if os.environ.get("INVOICE_TEST") == "1" else _V3_ROOT
_ATTACH_DIR    = _V3_ROOT / "data" / "attachments"
_PROCESSED_DIR = _DATA_DIR / "processed"
_SENDERS_FILE  = _V3_ROOT / "data" / "mock_senders.json"
_PARSER        = _V3_ROOT / "invoice_parser.py"
_PYTHON        = Path(sys.executable)

W = 68  # display width


def _load_senders() -> dict:
    if _SENDERS_FILE.exists():
        try:
            return json.loads(_SENDERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _domain(filename: str, senders: dict) -> str:
    email = senders.get(filename, "")
    if "@" in email:
        return email.split("@")[1]
    return ""


def _list_pdfs(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(folder.glob("*.pdf"), key=lambda f: f.stat().st_mtime, reverse=True)


def _print_pdf_table(pdfs: list[Path], senders: dict, label: str) -> None:
    print(f"\n{'='*W}")
    print(f"  {label} ATTACHMENTS  ({len(pdfs)} files)".ljust(W))
    print(f"{'='*W}")
    if not pdfs:
        print(f"  No PDF files found.")
        return
    print(f"  {'#':<4} {'File':<32} {'Domain':<22} {'Modified'}")
    print(f"  {'-'*4} {'-'*32} {'-'*22} {'-'*10}")
    for i, pdf in enumerate(pdfs, 1):
        mtime  = datetime.fromtimestamp(pdf.stat().st_mtime).strftime("%Y-%m-%d")
        domain = _domain(pdf.name, senders)[:22]
        name   = pdf.name[:32]
        print(f"  {i:<4} {name:<32} {domain:<22} {mtime}")


def _pick_file(pdfs: list[Path]) -> Path | None:
    print(f"\n  [Q] Back")
    print(f"{'-'*W}")
    raw = input("  Select number or Q: ").strip().upper()
    if raw == "Q":
        return None
    try:
        idx = int(raw) - 1
        if 0 <= idx < len(pdfs):
            return pdfs[idx]
        print("  Out of range.")
    except ValueError:
        print("  Invalid input.")
    return None


def _get_extraction_mode() -> str | None:
    print(f"\n  Select extraction mode:")
    print(f"    [1] Both (Default: Text for text pages, Vision for scanned pages)")
    print(f"    [2] Language Model only (Force Text, uses OCR for image pages)")
    print(f"    [3] Vision Model only (Force Vision, renders all pages as images)")
    print(f"    [Q] Cancel / Back")
    print(f"{'-'*W}")
    while True:
        choice = input("  Select mode [1]: ").strip().upper()
        if not choice or choice == "1":
            return "both"
        if choice == "2":
            return "text"
        if choice == "3":
            return "vision"
        if choice == "Q":
            return None
        print("  Invalid choice.")


def _run_parser(pdf: Path, mode: str) -> int:
    print(f"\n{'='*W}")
    print(f"  Running: {pdf.name}  [Mode: {mode.upper()}]".ljust(W))
    print(f"{'='*W}\n")
    
    # Inject INVOICE_TEST=1 for sandboxed run
    env = dict(os.environ, INVOICE_TEST="1")
    cmd = [str(_PYTHON), str(_PARSER), str(pdf)]
    if mode == "text":
        cmd.append("--force-text")
    elif mode == "vision":
        cmd.append("--force-vision")
        
    result = subprocess.run(cmd, env=env)
    return result.returncode


def _move_to_processed(pdf: Path) -> None:
    _PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    dest = _PROCESSED_DIR / pdf.name
    # Avoid silent overwrite if same filename already exists in processed/
    if dest.exists():
        stem = pdf.stem
        suffix = pdf.suffix
        counter = 1
        while dest.exists():
            dest = _PROCESSED_DIR / f"{stem}_{counter}{suffix}"
            counter += 1
    pdf.rename(dest)
    print(f"\n  Moved to processed: {dest.name}")


def main() -> None:
    senders = _load_senders()

    while True:
        print(f"\n{'='*W}")
        print("  INVOICE ATTACHMENT SELECTOR".center(W))
        print(f"{'='*W}")
        print("  [N]  New attachments        (data/attachments/)")
        print("  [P]  Processed attachments  (tests/data/processed/)")
        print("  [R]  Review all pending staged invoices")
        print("  [Q]  Quit")
        print(f"{'-'*W}")
        choice = input("  Select: ").strip().upper()

        if choice == "Q":
            break

        if choice == "R":
            # Injects INVOICE_TEST=1 for sandboxed pending review
            subprocess.run([str(_PYTHON), str(_PARSER), "--review-pending"], env=dict(os.environ, INVOICE_TEST="1"))
            continue

        if choice == "N":
            folder = _ATTACH_DIR
            label = "NEW"
            move_after = True
        elif choice == "P":
            folder = _PROCESSED_DIR
            label = "PROCESSED"
            move_after = False
        else:
            print("  Unknown option — try N, P, R, or Q.")
            continue

        pdfs = _list_pdfs(folder)
        _print_pdf_table(pdfs, senders, label)

        if not pdfs:
            continue

        selected = _pick_file(pdfs)
        if selected is None:
            continue

        mode = "both"
        if move_after:  # only ask for extraction mode when executing on new files
            selected_mode = _get_extraction_mode()
            if selected_mode is None:
                continue
            mode = selected_mode

        returncode = _run_parser(selected, mode)

        if move_after and returncode == 0:
            _move_to_processed(selected)
        elif move_after and returncode != 0:
            print(f"\n  Pipeline exited with error (code {returncode}) — file left in attachments/.")

        input("\n  Press ENTER to return to menu...")


if __name__ == "__main__":
    main()
