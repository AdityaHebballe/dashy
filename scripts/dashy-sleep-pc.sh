#!/usr/bin/env bash
set -euo pipefail

PHONE_ADB_TARGET="${PHONE_ADB_TARGET:-192.168.0.8:5555}"
ADB_BIN="${ADB_BIN:-/usr/bin/adb}"
TIMEOUT_BIN="${TIMEOUT_BIN:-/usr/bin/timeout}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-/usr/bin/systemctl}"

if [[ "${1:-}" == "--wake-only" ]]; then
    for _ in $(seq 1 8); do
        "${TIMEOUT_BIN}" 3s "${ADB_BIN}" connect "${PHONE_ADB_TARGET}" >/dev/null 2>&1 || true
        if "${TIMEOUT_BIN}" 5s "${ADB_BIN}" -s "${PHONE_ADB_TARGET}" shell input keyevent 224 >/dev/null 2>&1; then
            exit 0
        fi
        sleep 2
    done
    exit 1
fi

"${TIMEOUT_BIN}" 3s "${ADB_BIN}" connect "${PHONE_ADB_TARGET}"
"${TIMEOUT_BIN}" 5s "${ADB_BIN}" -s "${PHONE_ADB_TARGET}" shell input keyevent 223
sleep 0.5
"${SYSTEMCTL_BIN}" suspend -i
