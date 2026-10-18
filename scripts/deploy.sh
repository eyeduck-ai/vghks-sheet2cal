#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export UV_PYTHON_INSTALL_DIR="$PWD/.runtime/python"
export UV_PROJECT_ENVIRONMENT="$PWD/.runtime/service-venv"
uv python install 3.11
if systemctl --user is-active --quiet vghks-sheet2cal.service; then
  systemctl --user stop vghks-sheet2cal.service
fi
uv sync --locked --no-editable --no-dev --python 3.11 --reinstall-package vghks-sheet2cal
python3 scripts/install_service.py --install
loginctl --no-ask-password enable-linger "$USER"
systemctl --user status vghks-sheet2cal.service --no-pager
