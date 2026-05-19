@echo off
title Invoice Flow v2 Launcher
cls
color 0B

echo =======================================================================
echo              ___                      _              ___ _               
echo             ^|_ _^|_ _ __   __  ___ (_)__ ___  ^|_ _^| ^|_ ___ __ __ 
echo              ^| ^| ^| ' \ \ / / / _ \^| / _/ -_)  ^| ^| ^|  _/ _ \ \ / 
echo             ^|___^|_^|_^|_\_/_/  \___/^|_\__\___^| ^|___^|_^| \__/_/\_\ 
echo                                                                      
echo            Deterministic Human-in-the-Loop Invoice Pipeline
echo =======================================================================
echo Please choose an option:
echo   [1] Start Web Operator Dashboard (FastAPI + Visual Review Interface)
echo   [2] Run Email Processing Pipeline (Polls Outlook Inbox)
echo   [3] Run Full Local Unit Test Suite
echo   [4] Import Local Document File (.pdf, .xlsx, .csv)
echo   [5] Exit Launcher
echo.
echo =======================================================================
set /p choice="Enter option [1-5]: "

if "%choice%"=="1" goto dashboard
if "%choice%"=="2" goto pipeline
if "%choice%"=="3" goto tests
if "%choice%"=="4" goto import
if "%choice%"=="5" exit
goto invalid

:dashboard
echo.
echo [+] Starting FastAPI Web Dashboard on http://127.0.0.1:8000...
echo [+] Automatically opening your browser...
start "" "http://127.0.0.1:8000"
echo.
"c:\git\ms_outlook\v1\.venv\Scripts\python.exe" v2/dashboard/main.py
pause
goto exit

:pipeline
echo.
echo [+] Starting Email Processing Pipeline...
echo.
"c:\git\ms_outlook\v1\.venv\Scripts\python.exe" v2/main.py
echo.
echo [+] Pipeline run complete.
pause
goto exit

:tests
echo.
echo [+] Running Local Unit Test Suite...
echo.
"c:\git\ms_outlook\v1\.venv\Scripts\python.exe" v2/tests/test_parser.py
"c:\git\ms_outlook\v1\.venv\Scripts\python.exe" v2/tests/test_db.py
"c:\git\ms_outlook\v1\.venv\Scripts\python.exe" v2/tests/test_bootstrap.py
echo.
echo [+] All unit tests executed.
pause
goto exit

:import
echo.
echo =======================================================================
echo          IMPORT / INGEST LOCAL INVOICE DOCUMENT
echo =======================================================================
echo.
echo TIP: You can drag and drop your invoice file directly into this window!
set /p filepath="Enter path to your invoice file: "
echo.
echo [+] Ingesting document into SQLite pipeline DB...
"c:\git\ms_outlook\v1\.venv\Scripts\python.exe" v2/ingest_local.py %filepath%
echo.
pause
goto exit

:invalid
echo.
echo [x] Invalid choice, please try again.
pause
goto exit

:exit
exit
