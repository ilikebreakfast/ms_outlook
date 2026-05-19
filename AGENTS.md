# AI Agents Developer Guide (AGENTS.md)

Welcome, AI Agent! This guide outlines the architecture of the **MS Outlook Invoice Processing Pipeline** repository, its dual-version layout, the shared execution environment, and key development guidelines you must follow.

---

## 1. Repository Layout & Architecture

This repository is structured as a transition from a legacy pipeline (`v1`) to a modern modular system (`v2`), which will eventually fully replace `v1`.

```
ms_outlook/
│
├── run.bat                 # Root multi-version command launcher
├── AGENTS.md               # This documentation file for AI agents
├── CLAUDE.md               # Quick-reference and commands for Claude agents
├── GEMINI.md               # Quick-reference and commands for Gemini agents
│
├── v1/                     # Legacy email & invoice processing pipeline
│   ├── venv/               # Legacy local venv (deprecated)
│   ├── .gitignore          # v1-specific gitignore rules (independent)
│   ├── CLAUDE.md           # Legacy Claude.md (v1-specific)
│   └── ...
│
└── v2/                     # Modern email & invoice processing pipeline (Target Version)
    ├── venv/               # Shared virtual environment (created by run.bat)
    ├── .gitignore          # v2-specific gitignore rules (independent)
    ├── dashboard/          # FastAPI operator dashboard and static assets
    ├── data/               # Gitignored runtime data (attachments, logs, SQLite DB)
    └── ...
```

---

## 2. Shared Virtual Environment & Setup

To make the codebase self-contained and simplify dependency management, **`v2\venv`** is the primary virtual environment for the entire repository.

* **Setup Tool:** Run `run.bat` and select **Option `[0]`** to initialize or update this virtual environment. It automatically:
  1. Checks for a global Python installation (Python 3.10+ recommended).
  2. Runs `python -m venv v2\venv` if it doesn't exist.
  3. Upgrades `pip` to the latest version.
  4. Installs all packages specified in `v2/requirements.txt`.
* **Python Executable:** All scripts (including those in `v2`) must be executed using the interpreter at:
  ```
  v2\venv\Scripts\python.exe
  ```

---

## 3. Command Launcher (`run.bat`)

The root `run.bat` provides a keyboard-driven terminal menu. It automatically verifies that `v2\venv` exists before attempting execution of options 1-4.

* **`[0]` Setup/Update Virtual Environment:** Initializes or rebuilds `v2\venv` and installs all dependencies.
* **`[1]` Start Web Operator Dashboard:** Launches the FastAPI operator dashboard at `http://127.0.0.1:8000` via Uvicorn.
* **`[2]` Run Email Processing Pipeline:** Runs the `v2/main.py` pipeline (which polls the inbox, downloads attachments, extracts text, and parses fields).
* **`[3]` Run Full Local Unit Test Suite:** Executes tests in `v2/tests/` sequentially:
  * `test_parser.py`
  * `test_db.py`
  * `test_bootstrap.py`
* **`[4]` Import Local Document File:** Ingests a local file (.pdf, .xlsx, .csv) into the SQLite database.
* **`[5]` Exit Launcher:** Exits the command prompt window.

---

## 4. Key Guidelines for AI Agents (Important!)

When pair programming or editing code in this workspace, you **must** adhere to the following rules:

### A. Independent `.gitignore` Files
* Keep the root `.gitignore`, `v1/.gitignore`, and `v2/.gitignore` **completely independent and specific to their respective levels**.
* **Do not** hardcode subfolder paths (e.g. `v1/attachments/` or `v2/data/`) inside the root `.gitignore`. 
* Let subfolder `.gitignore` files handle their own folders relatively (e.g. `data/` in `v2/.gitignore` and `attachments/` in `v1/.gitignore`).

### B. Windows Batch File Parsing Safeguards
* **Avoid parenthesized blocks** (e.g. `if ... ( ... )`) in `.bat` scripts when printing or executing commands that might contain brackets or parentheses.
* In Windows Command Prompt, the parser evaluates a closing parenthesis `)` inside a block as the termination of the outer block, which immediately crashes the shell.
* Prefer standard labels and `goto` statements for clean, bulletproof control flow in `run.bat`.

### C. Relative Paths & Portability
* Always reference paths relative to the workspace root or the active script.
* Use `"v2\venv\Scripts\python.exe"` (properly quoted) to invoke the virtual environment.

### D. Documentation Preservation
* Maintain all existing comments, docstrings, and architectural descriptions unless the user explicitly requests a refactor of those descriptions.
