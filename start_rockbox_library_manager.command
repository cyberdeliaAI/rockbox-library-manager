#!/bin/zsh
cd "$(dirname "$0")" || exit 1

if [ -z "${PYTHON_BIN:-}" ]; then
  if [ -x .venv/bin/python ]; then
    PYTHON_BIN=".venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi
"$PYTHON_BIN" rockbox_library_manager.py
app_exit_code=$?

if [ $app_exit_code -ne 0 ]; then
  echo
  echo "Rockbox Library Manager exited with an error."
  echo
  read "?Press Return to close..."
fi
exit $app_exit_code
