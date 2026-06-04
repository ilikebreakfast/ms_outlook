"""
Interactive attachment selector for the invoice pipeline.

  N — new attachments (v3/data/attachments/)   → moved to processed/ after success
  U — unprocessed only (new attachments not yet staged for the chosen environment)
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from core.config import V3_ROOT, DATA_DIR, data_dir_for
from core.invoices.extractor import SUPPORTED_EXTENSIONS

_V3_ROOT       = V3_ROOT
_DATA_DIR      = DATA_DIR
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


# Accepted attachment types come straight from the rasteriser so the two
# never drift; rendered as ".pdf / .txt / .csv" for user-facing messages.
_SUPPORTED_EXTS = set(SUPPORTED_EXTENSIONS)
_SUPPORTED_EXTS_LABEL = " / ".join(SUPPORTED_EXTENSIONS)


def _list_attachments(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    files = [
        f for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() in _SUPPORTED_EXTS
    ]
    return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)


def _print_attachment_table(files: list[Path], senders: dict, label: str) -> None:
    print(f"\n{'='*W}")
    print(f"  {label} ATTACHMENTS  ({len(files)} files)".ljust(W))
    print(f"{'='*W}")
    if not files:
        print(f"  No supported files found ({_SUPPORTED_EXTS_LABEL}).")
        return
    print(f"  {'#':<4} {'File':<32} {'Type':<6} {'Domain':<20} {'Modified'}")
    print(f"  {'-'*4} {'-'*32} {'-'*6} {'-'*20} {'-'*10}")
    for i, f in enumerate(files, 1):
        mtime  = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d")
        domain = _domain(f.name, senders)[:20]
        name   = f.name[:32]
        ftype  = f.suffix.lstrip(".").upper()
        print(f"  {i:<4} {name:<32} {ftype:<6} {domain:<20} {mtime}")


def _pick_files(files: list[Path]) -> list[Path]:
    print(f"\n  [A] All")
    print(f"  [Q] Back")
    print(f"{'-'*W}")
    raw = input("  Select numbers (comma-separated), A for all, or Q: ").strip().upper()
    if raw == "Q":
        return []
    if raw == "A":
        return files

    selected = []
    for part in raw.split(","):
        part = part.strip()
        if not part: continue
        try:
            idx = int(part) - 1
            if 0 <= idx < len(files):
                if files[idx] not in selected:
                    selected.append(files[idx])
            else:
                print(f"  Warning: {part} is out of range.")
        except ValueError:
            print(f"  Warning: '{part}' is not a valid number.")
    return selected


def _get_target_environment() -> str | None:
    print(f"\n  Target Environment:")
    print(f"    [T] Test sandbox (don't move attachment)")
    print(f"    [L] Live production (moves attachment to processed)")
    print(f"    [Q] Cancel / Back")
    print(f"{'-'*W}")
    while True:
        choice = input("  Select environment [T]: ").strip().upper()
        if not choice or choice == "T":
            return "test"
        if choice == "L":
            return "live"
        if choice == "Q":
            return None
        print("  Invalid choice.")


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


def _run_parser(attachment: Path, mode: str, env_mode: str) -> int:
    print(f"\n{'='*W}")
    print(f"  Running: {attachment.name}  [Ext: {mode.upper()} | Env: {env_mode.upper()}]".ljust(W))
    print(f"{'='*W}\n")

    env = os.environ.copy()
    if env_mode == "test":
        env["INVOICE_TEST"] = "1"
    else:
        env.pop("INVOICE_TEST", None)

    cmd = [str(_PYTHON), str(_PARSER), str(attachment)]
    if mode == "text":
        cmd.append("--force-text")
    elif mode == "vision":
        cmd.append("--force-vision")
        
    result = subprocess.run(cmd, env=env)
    return result.returncode


def _get_staged_names(staging_root: Path) -> set[str]:
    """Return the set of source filenames already staged.

    Staging JSON files are named '<source-filename>.json' (e.g.
    'invoice.pdf.json'), so f.stem recovers the original attachment name
    including its extension. Matching on the full name keeps attachments that
    share a stem but differ in extension (invoice.pdf vs invoice.csv) distinct.
    """
    names: set[str] = set()
    for subdir in ("pending", "approved", "rejected"):
        d = staging_root / subdir
        if d.exists():
            for f in d.glob("*.json"):
                names.add(f.stem)
    return names


def _move_to_processed(attachment: Path) -> None:
    dest_dir = _V3_ROOT / "data" / "processed"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / attachment.name
    # Avoid silent overwrite if same filename already exists in processed/
    if dest.exists():
        stem = attachment.stem
        suffix = attachment.suffix
        counter = 1
        while dest.exists():
            dest = dest_dir / f"{stem}_{counter}{suffix}"
            counter += 1
    attachment.rename(dest)
    print(f"\n  Moved to processed: {dest.name}")


def main() -> None:
    senders = _load_senders()

    while True:
        print(f"\n{'='*W}")
        print("  INVOICE ATTACHMENT SELECTOR".center(W))
        print(f"{'='*W}")
        print("  [N]  New attachments        (data/attachments/)")
        print("  [U]  Unprocessed only        (not yet staged for the chosen env)")
        print("  [P]  Processed attachments  (data/processed/)")
        print("  [R]  Review all pending staged invoices (Test sandbox)")
        print("  [Q]  Quit")
        print(f"{'-'*W}")
        choice = input("  Select: ").strip().upper()

        if choice == "Q":
            break

        if choice == "R":
            # Injects INVOICE_TEST=1 for sandboxed pending review
            env = os.environ.copy()
            env["INVOICE_TEST"] = "1"
            subprocess.run([str(_PYTHON), str(_PARSER), "--review-pending"], env=env)
            continue

        if choice == "N":
            folder = _V3_ROOT / "data" / "attachments"
            label = "NEW"
            can_move = True
        elif choice == "U":
            folder = _V3_ROOT / "data" / "attachments"
            label = "UNPROCESSED"
            can_move = True
        elif choice == "P":
            folder = _V3_ROOT / "data" / "processed"
            label = "PROCESSED"
            can_move = False
        else:
            print("  Unknown option — try N, U, P, R, or Q.")
            continue

        files = _list_attachments(folder)

        if choice == "U":
            env_choice_for_filter = _get_target_environment()
            if not env_choice_for_filter:
                continue
            # Resolve the staging dir the parser subprocess will actually write
            # to for the chosen environment, via the same mapping core.config
            # uses — independent of this selector's own INVOICE_TEST.
            staging_root = (
                data_dir_for(env_choice_for_filter == "test") / "invoice_staging"
            )
            staged_names = _get_staged_names(staging_root)
            files = [f for f in files if f.name not in staged_names]
            label = f"UNPROCESSED ({env_choice_for_filter.upper()})"

        _print_attachment_table(files, senders, label)

        if not files:
            continue

        selected_files = _pick_files(files)
        if not selected_files:
            continue

        if choice == "U":
            env_mode = env_choice_for_filter
        else:
            env_choice = _get_target_environment()
            if not env_choice:
                continue
            env_mode = env_choice

        mode = "both"
        mode_choice = _get_extraction_mode()
        if not mode_choice:
            continue
        mode = mode_choice

        for selected in selected_files:
            returncode = _run_parser(selected, mode, env_mode)

            if can_move and env_mode == "live" and returncode == 0:
                _move_to_processed(selected)
            elif can_move and returncode != 0:
                print(f"\n  Pipeline exited with error (code {returncode}) — {selected.name} left in attachments/.")

        input("\n  Press ENTER to return to menu...")


if __name__ == "__main__":
    main()
