#!/usr/bin/env bash
# Install the privileged helper that biases BlueZ toward BLE for the
# paired iPhone — required for ANCS / per-app notifications.
#
# What this installs:
#   /usr/local/bin/verdigris-set-le-bearer  (the helper script)
#   /etc/sudoers.d/verdigris-ancs           (NOPASSWD rule for it)
# It also enables BlueZ's experimental userspace D-Bus API, which exposes the
# PreferredBearer property used to select LE without racing a Classic reconnect.
#
# Run as root:  sudo bash systemd/install-ancs-sudoers.sh
#
# Uninstall:
#   sudo rm /usr/local/bin/verdigris-set-le-bearer
#   sudo rm /etc/sudoers.d/verdigris-ancs

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo bash $0" >&2
    exit 1
fi

USER_TO_GRANT="${SUDO_USER:-}"
if [[ -z "$USER_TO_GRANT" || "$USER_TO_GRANT" == root ]]; then
    echo "Could not determine non-root user to grant. Run via sudo." >&2
    exit 1
fi
if [[ ! "$USER_TO_GRANT" =~ ^[A-Za-z_][A-Za-z0-9_.-]*\$?$ ]] \
        || ! id "$USER_TO_GRANT" >/dev/null 2>&1; then
    echo "Refusing invalid or unknown invoking user: $USER_TO_GRANT" >&2
    exit 1
fi

# 1. Install the helper script
SRC="$(dirname "$0")/set-le-bearer.sh"
DST=/usr/local/bin/verdigris-set-le-bearer
install -m 755 -o root -g root "$SRC" "$DST"
echo "[+] Installed $DST"

# 2. Build + validate the sudoers entry, then install
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
cat > "$TMP" <<EOF
# Lets the verdigris daemon set BlueZ's per-device LastUsedBearer
# (the unlock for ANCS / per-app notifications on iOS). The helper
# strictly validates its two MAC arguments, so even with this rule
# the attack surface is bounded to one specific file edit.
$USER_TO_GRANT ALL=(root) NOPASSWD: $DST
EOF

if ! visudo -cf "$TMP" >/dev/null; then
    echo "FATAL: generated sudoers entry failed visudo check" >&2
    exit 1
fi

install -m 440 -o root -g root "$TMP" /etc/sudoers.d/verdigris-ancs
echo "[+] Installed /etc/sudoers.d/verdigris-ancs (for $USER_TO_GRANT)"

# PreferredBearer is an experimental *userspace D-Bus property* in BlueZ.
# This does not enable the separate kernel-experimental feature set.
BLUEZ_CONF=/etc/bluetooth/main.conf
if [[ -f "$BLUEZ_CONF" ]]; then
    if grep -Eq '^[[:space:]]*Experimental[[:space:]]*=[[:space:]]*true([[:space:]]*(#.*)?)?$' \
            "$BLUEZ_CONF"; then
        echo "[+] BlueZ Experimental userspace API already enabled"
    else
        BACKUP="${BLUEZ_CONF}.verdigris.bak"
        [[ -e "$BACKUP" ]] || cp -p "$BLUEZ_CONF" "$BACKUP"
        if grep -Eq '^[[:space:]]*#?[[:space:]]*Experimental[[:space:]]*=' \
                "$BLUEZ_CONF"; then
            sed -Ei \
                '0,/^[[:space:]]*#?[[:space:]]*Experimental[[:space:]]*=/s//Experimental =/' \
                "$BLUEZ_CONF"
            sed -Ei \
                '0,/^Experimental[[:space:]]*=[[:space:]]*.*/s//Experimental = true/' \
                "$BLUEZ_CONF"
        else
            sed -i '0,/^\[General\]$/s//[General]\nExperimental = true/' "$BLUEZ_CONF"
        fi
        echo "[+] Enabled BlueZ Experimental userspace API in $BLUEZ_CONF"
        echo "[+] Original saved as $BACKUP"
    fi
else
    echo "[!] $BLUEZ_CONF not found; enable bluetoothd --experimental manually" >&2
fi

cat <<EOF

[+] Done. Restart Bluetooth and the Verdigris daemon, then trigger ANCS:
      sudo systemctl restart bluetooth
      systemctl --user restart verdigris
      verdigris ancs-enable

If the daemon already had a Connected pair, the ancs-enable command will
set BlueZ's live LE preference and connect the LE bearer without disrupting
the Classic MAP/PBAP connection. The daemon reapplies the BlueZ 5.87 address-
resolution workaround automatically after future Bluetooth restarts.
EOF
