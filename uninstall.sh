#!/usr/bin/env bash
set -euo pipefail

APP_NAME="dashy"
INSTALL_DIR="${HOME}/.local/share/${APP_NAME}"
SYSTEMD_DIR="${HOME}/.config/systemd/user"
SERVICE_FILE="${SYSTEMD_DIR}/${APP_NAME}.service"
CACHE_DIR="${HOME}/.cache/dashy"

systemctl --user disable --now "${APP_NAME}.service" 2>/dev/null || true
rm -f "${SERVICE_FILE}"
systemctl --user daemon-reload

rm -rf "${INSTALL_DIR}"
rm -rf "${CACHE_DIR}"

echo "Dashy uninstalled."
