#!/usr/bin/env bash
set -euo pipefail

APP_NAME="dashy"
INSTALL_DIR="${HOME}/.local/share/${APP_NAME}"
SYSTEMD_DIR="${HOME}/.config/systemd/user"
SERVICE_FILE="${SYSTEMD_DIR}/${APP_NAME}.service"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
HOSTNAME_LOCAL="$(hostname).local"
PORT="${DASHY_PORT:-5000}"

if [[ -z "${PYTHON_BIN}" ]]; then
    echo "python3 not found" >&2
    exit 1
fi

mkdir -p "${INSTALL_DIR}"
mkdir -p "${SYSTEMD_DIR}"

install -m 0644 server.py "${INSTALL_DIR}/server.py"
install -m 0644 index.html "${INSTALL_DIR}/index.html"

cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Dashy local dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=${PYTHON_BIN} ${INSTALL_DIR}/server.py
Environment=PYTHONUNBUFFERED=1
Environment=DASHY_HOST=0.0.0.0
Environment=DASHY_PORT=${PORT}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now "${APP_NAME}.service"

echo
echo "Dashy installed."
echo "Service: ${SERVICE_FILE}"
echo "App dir: ${INSTALL_DIR}"
echo "URL: http://${HOSTNAME_LOCAL}:${PORT}/"
echo
echo "Notes:"
echo "- This is a systemd user service, so it starts automatically when your user session starts."
echo "- For true boot-before-login behavior, enable linger manually:"
echo "  loginctl enable-linger ${USER}"
echo "- .local access works best when Avahi is running on this machine."
