"""
Security checks applied before any attachment is processed.

Checks (in order):
  1. Filename sanitisation        — strip path traversal
  2. File size limit              — reject oversized files
  3. Magic byte validation        — confirm actual file type matches extension
  4. PDF structure scan           — flag embedded JavaScript, auto-actions, URIs
  5. Sender allowlist             — skip emails from unknown domains
  6. ClamAV antivirus scan        — optional, skipped gracefully if not installed
  7. Prompt injection scrubbing   — sanitise extracted text before it enters any parser

None of these checks modify or execute the file. They only read bytes.
"""
import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

from config.settings import CLAMAV_ENABLED, CLAMAV_CMD

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

MAX_FILE_SIZE_MB = 20

# Magic bytes for accepted types
MAGIC_BYTES: dict[str, list[bytes]] = {
    ".pdf":  [b"%PDF"],
    ".png":  [b"\x89PNG"],
    ".jpg":  [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
    ".tiff": [b"II*\x00", b"MM\x00*"],
    ".bmp":  [b"BM"],
    ".xlsx": [b"PK\x03\x04"],  # OOXML = ZIP container
    ".xls":  [b"\xD0\xCF\x11\xE0"],  # OLE2 Compound Document (BIFF)

}

# PDF keywords that can execute code or exfiltrate data — always block.
PDF_FATAL_PATTERNS: list[tuple[bytes, str]] = [
    (b"/JavaScript", "embedded JavaScript"),
    (b"/JS",         "embedded JavaScript (short form)"),
    (b"/Launch",     "Launch action (executes external program)"),
    (b"/XFA",        "XFA form (XML Forms Architecture, commonly abused)"),
]

# PDF action triggers that are only dangerous when paired with an execution payload.
# Standalone /AA or /OpenAction in POS receipts, print dialogs, and page-navigation
# PDFs are legitimate and produce false positives. Block only when a JS/Launch
# payload is also present.
PDF_ACTION_TRIGGERS: list[bytes] = [b"/AA", b"/OpenAction"]
PDF_EXEC_PAYLOADS: list[bytes] = [b"/JavaScript", b"/JS", b"/Launch"]

# PDF keywords that are suspicious but appear in many legitimate documents.
# These are logged as warnings but do NOT block processing.
#   /EmbeddedFile — present in ZUGFeRD/Factur-X invoices, POS receipts,
#                   and PDFs with embedded fonts or ICC colour profiles.
#                   Blocking on this produces too many false positives.
PDF_WARN_PATTERNS: list[tuple[bytes, str]] = [
    (b"/EmbeddedFile", "embedded file attachment (informational — not blocked)"),
    (b"/AA",           "auto-action trigger (no executable payload detected — informational)"),
    (b"/OpenAction",   "OpenAction trigger (no executable payload detected — informational)"),
]

# Prompt injection phrases to scrub from extracted text before parsing
# These are instruction-like strings that could hijack an LLM if one is added later.
INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?",
    r"you\s+are\s+now\s+a",
    r"disregard\s+(all\s+)?previous",
    r"system\s*:\s*",
    r"<\s*/?system\s*>",
    r"<\s*/?instructions?\s*>",
    r"act\s+as\s+(a|an)\s+\w+",
]
_INJECTION_RE = re.compile("|".join(INJECTION_PATTERNS), re.IGNORECASE)


# ---------------------------------------------------------------------------
# 1. Filename sanitisation
# ---------------------------------------------------------------------------

def sanitise_filename(name: str) -> str:
    """Strip any directory components and dangerous characters from a filename."""
    safe = Path(name).name  # drop any leading path
    safe = re.sub(r"[^\w.\-() ]", "_", safe)
    return safe or "unnamed"


# ---------------------------------------------------------------------------
# 2. File size
# ---------------------------------------------------------------------------

def check_file_size(path: Path) -> Optional[str]:
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        return f"File too large: {size_mb:.1f} MB (limit {MAX_FILE_SIZE_MB} MB)"
    return None


# ---------------------------------------------------------------------------
# 3. Magic byte validation
# ---------------------------------------------------------------------------

def check_magic_bytes(path: Path) -> Optional[str]:
    ext = path.suffix.lower()
    expected = MAGIC_BYTES.get(ext)
    if not expected:
        return f"Unsupported extension: {ext}"

    header = path.read_bytes()[:8]
    if not any(header.startswith(magic) for magic in expected):
        return f"Magic bytes don't match extension {ext} — possible spoofed file type"
    return None


# ---------------------------------------------------------------------------
# 4. PDF structure scan
# ---------------------------------------------------------------------------

def scan_pdf_structure(path: Path) -> tuple[list[str], list[str]]:
    """
    Reads raw PDF bytes and checks for dangerous or suspicious structures.
    Does NOT parse or execute anything.

    Returns:
        (fatal_issues, warnings)
        fatal_issues — patterns that indicate code execution risk; caller should block.
        warnings     — informational patterns that appear in legitimate PDFs; log only.
    """
    if path.suffix.lower() != ".pdf":
        return [], []

    fatal_issues: list[str] = []
    warnings: list[str] = []
    try:
        data = path.read_bytes()
        for pattern, label in PDF_FATAL_PATTERNS:
            if pattern in data:
                fatal_issues.append(f"Blocked — dangerous PDF structure: {label}")
        # Action triggers (/AA, /OpenAction) are only fatal when an execution payload
        # (/JS, /JavaScript, /Launch) is also present. Standalone triggers appear in
        # legitimate POS receipts and print-dialog PDFs.
        has_exec_payload = any(p in data for p in PDF_EXEC_PAYLOADS)
        for trigger in PDF_ACTION_TRIGGERS:
            if trigger in data:
                if has_exec_payload:
                    fatal_issues.append(
                        f"Blocked — dangerous PDF structure: {trigger.decode()} with executable payload"
                    )
                # warn-only path: find the matching label from PDF_WARN_PATTERNS
        for pattern, label in PDF_WARN_PATTERNS:
            if pattern in data:
                # Skip action triggers that were already escalated to fatal above
                if pattern in PDF_ACTION_TRIGGERS and has_exec_payload:
                    continue
                warnings.append(f"PDF note: {label}")
    except Exception as exc:
        fatal_issues.append(f"Could not scan PDF structure: {exc}")
    return fatal_issues, warnings


# ---------------------------------------------------------------------------
# 5. ClamAV antivirus scan
# ---------------------------------------------------------------------------

def scan_with_clamav(path: Path) -> Optional[str]:
    """
    Runs clamscan on the file. Returns an error string if infected or scan fails,
    None if clean. Skipped silently if ClamAV is not enabled or not installed.
    """
    if not CLAMAV_ENABLED:
        return None

    try:
        result = subprocess.run(
            [CLAMAV_CMD, "--no-summary", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            log.info(f"ClamAV: clean — {path.name}")
            return None
        elif result.returncode == 1:
            threat = result.stdout.strip().splitlines()[0] if result.stdout else "unknown threat"
            return f"ClamAV: VIRUS DETECTED — {threat}"
        else:
            # returncode 2 = scan error (e.g. permission issue). Log but don't block.
            log.warning(f"ClamAV scan error for {path.name}: {result.stderr.strip()}")
            return None

    except FileNotFoundError:
        log.warning(f"ClamAV not found at {CLAMAV_CMD!r} — skipping AV scan. Set CLAMAV_CMD in .env.")
        return None
    except subprocess.TimeoutExpired:
        return f"ClamAV scan timed out for {path.name}"


# ---------------------------------------------------------------------------
# 6. Sender allowlist
# ---------------------------------------------------------------------------

def is_allowed_sender(sender_email: str, allowed_domains: set[str], allowed_emails: set[str]) -> bool:
    """
    Returns True if the sender is in either the domain allowlist or the exact email allowlist.
    Personal senders (hotmail.com, gmail.com) are matched by exact address, not domain.
    Pass empty sets to allow all senders (not recommended).
    """
    if not allowed_domains and not allowed_emails:
        return True
    email_lower = sender_email.lower()
    domain = email_lower.split("@")[-1] if "@" in email_lower else ""
    return email_lower in allowed_emails or domain in allowed_domains


# ---------------------------------------------------------------------------
# 6. Prompt injection scrubbing
# ---------------------------------------------------------------------------

def scrub_prompt_injection(text: str) -> str:
    """
    Remove phrases that look like LLM instruction injection from extracted text.
    Call this on raw OCR/PDF text before feeding it to any parser or LLM.
    """
    cleaned = _INJECTION_RE.sub("[REDACTED]", text)
    if cleaned != text:
        log.warning("Prompt injection pattern detected and scrubbed from extracted text.")
    return cleaned


# ---------------------------------------------------------------------------
# Combined gate — call this before processing any attachment
# ---------------------------------------------------------------------------

def validate_attachment(path: Path, sender_email: str, allowed_domains: set[str], allowed_emails: set[str] = set()) -> tuple[bool, list[str]]:
    """
    Runs all checks on an attachment before it is processed.

    Returns:
        (ok, issues)
        ok     — False means the file should be skipped entirely
        issues — list of warning/error strings (may be non-empty even when ok=True for warnings)
    """
    issues = []
    fatal = False

    size_err = check_file_size(path)
    if size_err:
        issues.append(size_err)
        fatal = True

    magic_err = check_magic_bytes(path)
    if magic_err:
        issues.append(magic_err)
        fatal = True

    pdf_fatal, pdf_warnings = scan_pdf_structure(path)
    issues.extend(pdf_fatal)
    issues.extend(pdf_warnings)   # warnings land in the log but don't block
    if pdf_fatal:
        fatal = True

    av_err = scan_with_clamav(path)
    if av_err:
        issues.append(av_err)
        fatal = True

    if not is_allowed_sender(sender_email, allowed_domains, allowed_emails):
        issues.append(f"Sender not in allowlist: {sender_email}")
        fatal = True

    return not fatal, issues
