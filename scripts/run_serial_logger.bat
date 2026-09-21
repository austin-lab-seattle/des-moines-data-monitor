@echo off
setlocal

cd /d "%~dp0.."
set "PYTHONUNBUFFERED=1"

if exist ".venv\Scripts\python.exe" (
  set "PYTHON_EXE=.venv\Scripts\python.exe"
) else (
  set "PYTHON_EXE=python"
)

"%PYTHON_EXE%" scripts\log_serial_instruments.py --config serial_instruments_config.json
exit /b %ERRORLEVEL%
