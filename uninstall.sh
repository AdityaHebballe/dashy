#!/usr/bin/env bash
set -euo pipefail

APP_NAME="dashy"
INSTALL_DIR="${HOME}/.local/share/${APP_NAME}"
SYSTEMD_DIR="${HOME}/.config/systemd/user"
SERVICE_FILE="${SYSTEMD_DIR}/${APP_NAME}.service"
CACHE_DIR="${HOME}/.cache/dashy"
CONFIG_DIR="${HOME}/.config/dashy"
CONFIG_LAUNCHER="${HOME}/.local/bin/dashy-config"
SYSTEM_SERVICE_NAME="phone-post-wake.service"
SYSTEM_SERVICE_FILE="/etc/systemd/system/${SYSTEM_SERVICE_NAME}"
SYSTEM_SCRIPT_FILE="/usr/local/bin/dashy-sleep-pc"

systemctl --user disable --now "${APP_NAME}.service" 2>/dev/null || true
rm -f "${SERVICE_FILE}"
systemctl --user daemon-reload

if command -v sudo >/dev/null 2>&1; then
    sudo systemctl disable "${SYSTEM_SERVICE_NAME}" >/dev/null 2>&1 || true
    sudo rm -f "${SYSTEM_SERVICE_FILE}"
    sudo rm -f "${SYSTEM_SCRIPT_FILE}"
    sudo systemctl daemon-reload
fi

rm -rf "${INSTALL_DIR}"
rm -rf "${CACHE_DIR}"
rm -rf "${CONFIG_DIR}"
rm -f "${CONFIG_LAUNCHER}"

echo "Dashy uninstalled."
