#!/usr/bin/env bash
# Builds (if needed) and installs SSH Browser into /Applications.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -d "dist/SSH Browser.app" ] || scripts/build_app.sh
DEST="/Applications/SSH Browser.app"
rm -rf "$DEST"
cp -R "dist/SSH Browser.app" "$DEST"
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true
echo "Installed: $DEST"
