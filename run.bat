@echo off
title Invoice Flow v2 Launcher
color 0B

:menu
cls
echo =======================================================================
echo              ___                      _              ___ _               
echo             ^|_ _^|_ _ __   __  ___ (_)__ ___  ^|_ _^| ^|_ ___ __ __ 
echo              ^| ^| ^| ' \ \ / / / _ \^| / _/ -_)  ^| ^| ^|  _/ _ \ \ / 
echo             ^|___^|_^|_^|_\_/_/  \___/^|_\__\___^| ^|___^|_^| \__/_/\_\ 
echo                                                                      
echo            Deterministic Human-in-the-Loop Invoice Pipeline
echo =======================================================================
echo Please choose an option:
echo   [0] Setup/Update Virtual Environment (Install dependencies)
echo   [1] Start Web Operator Dashboard (FastAPI + Visual Review Interface)
echo   [2] Run Email Processing Pipeline (Polls Outlook Inbox)
echo   [3] Run Full Local Unit Test Suite
echo   [4] Import Local Document File (.pdf, .xlsx, .csv)
echo   [5] Exit Launcher
echo.
echo =======================================================================
set /p choice="Enter option [0-5]: "

if "%choice%"=="0" goto setup
if "%choice%"=="1" goto dashboard
if "%choice%"=="2" goto pipeline
if "%choice%"=="3" goto tests
if "%choice%"=="4" goto import
if "%choice%"=="5" goto exit_script
goto invalid

:setup
echo.
echo =======================================================================
echo          INITIALIZE / UPDATE VIRTUAL ENVIRONMENT
echo =======================================================================
echo.
where python >nul 2>nul
if errorlevel 1 goto no_python

if exist "v2\venv\Scripts\python.exe" goto venv_exists

echo [+] Creating a new virtual environment in v2\venv...
python -m venv v2\venv
goto venv_done

:venv_exists
echo [+] Existing virtual environment found in v2\venv.

:venv_done
echo [+] Upgrading pip to the latest version...
"v2\venv\Scripts\python.exe" -m pip install --upgrade pip
echo [+] Installing/updating dependencies from v2/requirements.txt...
"v2\venv\Scripts\python.exe" -m pip install -r v2/requirements.txt
echo.
echo [+] Setup completed successfully!
pause
goto menu

:no_python
echo [x] Error: python was not found in your system PATH.
echo     Please install Python (3.10+) and ensure "Add Python to PATH" is checked.
pause
goto menu

:dashboard
echo.
if not exist "v2\venv\Scripts\python.exe" goto venv_missing
echo [+] Starting FastAPI Web Dashboard on http://127.0.0.1:8000...
echo [+] Automatically opening your browser...
start "" "http://127.0.0.1:8000"
echo.
"v2\venv\Scripts\python.exe" v2/dashboard/main.py
pause
goto menu

:pipeline
echo.
if not exist "v2\venv\Scripts\python.exe" goto venv_missing
echo [+] Starting Email Processing Pipeline...
echo.
"v2\venv\Scripts\python.exe" v2/main.py
echo.
echo [+] Pipeline run complete.
pause
goto menu

:tests
echo.
if not exist "v2\venv\Scripts\python.exe" goto venv_missing
echo [+] Running Local Unit Test Suite...
echo.
"v2\venv\Scripts\python.exe" v2/tests/test_parser.py
"v2\venv\Scripts\python.exe" v2/tests/test_db.py
"v2\venv\Scripts\python.exe" v2/tests/test_bootstrap.py
echo.
echo [+] All unit tests executed.
pause
goto menu

:import
echo.
if not exist "v2\venv\Scripts\python.exe" goto venv_missing
echo =======================================================================
echo          IMPORT / INGEST LOCAL INVOICE DOCUMENT
echo =======================================================================
echo.
echo TIP: You can drag and drop your invoice file directly into this window!
set /p filepath="Enter path to your invoice file (leave blank to scan v2/data/attachments): "
echo.
echo [+] Ingesting document into SQLite pipeline DB...
"v2\venv\Scripts\python.exe" v2/ingest_local.py "%filepath%"
echo.
pause
goto menu

:venv_missing
echo.
echo [x] Error: Virtual environment is missing or incomplete!
echo     You must first initialize the environment and install all dependencies.
echo     Please choose Option [0] from the main menu.
echo.
pause
goto menu

:invalid
echo.
echo [x] Invalid choice, please try again.
pause
goto menu

:exit_script
exit
