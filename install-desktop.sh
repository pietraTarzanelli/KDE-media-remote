#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/applications"
DESKTOP_FILE="$APP_DIR/kde-media-remote.desktop"

mkdir -p "$APP_DIR"

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=KDE Media Remote
Comment=Avvia il server del telecomando web
Exec=$PROJECT_DIR/.venv/bin/python $PROJECT_DIR/run.py
Path=$PROJECT_DIR
Icon=input-gaming
Terminal=false
Categories=Utility;Network;
StartupNotify=false
EOF

chmod +x "$DESKTOP_FILE"

# Refresh KDE application database if available.
if command -v kbuildsycoca6 >/dev/null 2>&1; then
    kbuildsycoca6 >/dev/null 2>&1 || true
elif command -v kbuildsycoca5 >/dev/null 2>&1; then
    kbuildsycoca5 >/dev/null 2>&1 || true
fi

echo "Installato: $DESKTOP_FILE"
echo "Ora cerca 'KDE Media Remote' nel menu applicazioni e aggiungilo alla barra."
