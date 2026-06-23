#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"
export PYTHONUNBUFFERED=1

if [[ -x ".venv/bin/python3" ]]; then
  PYTHON_EXE=".venv/bin/python3"
else
  PYTHON_EXE="python3"
fi

"$PYTHON_EXE" scripts/upload_instrument_data.py
