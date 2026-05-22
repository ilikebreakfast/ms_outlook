# GEMINI.md

This file provides guidance and command references to Gemini-based AI assistants (such as Antigravity) when working with code in this repository.

> [!IMPORTANT]
> **AI Developer Guidelines:** For detailed instructions on the multi-version architecture (`v1`/`v2`/`v3`), independent `.gitignore` rules, batch script safety, and virtual environment setup, refer to the global **[AGENTS.md](file:///c:/git/ms_outlook/AGENTS.md)** developer guide first.

---

## Command Reference

### Environment Setup
To set up or update the primary virtual environment and install all dependencies:
```powershell
# Run launcher and choose Option 0
.\run.bat
```
*(Or manually create it at `v2\venv` and install `v2/requirements.txt`)*

### Running v3 Pipeline Tasks (Active)
```powershell
# Executing active Python scripts in v3 (use the v2 shared venv)
& "v2\venv\Scripts\python.exe" v3/some_script.py
```

### Starting the Legacy Web Operator Dashboard (v2)
```powershell
# Direct invocation
& "v2\venv\Scripts\python.exe" v2/dashboard/main.py
```

---

## Development Code Style & Guidelines

* **Version Independence:** Work inside `v3/` for all active modern feature enhancements. Leave `v1/` and `v2/` unchanged unless requested.
* **PowerShell Compatibility:** Remember that terminal commands executed on Windows via AI tools run in PowerShell; use the call operator `&` for quoted paths, and `.\run.bat` for local scripts.
* **Comments:** Maintain docstrings and inline code comments as specified in the [AGENTS.md](file:///c:/git/ms_outlook/AGENTS.md) guide.
