# AI Agents Developer Guide (AGENTS.md)

Welcome, AI Agent! This guide outlines the architecture of the **MS Outlook Invoice Processing Pipeline** repository, its dual-version layout, the shared execution environment, and key development guidelines you must follow.

# Behavior guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

Tradeoff: These guidelines bias toward caution over speed. For trivial tasks, use judgment.

1. Think Before Coding
Don't assume. Don't hide confusion. Surface tradeoffs.

Before implementing:

State your assumptions explicitly. If uncertain, ask.
If multiple interpretations exist, present them - don't pick silently.
If a simpler approach exists, say so. Push back when warranted.
If something is unclear, stop. Name what's confusing. Ask.
2. Simplicity First
Minimum code that solves the problem. Nothing speculative.

No features beyond what was asked.
No abstractions for single-use code.
No "flexibility" or "configurability" that wasn't requested.
No error handling for impossible scenarios.
If you write 200 lines and it could be 50, rewrite it.
Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

3. Surgical Changes
Touch only what you must. Clean up only your own mess.

When editing existing code:

Don't "improve" adjacent code, comments, or formatting.
Don't refactor things that aren't broken.
Match existing style, even if you'd do it differently.
If you notice unrelated dead code, mention it - don't delete it.
When your changes create orphans:

Remove imports/variables/functions that YOUR changes made unused.
Don't remove pre-existing dead code unless asked.
The test: Every changed line should trace directly to the user's request.

4. Goal-Driven Execution
Define success criteria. Loop until verified.

Transform tasks into verifiable goals:

"Add validation" → "Write tests for invalid inputs, then make them pass"
"Fix the bug" → "Write a test that reproduces it, then make it pass"
"Refactor X" → "Ensure tests pass before and after"
For multi-step tasks, state a brief plan:

1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

These guidelines are working if: fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

---

## 1. Repository Layout & Architecture

This repository is structured as a progressive implementation of the MS Outlook Invoice Processing Pipeline, spanning legacy (`v1`), the intermediate modern version (`v2`), and the current step-by-step active development version (`v3`).

> **IMPORTANT:** Each version has its own specific AI agent guidelines. When working within a specific directory, you **MUST** refer to its individual `AGENTS.md` file:
> * **v1:** [v1/AGENTS.md](file:///c:/git/ms_outlook/v1/AGENTS.md)
> * **v2:** [v2/AGENTS.md](file:///c:/git/ms_outlook/v2/AGENTS.md)
> * **v3:** [v3/AGENTS.md](file:///c:/git/ms_outlook/v3/AGENTS.md)

```
ms_outlook/
│
├── run.bat                 # Root multi-version command launcher
├── AGENTS.md               # This documentation file for AI agents
├── CLAUDE.md               # Quick-reference and commands for Claude agents
├── GEMINI.md               # Quick-reference and commands for Gemini agents
│
├── v1/                     # Legacy email & invoice processing pipeline (deprecated)
│
├── v2/                     # Modern email & invoice processing pipeline (previous version)
│
└── v3/                     # Active Development: Simple step-by-step robust pipeline
    ├── .gitignore          # v3-specific gitignore rules (independent)
    ├── data/               # Local mock attachments, JSON configs, and pipeline DB
    └── ...
```

---

## 2. Shared Virtual Environment & Setup

To make the codebase self-contained and simplify dependency management, **`v2\venv`** is the primary virtual environment for the entire repository (shares dependencies and interpreter for both `v2` and `v3` scripts).

* **Setup Tool:** Run `run.bat` and select **Option `[0]`** to initialize or update this virtual environment. It automatically:
  1. Checks for a global Python installation (Python 3.10+ recommended).
  2. Runs `python -m venv v2\venv` if it doesn't exist.
  3. Upgrades `pip` to the latest version.
  4. Installs all packages specified in `v2/requirements.txt`.
* **Python Executable:** All scripts (including those in `v2` and `v3`) must be executed using the interpreter at:
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
* Keep the root `.gitignore`, `v1/.gitignore`, `v2/.gitignore`, and `v3/.gitignore` **completely independent and specific to their respective levels**.
* **Do not** hardcode subfolder paths (e.g. `v1/attachments/`, `v2/data/`, or `v3/data/`) inside the root `.gitignore`. 
* Let subfolder `.gitignore` files handle their own folders relatively (e.g. `data/` in `v3/.gitignore` or `v2/.gitignore` and `attachments/` in `v1/.gitignore`).

### B. Windows Batch File Parsing Safeguards
* **Avoid parenthesized blocks** (e.g. `if ... ( ... )`) in `.bat` scripts when printing or executing commands that might contain brackets or parentheses.
* In Windows Command Prompt, the parser evaluates a closing parenthesis `)` inside a block as the termination of the outer block, which immediately crashes the shell.
* Prefer standard labels and `goto` statements for clean, bulletproof control flow in `run.bat`.

### C. Relative Paths & Portability
* Always reference paths relative to the workspace root or the active script.
* Use `"v2\venv\Scripts\python.exe"` (properly quoted) to invoke the virtual environment for python files in both `v2` and `v3`.

### D. Documentation Preservation
* Maintain all existing comments, docstrings, and architectural descriptions unless the user explicitly requests a refactor of those descriptions.


