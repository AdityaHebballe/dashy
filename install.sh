#!/usr/bin/env bash
set -euo pipefail

APP_NAME="dashy"
INSTALL_DIR="${HOME}/.local/share/${APP_NAME}"
SYSTEMD_DIR="${HOME}/.config/systemd/user"
SERVICE_FILE="${SYSTEMD_DIR}/${APP_NAME}.service"
SYSTEM_SERVICE_NAME="phone-post-wake.service"
SYSTEM_SERVICE_FILE="/etc/systemd/system/${SYSTEM_SERVICE_NAME}"
SYSTEM_SCRIPT_FILE="/usr/local/bin/dashy-sleep-pc"
LOCAL_BIN_DIR="${HOME}/.local/bin"
CONFIG_LAUNCHER="${LOCAL_BIN_DIR}/dashy-config"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
CONFIG_PYTHON="/usr/bin/python3"
VENV_DIR="${INSTALL_DIR}/.venv"
VENV_PYTHON="${VENV_DIR}/bin/python"
VENV_PIP="${VENV_DIR}/bin/pip"
HOSTNAME_LOCAL="$(hostname).local"
PORT="${DASHY_PORT:-5000}"
PHONE_ADB_TARGET="${PHONE_ADB_TARGET:-192.168.0.8:5555}"

if [[ -z "${PYTHON_BIN}" ]]; then
    echo "python3 not found" >&2
    exit 1
fi

mkdir -p "${INSTALL_DIR}"
mkdir -p "${SYSTEMD_DIR}"
mkdir -p "${LOCAL_BIN_DIR}"
mkdir -p "${INSTALL_DIR}/static"
mkdir -p "${INSTALL_DIR}/scripts"

install -m 0644 server.py "${INSTALL_DIR}/server.py"
install -m 0644 index.html "${INSTALL_DIR}/index.html"
install -m 0644 dashy_config.py "${INSTALL_DIR}/dashy_config.py"
install -m 0644 requirements.txt "${INSTALL_DIR}/requirements.txt"
install -m 0644 static/styles.css "${INSTALL_DIR}/static/styles.css"
install -m 0644 static/app.js "${INSTALL_DIR}/static/app.js"
install -m 0755 scripts/dashy-sleep-pc.sh "${INSTALL_DIR}/scripts/dashy-sleep-pc.sh"

cat > "${CONFIG_LAUNCHER}" <<EOF
#!/usr/bin/env bash
exec "${CONFIG_PYTHON}" "${INSTALL_DIR}/dashy_config.py" "\$@"
EOF
chmod 0755 "${CONFIG_LAUNCHER}"

"${PYTHON_BIN}" -m venv "${VENV_DIR}"
"${VENV_PIP}" install --upgrade pip
"${VENV_PIP}" install -r "${INSTALL_DIR}/requirements.txt"

cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Dashy local dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=${VENV_PYTHON} ${INSTALL_DIR}/server.py
Environment=PYTHONUNBUFFERED=1
Environment=DASHY_HOST=0.0.0.0
Environment=DASHY_PORT=${PORT}
Nice=10
IOSchedulingClass=idle
IOSchedulingPriority=7
CPUWeight=1
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable "${APP_NAME}.service" >/dev/null 2>&1 || true
systemctl --user restart "${APP_NAME}.service" >/dev/null 2>&1 || systemctl --user start "${APP_NAME}.service"

if command -v sudo >/dev/null 2>&1; then
    sudo install -m 0755 "${INSTALL_DIR}/scripts/dashy-sleep-pc.sh" "${SYSTEM_SCRIPT_FILE}"
    sudo tee "${SYSTEM_SERVICE_FILE}" >/dev/null <<EOF
[Unit]
Description=Wake Phone AFTER Suspend
After=suspend.target network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=${USER}
Group=$(id -gn)
Environment=HOME=${HOME}
Environment=PHONE_ADB_TARGET=${PHONE_ADB_TARGET}
ExecStart=/bin/bash -lc 'sleep 4; exec "${SYSTEM_SCRIPT_FILE}" --wake-only'

[Install]
WantedBy=suspend.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable "${SYSTEM_SERVICE_NAME}" >/dev/null 2>&1 || true
else
    echo "- Skipped system wake-service install because sudo is not available."
fi

echo
echo "Dashy installed/updated."
echo "Service: ${SERVICE_FILE}"
echo "App dir: ${INSTALL_DIR}"
echo "Venv: ${VENV_DIR}"
echo "URL: http://${HOSTNAME_LOCAL}:${PORT}/"
echo
echo "Notes:"
echo "- This is a systemd user service, so it starts automatically when your user session starts."
echo "- Re-running ./install.sh updates the installed files and restarts the service."
echo "- The service is intentionally de-prioritized for gaming."
echo "- Sleep helper: ${SYSTEM_SCRIPT_FILE}"
echo "- Post-wake service: ${SYSTEM_SERVICE_FILE}"
echo "- Config app launcher: ${CONFIG_LAUNCHER}"
echo "- For true boot-before-login behavior, enable linger manually:"
echo "  loginctl enable-linger ${USER}"
echo "- .local access works best when Avahi is running on this machine."
echo "- Service management commands:"
echo "  systemctl --user status ${APP_NAME}"
echo "  journalctl --user -u ${APP_NAME} -n 100 --no-pager"
