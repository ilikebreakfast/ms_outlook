# CLAUDE.md

This file provides guidance and command references to Claude Code (`claude.ai/code`) when working with code in this repository.

> [!IMPORTANT]
> **AI Developer Guidelines:** For detailed instructions on the dual-version architecture (`v1`/`v2`), independent `.gitignore` rules, batch script safety, and virtual environment setup, refer to the global **[AGENTS.md](file:///c:/git/ms_outlook/AGENTS.md)** developer guide first.

---

## Command Reference

### Environment Setup
To set up or update the primary virtual environment and install all dependencies:
```bash
# Run launcher and choose Option 0
run.bat
```
*(Or manually create it at `v2\venv` and install `v2/requirements.txt`)*

### Starting the FastAPI Operator Dashboard
```bash
# Via launcher (Option 1)
run.bat

# Or direct invocation
"v2\venv\Scripts\python.exe" v2/dashboard/main.py
```

### Running the Email processing pipeline
```bash
# Via launcher (Option 2)
run.bat

# Or direct invocation
"v2\venv\Scripts\python.exe" v2/main.py
```

### Running the Unit Test Suite
To run the full test suite sequentially:
```bash
# Via launcher (Option 3)
run.bat

# Or direct invocation
"v2\venv\Scripts\python.exe" v2/tests/test_parser.py
"v2\venv\Scripts\python.exe" v2/tests/test_db.py
"v2\venv\Scripts\python.exe" v2/tests/test_bootstrap.py
```

---

## Development Code Style & Guidelines

* **Version Independence:** Work inside `v2/` for all modern feature enhancements. Leave `v1/` unchanged as it is legacy.
* **Imports:** Ensure any new `v2` modules maintain import-safety relative to the `v2` root.
* **Comments:** Maintain docstrings and inline code comments as specified in the [AGENTS.md](file:///c:/git/ms_outlook/AGENTS.md) guide.
