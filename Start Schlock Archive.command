#!/bin/bash
# Double-click me in Finder to start Schlock Archive.
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 isn't installed."
  echo "Install it from https://www.python.org/downloads/macos/ (or run: xcode-select --install), then try again."
  read -r -p "Press Return to close..."
  exit 1
fi
python3 app.py "$@"
status=$?
if [ $status -ne 0 ]; then
  echo
  read -r -p "Schlock Archive stopped with an error (code $status). Press Return to close..."
fi
