#!/usr/bin/env bash
# Install a sudoers.d entry that lets the verdigris user daemon set the
# adapter CoD without prompting for a password — but only for that one
# specific btmgmt invocation.
#
# Run as root: sudo bash systemd/install-cod-sudoers.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo bash $0" >&2; exit 1
fi

USER_TO_GRANT="${SUDO_USER:-}"
if [[ -z "$USER_TO_GRANT" || "$USER_TO_GRANT" == root ]]; then
    echo "Could not determine the non-root invoking user. Run this via sudo." >&2
    exit 1
fi
if [[ ! "$USER_TO_GRANT" =~ ^[A-Za-z_][A-Za-z0-9_.-]*\$?$ ]] \
        || ! id "$USER_TO_GRANT" >/dev/null 2>&1; then
    echo "Refusing invalid or unknown invoking user: $USER_TO_GRANT" >&2
    exit 1
fi

BTMGMT_PATH="$(command -v btmgmt || true)"
if [[ -z "$BTMGMT_PATH" || "$BTMGMT_PATH" != /* ]]; then
    echo "Could not find btmgmt on PATH." >&2
    exit 1
fi

SRC="$(dirname "$0")/sudoers-verdigris-cod"
DST=/etc/sudoers.d/verdigris-cod
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

# Render the machine-specific values before validating. The source file is a
# template and is intentionally not itself valid sudoers syntax.
sed \
    -e "s|@VERDIGRIS_USER@|$USER_TO_GRANT|g" \
    -e "s|@BTMGMT_PATH@|$BTMGMT_PATH|g" \
    "$SRC" > "$TMP"

# Validate before installing — bad sudoers files can lock you out.
if ! visudo -cf "$TMP" >/dev/null; then
    echo "FATAL: generated sudoers entry failed visudo -c. Not installing." >&2
    exit 1
fi

install -m 440 -o root -g root "$TMP" "$DST"
echo "[+] Installed $DST (for $USER_TO_GRANT)"

# Quick verification
if visudo -cf "$DST" >/dev/null; then
    echo "[+] visudo says $DST is valid"
else
    echo "[!] $DST failed validation post-install — removing"
    rm -f "$DST"
    exit 1
fi

cat <<EOF

[+] The verdigris user daemon can now run 'btmgmt class 4 8' without
    a password. On each daemon start (e.g. boot, login), it will set
    the adapter to A/V Hands-Free CoD automatically.

    Verify by restarting the daemon:
      systemctl --user restart verdigris

    Then check journalctl --user -u verdigris for a 'CoD set ok' line.

    Uninstall:
      sudo rm $DST
EOF
