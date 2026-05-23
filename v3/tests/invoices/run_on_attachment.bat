@echo off
set INVOICE_TEST=1
"%~dp0..\..\..\v2\venv\Scripts\python.exe" "%~dp0scripts\run_on_attachment.py"
pause
