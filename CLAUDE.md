# CLAUDE.md

This file provides guidance and command references to Claude Code (`claude.ai/code`) when working with code in this repository.

> [!IMPORTANT]
> **AI Developer Guidelines:** For detailed instructions on the multi-version architecture (`v1`/`v2`/`v3`), independent `.gitignore` rules, batch script safety, and virtual environment setup, refer to the global **[AGENTS.md](file:///c:/git/ms_outlook/AGENTS.md)** developer guide first.

---

## Command Reference

### Environment Setup
To set up or update the primary virtual environment and install all dependencies:
```bash
# Run launcher and choose Option 0
run.bat
```
*(Or manually create it at `v2\venv` and install `v2/requirements.txt`)*

### Running v3 Pipeline Tasks (Active)
```bash
# Executing active Python scripts in v3 (use the v2 shared venv)
"v2\venv\Scripts\python.exe" v3/some_script.py
```

### Running the Legacy Web Operator Dashboard (v2)
```bash
# Direct invocation
"v2\venv\Scripts\python.exe" v2/dashboard/main.py
```

---

## Development Code Style & Guidelines

* **Version Independence:** Work inside `v3/` for all modern active feature enhancements. Leave `v1/` and `v2/` unchanged unless requested.
* **Imports:** Ensure any new `v3` modules maintain import-safety relative to the `v3` root.
* **Comments:** Maintain docstrings and inline code comments as specified in the [AGENTS.md](file:///c:/git/ms_outlook/AGENTS.md) guide.
