@echo off
setlocal

cd /d "%~dp0.."
set "PYTHONUNBUFFERED=1"

if exist ".venv\Scripts\python.exe" (
  set "PYTHON_EXE=.venv\Scripts\python.exe"
) else (
  set "PYTHON_EXE=python"
)

"%PYTHON_EXE%" scripts\upload_instrument_data.py >> collector.log 2>&1
exit /b %ERRORLEVEL%
