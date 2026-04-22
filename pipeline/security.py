"""
Security checks applied before any attachment is processed.

Checks (in order):
  1. Filename sanitisation        — strip path traversal
  2. File size limit              — reject oversized files
  3. Magic byte validation        — confirm actual file type matches extension
  4. PDF structure scan           — flag embedded JavaScript, auto-actions, URIs
  5. Sender allowlist             — skip emails from unknown domains
  6. Prompt injection scrubbing   — sanitise extracted text before it enters any parser

None of these checks modify or execute the file. They only read bytes.
"""
import logging
import re
from pathlib import Path
from typing import Optional

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
}

# PDF internal keywords that indicate active/dangerous content
PDF_DANGER_PATTERNS = [
    b"/JavaScript",
    b"/JS",
    b"/AA",          # auto-action (runs on open)
    b"/OpenAction",  # runs on open
    b"/Launch",      # launches external program
    b"/EmbeddedFile",
    b"/XFA",         # XML Forms Architecture — complex, often abused
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

def scan_pdf_structure(path: Path) -> list[str]:
    """
    Reads raw PDF bytes and flags dangerous internal structures.
    Returns a list of warning strings (empty = clean).
    Does NOT parse or execute anything.
    """
    if path.suffix.lower() != ".pdf":
        return []

    warnings = []
    try:
        data = path.read_bytes()
        for pattern in PDF_DANGER_PATTERNS:
            if pattern in data:
                warnings.append(f"Dangerous PDF keyword found: {pattern.decode(errors='replace')}")
    except Exception as exc:
        warnings.append(f"Could not scan PDF structure: {exc}")
    return warnings


# ---------------------------------------------------------------------------
# 5. Sender allowlist
# ---------------------------------------------------------------------------

def is_allowed_sender(sender_email: str, allowed_domains: set[str]) -> bool:
    """
    Returns True if the sender's domain is in the allowlist.
    Pass an empty set to allow all senders (not recommended).
    """
    if not allowed_domains:
        return True
    domain = sender_email.split("@")[-1].lower() if "@" in sender_email else ""
    return domain in allowed_domains


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

def validate_attachment(path: Path, sender_email: str, allowed_domains: set[str]) -> tuple[bool, list[str]]:
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

    pdf_warnings = scan_pdf_structure(path)
    issues.extend(pdf_warnings)
    if pdf_warnings:
        # Treat embedded JS / auto-actions as fatal — do not process
        fatal = True

    if not is_allowed_sender(sender_email, allowed_domains):
        issues.append(f"Sender domain not in allowlist: {sender_email}")
        fatal = True

    return not fatal, issues
