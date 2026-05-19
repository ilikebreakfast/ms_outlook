import re
import subprocess
import logging
from pathlib import Path
from typing import Optional, Tuple, List

from core.settings import CLAMAV_ENABLED, CLAMAV_CMD, MAX_FILE_SIZE_MB

log = logging.getLogger(__name__)

# Accepted magic byte headers
MAGIC_BYTES = {
    ".pdf":  [b"%PDF"],
    ".png":  [b"\x89PNG"],
    ".jpg":  [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
    ".tiff": [b"II*\x00", b"MM\x00*"],
    ".bmp":  [b"BM"],
    ".xlsx": [b"PK\x03\x04"],  # ZIP container
    ".xls":  [b"\xD0\xCF\x11\xE0"],  # OLE2 (BIFF)
    ".csv":  [],  # Verified via UTF-8 decode
}

# Dangerous PDF markers
PDF_FATAL_PATTERNS = [
    (b"/JavaScript", "embedded JavaScript"),
    (b"/JS",         "embedded JavaScript (short form)"),
    (b"/Launch",     "Launch action (executes external program)"),
    (b"/XFA",        "XFA form (XML Forms Architecture, commonly abused)"),
]

PDF_ACTION_TRIGGERS = [b"/AA", b"/OpenAction"]
PDF_EXEC_PAYLOADS = [b"/JavaScript", b"/JS", b"/Launch"]

PDF_WARN_PATTERNS = [
    (b"/EmbeddedFile", "embedded file attachment (informational — not blocked)"),
    (b"/AA",           "auto-action trigger (no executable payload detected — informational)"),
    (b"/OpenAction",   "OpenAction trigger (no executable payload detected — informational)"),
]

# Prompt injection filters
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


def sanitise_filename(name: str) -> str:
    """Sanitise target file names, stripping relative paths and special characters."""
    safe = Path(name).name
    safe = re.sub(r"[^\w.\-() ]", "_", safe)
    return safe or "unnamed"


def check_file_size(path: Path) -> Optional[str]:
    """Verify that file size does not exceed system limit."""
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        return f"File too large: {size_mb:.1f} MB (limit {MAX_FILE_SIZE_MB} MB)"
    return None


def check_magic_bytes(path: Path) -> Optional[str]:
    """Ensure raw file content headers match the declared file extension."""
    ext = path.suffix.lower()
    if ext not in MAGIC_BYTES:
        return f"Unsupported extension: {ext}"

    expected = MAGIC_BYTES[ext]
    if not expected:
        # Verify plain-text decodes as UTF-8
        try:
            path.read_bytes()[:4096].decode("utf-8")
        except UnicodeDecodeError:
            return f"File with extension {ext} is not valid UTF-8 text"
        return None

    try:
        header = path.read_bytes()[:8]
        if not any(header.startswith(magic) for magic in expected):
            return f"Magic bytes don't match extension {ext} — possible spoofed file type"
    except Exception as exc:
        return f"Could not read magic bytes: {exc}"
    return None


def scan_pdf_structure(path: Path) -> Tuple[List[str], List[str]]:
    """Scan PDF bytes for potential code execution vectors or dangerous properties."""
    if path.suffix.lower() != ".pdf":
        return [], []

    fatal_issues: List[str] = []
    warnings: List[str] = []
    
    try:
        data = path.read_bytes()
        for pattern, label in PDF_FATAL_PATTERNS:
            if pattern in data:
                fatal_issues.append(f"Blocked — dangerous PDF structure: {label}")
                
        has_exec_payload = any(p in data for p in PDF_EXEC_PAYLOADS)
        for trigger in PDF_ACTION_TRIGGERS:
            if trigger in data:
                if has_exec_payload:
                    fatal_issues.append(
                        f"Blocked — dangerous PDF structure: {trigger.decode()} with executable payload"
                    )
                    
        for pattern, label in PDF_WARN_PATTERNS:
            if pattern in data:
                if pattern in PDF_ACTION_TRIGGERS and has_exec_payload:
                    continue
                warnings.append(f"PDF note: {label}")
    except Exception as exc:
        fatal_issues.append(f"Could not scan PDF structure: {exc}")
        
    return fatal_issues, warnings


def scan_with_clamav(path: Path) -> Optional[str]:
    """Execute clamscan on target file if enabled in config."""
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
            log.warning(f"ClamAV scan error for {path.name}: {result.stderr.strip()}")
            return None
    except FileNotFoundError:
        log.warning("ClamAV bin not found — skipping scan.")
        return None
    except subprocess.TimeoutExpired:
        return f"ClamAV scan timed out for {path.name}"


def scrub_prompt_injection(text: str) -> str:
    """Scrub raw text vectors to eliminate potential prompt injection attacks."""
    cleaned = _INJECTION_RE.sub("[REDACTED]", text)
    if cleaned != text:
        log.warning("Prompt injection pattern scrubbed from raw document text.")
    return cleaned


def validate_attachment(path: Path) -> Tuple[bool, List[str]]:
    """
    Run full suite of security gates on an attachment.
    Returns:
        (is_approved, issues)
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
    issues.extend(pdf_warnings)
    if pdf_fatal:
        fatal = True

    av_err = scan_with_clamav(path)
    if av_err:
        issues.append(av_err)
        fatal = True

    return not fatal, issues
