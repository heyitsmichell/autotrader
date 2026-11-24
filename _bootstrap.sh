#!/usr/bin/env bash
# Sourced by setup.sh, start.sh, stop.sh, auth.sh, install_autostart.sh (not run directly).
# Creates .venv and installs requirements when the venv is missing or incomplete, so these
# scripts work on a fresh checkout. Needs Python 3.10-3.12 (the Webull MCP package does not
# support newer versions).
#
# Works on macOS/Linux (.venv/bin/python) and on Windows under Git Bash/MSYS
# (.venv/Scripts/python.exe). After sourcing, call detect_venv_py; VENV_PY is the venv
# interpreter (empty when there is no venv yet).

VENV_PY=""

detect_venv_py() {
  VENV_PY=""
  local c
  for c in ./.venv/bin/python ./.venv/Scripts/python.exe; do
    if [ -x "$c" ]; then VENV_PY="$c"; return 0; fi
  done
  return 1
}

_find_python() {
  local c
  for c in python3.12 python3.11 python3.10; do
    if command -v "$c" >/dev/null 2>&1; then echo "$c"; return 0; fi
  done
  # Windows (Git Bash): the py launcher selects a version
  if command -v py >/dev/null 2>&1; then
    for c in 3.12 3.11 3.10; do
      if py "-$c" -c "import sys" >/dev/null 2>&1; then echo "py -$c"; return 0; fi
    done
  fi
  return 1
}

_venv_version_ok() {
  "$VENV_PY" -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,12) else 1)' 2>/dev/null
}

ensure_venv() {
  if detect_venv_py; then
    if ! _venv_version_ok; then
      echo ".venv uses a Python version the Webull package does not support (need 3.10-3.12)." >&2
      echo "Delete the .venv folder and run this again with Python 3.12." >&2
      exit 1
    fi
    # venv exists but dependencies were never installed (e.g. an interrupted first run)
    if ! "$VENV_PY" -c 'import dotenv' >/dev/null 2>&1; then
      echo "== Installing Python dependencies into .venv =="
      "$VENV_PY" -m pip install --upgrade pip
      "$VENV_PY" -m pip install -r requirements.txt
    fi
    return 0
  fi

  local py
  if ! py="$(_find_python)"; then
    echo "Python 3.10, 3.11 or 3.12 is required (found: $(python3 --version 2>&1))." >&2
    echo "Install one (e.g. 'brew install python@3.12', 'sudo apt install python3.12-venv', or python.org on Windows) and run this again." >&2
    exit 1
  fi
  echo "== Creating .venv with $py and installing dependencies =="
  $py -m venv .venv
  if ! detect_venv_py; then
    echo "Could not find the interpreter in .venv after creating it." >&2
    exit 1
  fi
  "$VENV_PY" -m pip install --upgrade pip
  "$VENV_PY" -m pip install -r requirements.txt
}
