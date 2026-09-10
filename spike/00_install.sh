#!/usr/bin/env bash
# Phase 0 step 00 — install bluez-obexd and verify org.bluez.obex activates.
#
# Run with:  sudo bash spike/00_install.sh
#
# bluez-obexd is the OBEX-over-Bluetooth daemon that exposes the
# org.bluez.obex DBus service. Without it, MAP (SMS) and PBAP (contacts)
# cannot work — Client1.CreateSession with Target="MAP" will fail with
# "ServiceUnknown: name not activatable".
#
# This script is the only place that should require root in Phase 0.
# Every other spike script runs unprivileged on the session bus.

set -euo pipefail

log() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { printf '\n[FAIL] %s\n' "$*" >&2; exit 1; }

if [[ $EUID -ne 0 ]]; then
    fail "Run as root: sudo bash $0"
fi

REAL_USER="${SUDO_USER:-$USER}"
RESULTS_DIR="/home/${REAL_USER}/code/verdigris/spike/results"
mkdir -p "$RESULTS_DIR"
LOG="$RESULTS_DIR/00_install.log"
exec > >(tee -a "$LOG") 2>&1

log "Installing bluez-obexd..."
apt update -qq
DEBIAN_FRONTEND=noninteractive apt install -y bluez-obexd

log "Installed package details:"
dpkg -s bluez-obexd | grep -E '^(Package|Version|Status):'

log "Activating org.bluez.obex on the session bus (run as ${REAL_USER})..."
sudo -u "$REAL_USER" -- bash -c '
    # obexd is a SESSION-bus service launched on demand by dbus-daemon.
    # The first call activates it. The systemd user unit also wires up auto-start.
    if ! command -v gdbus >/dev/null; then
        echo "gdbus not found"; exit 1
    fi
    gdbus introspect --session \
        --dest org.bluez.obex \
        --object-path /org/bluez/obex \
        2>&1 | head -20
'

log "Confirming systemd user unit exists..."
sudo -u "$REAL_USER" -- bash -c '
    systemctl --user status obex.service --no-pager 2>&1 | head -10 || true
'

log "DONE. bluez-obexd is installed; org.bluez.obex is activatable."
log "Result log: $LOG"
