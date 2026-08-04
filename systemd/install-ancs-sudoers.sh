#!/usr/bin/env bash
# Install the privileged helper that biases BlueZ toward BLE for the
# paired iPhone — required for ANCS / per-app notifications.
#
# What this installs:
#   /usr/local/bin/iphonebridge-set-le-bearer  (the helper script)
#   /etc/sudoers.d/iphonebridge-ancs           (NOPASSWD rule for it)
#
# Run as root:  sudo bash systemd/install-ancs-sudoers.sh
#
# Uninstall:
#   sudo rm /usr/local/bin/iphonebridge-set-le-bearer
#   sudo rm /etc/sudoers.d/iphonebridge-ancs

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo bash $0" >&2
    exit 1
fi

USER_TO_GRANT="${SUDO_USER:-$USER}"
if [[ "$USER_TO_GRANT" == "root" || -z "$USER_TO_GRANT" ]]; then
    echo "Could not determine non-root user to grant. Run via sudo." >&2
    exit 1
fi

# 1. Install the helper script
SRC="$(dirname "$0")/set-le-bearer.sh"
DST=/usr/local/bin/iphonebridge-set-le-bearer
install -m 755 -o root -g root "$SRC" "$DST"
echo "[+] Installed $DST"

# 2. Build + validate the sudoers entry, then install
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
cat > "$TMP" <<EOF
# Lets the iphonebridge daemon set BlueZ's per-device LastUsedBearer
# (the unlock for ANCS / per-app notifications on iOS). The helper
# strictly validates its two MAC arguments, so even with this rule
# the attack surface is bounded to one specific file edit.
$USER_TO_GRANT ALL=(root) NOPASSWD: $DST
EOF

if ! visudo -cf "$TMP" >/dev/null; then
    echo "FATAL: generated sudoers entry failed visudo check" >&2
    exit 1
fi

install -m 440 -o root -g root "$TMP" /etc/sudoers.d/iphonebridge-ancs
echo "[+] Installed /etc/sudoers.d/iphonebridge-ancs (for $USER_TO_GRANT)"

cat <<EOF

[+] Done. Next step: trigger an ANCS attempt
      blue ancs-enable

If the daemon already had a Connected pair, the ancs-enable command will
edit the bonding record, disconnect, and reconnect. BlueZ should then
open the BLE link and the ANCS GATT characteristics will appear, which
the daemon's AncsClient picks up automatically.
EOF
