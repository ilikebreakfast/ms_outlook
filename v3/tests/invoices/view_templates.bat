@echo off
set PYTHON="%~dp0..\..\..\v2\venv\Scripts\python.exe"
"%~dp0..\..\..\v2\venv\Scripts\python.exe" "%~dp0scripts\view_templates.py" %*
pause
