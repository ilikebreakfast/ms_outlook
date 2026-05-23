import os
from pathlib import Path

V3_ROOT = Path(__file__).resolve().parent.parent

if os.environ.get("INVOICE_TEST") == "1":
    DATA_DIR = V3_ROOT / "tests" / "data"
else:
    DATA_DIR = V3_ROOT / "data"

# ---------------------------------------------------------------------------
# Ollama model config — override via environment variables if needed
# ---------------------------------------------------------------------------
OLLAMA_BASE   = os.environ.get("OLLAMA_BASE",   "http://localhost:11434")
TEXT_MODEL    = os.environ.get("TEXT_MODEL",    "gemma3:4b")
VISION_MODEL  = os.environ.get("VISION_MODEL",  "gemma3:4b")
# Set VISION_ENABLED=1 to allow image-based extraction (requires GPU acceleration).
# Defaults off — vision models on CPU are too slow for production use.
VISION_ENABLED = os.environ.get("VISION_ENABLED", "0") == "1"
