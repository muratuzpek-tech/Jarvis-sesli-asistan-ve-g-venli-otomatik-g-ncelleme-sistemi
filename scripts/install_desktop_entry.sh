#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
ICON="$ROOT/assets/jarvis.svg"
APP_DIR="$HOME/.local/share/applications"
DESKTOP_FILE="$APP_DIR/jarvis-ai.desktop"
DESKTOP_LINK="$HOME/Desktop/JARVIS-AI.desktop"

if [[ ! -x "$PYTHON" ]]; then
  echo "HATA: Python ortamı bulunamadı: $PYTHON" >&2
  echo "Önce .venv kurulumu tamamlanmalı." >&2
  exit 1
fi
if [[ ! -f "$ICON" ]]; then
  echo "HATA: İkon bulunamadı: $ICON" >&2
  exit 1
fi

mkdir -p "$APP_DIR" "$HOME/Desktop"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=JARVIS AI
GenericName=Premium AI Workstation
Comment=JARVIS sesli yapay zeka asistanı
Path=$ROOT
Exec=env JARVIS_AUTO_DISCOVERY=0 JARVIS_ALLOW_DEP_INSTALL=0 $PYTHON -m jarvis
Icon=$ICON
Terminal=false
StartupNotify=true
Categories=Utility;Office;AudioVideo;
Keywords=JARVIS;AI;Assistant;Voice;
EOF

cp "$DESKTOP_FILE" "$DESKTOP_LINK"
chmod 644 "$DESKTOP_FILE"
chmod +x "$DESKTOP_LINK"
if command -v gio >/dev/null 2>&1; then
  gio set "$DESKTOP_LINK" metadata::trusted true 2>/dev/null || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
fi

echo "JARVIS masaüstü başlatıcısı hazır: $DESKTOP_LINK"
echo "Uygulama menüsü kaydı: $DESKTOP_FILE"
