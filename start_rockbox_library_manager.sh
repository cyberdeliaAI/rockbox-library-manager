#!/usr/bin/env sh
cd "$(dirname "$0")" || exit 1
if [ -z "${PYTHON_BIN:-}" ]; then
    if [ -x .venv/bin/python ]; then
        PYTHON_BIN=".venv/bin/python"
    else
        PYTHON_BIN="python3"
    fi
fi
exec "$PYTHON_BIN" rockbox_library_manager.py
