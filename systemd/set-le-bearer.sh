#!/usr/bin/env bash
# verdigris-set-le-bearer ADAPTER_MAC DEVICE_MAC
# verdigris-set-le-bearer --address-resolution HCI ADDRESS_TYPE DEVICE_MAC
#
# Sets PreferredBearer=le and LastUsedBearer=le in the BlueZ pairing record at
#   /var/lib/bluetooth/<adapter>/<device>/info
# to bias BlueZ toward BLE on the next Connect() of that device. This is
# the key unlock for ANCS access on iOS — without it, BlueZ defaults to
# BR/EDR (which carries MAP/PBAP but not ANCS) and BLE GATT enumeration
# never runs. See https://github.com/bmh129/ancs4linux for the rationale.
#
# Designed for narrow sudoers exposure:
#   user ALL=(root) NOPASSWD: /usr/local/bin/verdigris-set-le-bearer
#
# Every argument is validated before use. The optional address-resolution mode
# can only set the one kernel device flag required by BlueZ 5.87; the normal
# mode remains limited to one pairing-record file and two fixed values.

set -euo pipefail

usage() {
    echo "usage: $0 <adapter-mac> <device-mac>" >&2
    echo "       $0 --address-resolution <hciN> <public|random> <device-mac>" >&2
    exit 1
}

mac_re='^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$'

if [[ "${1:-}" == --address-resolution ]]; then
    [[ $# -eq 4 ]] || usage
    HCI="$2"
    ADDRESS_TYPE="$3"
    DEVICE="$4"

    [[ "$HCI" =~ ^hci([0-9]+)$ ]] \
        || { echo "bad adapter name: $HCI" >&2; exit 1; }
    HCI_INDEX="${BASH_REMATCH[1]}"
    case "$ADDRESS_TYPE" in
        public) MGMT_ADDRESS_TYPE=1 ;;
        random) MGMT_ADDRESS_TYPE=2 ;;
        *) echo "bad LE address type: $ADDRESS_TYPE" >&2; exit 1 ;;
    esac
    [[ "$DEVICE" =~ $mac_re ]] \
        || { echo "bad device MAC: $DEVICE" >&2; exit 1; }

    exec btmgmt --index "$HCI_INDEX" set-flags \
        -t "$MGMT_ADDRESS_TYPE" -f 4 "$DEVICE"
fi

[[ $# -eq 2 ]] || usage

ADAPTER="$1"
DEVICE="$2"

[[ "$ADAPTER" =~ $mac_re ]] || { echo "bad adapter MAC: $ADAPTER" >&2; exit 1; }
[[ "$DEVICE"  =~ $mac_re ]] || { echo "bad device MAC: $DEVICE"  >&2; exit 1; }

# Upper-case for the path (BlueZ uses uppercase MACs in directory names)
ADAPTER_UC=$(echo "$ADAPTER" | tr 'a-f' 'A-F')
DEVICE_UC=$(echo "$DEVICE"  | tr 'a-f' 'A-F')

FILE="/var/lib/bluetooth/${ADAPTER_UC}/${DEVICE_UC}/info"
[[ -f "$FILE" ]] || { echo "no pairing info at $FILE" >&2; exit 1; }

TMP=$(mktemp -p "$(dirname "$FILE")" .info.XXXXXX) || exit 1
trap 'rm -f "$TMP"' EXIT

# These keys belong to [General]. Appending them at EOF is incorrect whenever
# the bonding record has later key sections, and BlueZ silently ignores them.
# Write both the explicit preference and last-used fallback for compatibility
# across BlueZ versions.
awk '
    function missing() {
        if (!seen_preferred) print "PreferredBearer=le"
        if (!seen_last) print "LastUsedBearer=le"
    }
    /^\[/ {
        if (in_general) missing()
        in_general = ($0 == "[General]")
        found_general = found_general || in_general
        print
        next
    }
    in_general && /^PreferredBearer=/ {
        print "PreferredBearer=le"
        seen_preferred = 1
        next
    }
    in_general && /^LastUsedBearer=/ {
        print "LastUsedBearer=le"
        seen_last = 1
        next
    }
    { print }
    END {
        if (in_general) missing()
        if (!found_general) exit 2
    }
' "$FILE" > "$TMP" || {
    echo "pairing record has no [General] section: $FILE" >&2
    exit 1
}

# Preserve original ownership + mode (typically root:root, 0600)
chmod --reference="$FILE" "$TMP"
chown --reference="$FILE" "$TMP"

mv "$TMP" "$FILE"
trap - EXIT

echo "[ok] PreferredBearer=le set in $FILE"
