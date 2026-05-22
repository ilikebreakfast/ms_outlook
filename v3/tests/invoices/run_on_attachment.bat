@echo off
rem Usage:
rem   run_on_attachment.bat path\to\invoice.pdf
rem   run_on_attachment.bat path\to\invoice.pdf --auto
rem   run_on_attachment.bat --review-pending

set PYTHON="%~dp0..\..\..\v2\venv\Scripts\python.exe"
set PARSER="%~dp0..\..\invoice_parser.py"

if "%~1"=="" (
    echo.
    echo Usage:
    echo   run_on_attachment.bat path\to\invoice.pdf
    echo   run_on_attachment.bat path\to\invoice.pdf --auto
    echo   run_on_attachment.bat --review-pending
    echo.
    pause
    exit /b 1
)

%PYTHON% %PARSER% %*
pause
