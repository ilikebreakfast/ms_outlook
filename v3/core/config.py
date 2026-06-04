import os
from pathlib import Path

V3_ROOT = Path(__file__).resolve().parent.parent


def data_dir_for(test_mode: bool) -> Path:
    """Resolve the data directory for an explicit environment.

    This is the single source of truth for the test/live split, independent of
    the current process's INVOICE_TEST. Callers that need to reason about a
    different environment than their own (e.g. the attachment selector deciding
    where the parser subprocess will stage files) should use this instead of
    rebuilding the path by hand.
    """
    return V3_ROOT / "tests" / "data" if test_mode else V3_ROOT / "data"


DATA_DIR = data_dir_for(os.environ.get("INVOICE_TEST") == "1")

# ---------------------------------------------------------------------------
# Ollama model config — override via environment variables if needed
# ---------------------------------------------------------------------------
OLLAMA_BASE   = os.environ.get("OLLAMA_BASE",   "http://localhost:11434")
TEXT_MODEL    = os.environ.get("TEXT_MODEL",    "gemma3:4b")
VISION_MODEL  = os.environ.get("VISION_MODEL",  "gemma3:4b")
# Set VISION_ENABLED=1 to allow image-based extraction (requires GPU acceleration).
# Defaults off — vision models on CPU are too slow for production use.
VISION_ENABLED = os.environ.get("VISION_ENABLED", "0") == "1"
