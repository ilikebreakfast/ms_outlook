# GEMINI.md

This file provides guidance and command references to Gemini-based AI assistants (such as Antigravity) when working with code in this repository.

> [!IMPORTANT]
> **AI Developer Guidelines:** For detailed instructions on the dual-version architecture (`v1`/`v2`), independent `.gitignore` rules, batch script safety, and virtual environment setup, refer to the global **[AGENTS.md](file:///c:/git/ms_outlook/AGENTS.md)** developer guide first.

---

## Command Reference

### Environment Setup
To set up or update the primary virtual environment and install all dependencies:
```powershell
# Run launcher and choose Option 0
.\run.bat
```
*(Or manually create it at `v2\venv` and install `v2/requirements.txt`)*

### Starting the FastAPI Operator Dashboard
```powershell
# Via launcher (Option 1)
.\run.bat

# Or direct invocation
& "v2\venv\Scripts\python.exe" v2/dashboard/main.py
```

### Running the Email processing pipeline
```powershell
# Via launcher (Option 2)
.\run.bat

# Or direct invocation
& "v2\venv\Scripts\python.exe" v2/main.py
```

### Running the Unit Test Suite
To run the full test suite sequentially:
```powershell
# Via launcher (Option 3)
.\run.bat

# Or direct PowerShell invocation
& "v2\venv\Scripts\python.exe" v2/tests/test_parser.py
& "v2\venv\Scripts\python.exe" v2/tests/test_db.py
& "v2\venv\Scripts\python.exe" v2/tests/test_bootstrap.py
```

---

## Development Code Style & Guidelines

* **Version Independence:** Work inside `v2/` for all modern feature enhancements. Leave `v1/` unchanged as it is legacy.
* **PowerShell Compatibility:** Remember that terminal commands executed on Windows via AI tools run in PowerShell; use the call operator `&` for quoted paths, and `.\run.bat` for local scripts.
* **Comments:** Maintain docstrings and inline code comments as specified in the [AGENTS.md](file:///c:/git/ms_outlook/AGENTS.md) guide.
