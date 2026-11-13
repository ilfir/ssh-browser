#!/usr/bin/env bash
# Builds dist/SSH Browser.app. Usage: scripts/build_app.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-/opt/homebrew/bin/python3.14}"
[ -x "$PY" ] || PY="$(command -v python3.14 || true)"
[ -x "$PY" ] || { echo "Python 3.14 not found (brew install python@3.14)"; exit 1; }

[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q -r requirements.txt pyinstaller
.venv/bin/python tools/make_icon.py

rm -rf build dist
.venv/bin/pyinstaller --noconfirm --windowed --clean \
  --name "SSH Browser" \
  --icon assets/AppIcon.icns \
  --osx-bundle-identifier com.ilfir.sshbrowser \
  --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets \
  --exclude-module PySide6.QtWebEngineQuick --exclude-module PySide6.QtPdf \
  --exclude-module PySide6.Qt3DCore --exclude-module PySide6.QtQuick --exclude-module PySide6.QtQml \
  run_app.py

# Declare the app category
/usr/libexec/PlistBuddy -c "Add :LSApplicationCategoryType string public.app-category.utilities" \
  "dist/SSH Browser.app/Contents/Info.plist" 2>/dev/null || true
echo "Built: dist/SSH Browser.app"
