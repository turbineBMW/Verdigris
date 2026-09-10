#!/usr/bin/env bash
# 01a_cod_repair.sh — change adapter Class-of-Device to A/V Hands-Free,
# restart bluetoothd, remove the existing iPhone pairing on Linux side,
# put adapter in discoverable mode so the iPhone can find it.
#
# Run with:  sudo bash spike/01a_cod_repair.sh
#
# Why: iOS hides "Share System Notifications" / "Show Notifications"
# toggles for paired devices whose Major Class is Computer. iOS also
# refuses MAP OBEX connections (0x43 Forbidden) without those toggles
# enabled. Changing the adapter CoD to 0x240408 (Audio/Video > Hands-Free
# Device) should make the toggles appear after a fresh pair.
#
# Reversible: backs up /etc/bluetooth/main.conf to a timestamped .bak
# file before editing. Roll back with:
#     sudo cp /etc/bluetooth/main.conf.bak.<TS> /etc/bluetooth/main.conf
#     sudo systemctl restart bluetooth.service

set -euo pipefail

CONF=/etc/bluetooth/main.conf
TS=$(date +%s)
BAK=/etc/bluetooth/main.conf.bak.$TS
NEW_CLASS=0x240408
IPHONE_MAC="AA:BB:CC:DD:EE:FF"
REAL_USER="${SUDO_USER:-$USER}"
RESULTS=/home/${REAL_USER}/code/verdigris/spike/results
LOG=$RESULTS/01a_cod_repair.log

mkdir -p "$RESULTS"
exec > >(tee -a "$LOG") 2>&1

log()  { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { printf '\n[FAIL] %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "Run as root: sudo bash $0"

log "Backing up $CONF -> $BAK"
cp -a "$CONF" "$BAK"

log "Setting Class = $NEW_CLASS in $CONF [General] section"
python3 - <<PYEOF
import re, pathlib
p = pathlib.Path("$CONF")
text = p.read_text()
m = re.search(r'^\[General\]\s*\n', text, re.M)
if not m:
    raise SystemExit("[General] section not found in main.conf")
start = m.end()
end_m = re.search(r'^\[', text[start:], re.M)
end = start + (end_m.start() if end_m else len(text) - start)
section = text[start:end]
# strip any existing Class line (commented or not)
section = re.sub(r'^\s*#?\s*Class\s*=.*\n', '', section, flags=re.M)
section = "Class = $NEW_CLASS\n" + section.lstrip('\n')
p.write_text(text[:start] + section + text[end:])
print("[ok] main.conf updated")
PYEOF

log "Effective [General] section now:"
sed -n '/^\[General\]/,/^\[/p' "$CONF" | head -20

log "Restarting bluetooth.service ..."
systemctl restart bluetooth.service
sleep 2

log "New adapter Class (should show 0x240408 ... A/V Hands-Free):"
bluetoothctl show | grep -E "Class|Powered" | head -5

log "Removing existing iPhone pairing on Linux side: $IPHONE_MAC"
bluetoothctl remove "$IPHONE_MAC" 2>&1 || log "(already removed)"

log "Enabling adapter pairable + discoverable (auto-timeout ~3 min)"
bluetoothctl <<'EOC' >/dev/null
power on
pairable on
discoverable on
EOC

bluetoothctl show | grep -E "Powered|Discoverable|Pairable" | head -5

cat <<EOF

================================================================
DONE on the Linux side. Now, on your iPhone:

  1. Settings -> Bluetooth.
     If 'pop-os' is still in 'My Devices': tap the (i), 'Forget This Device'.

  2. Wait a few seconds. 'pop-os' should reappear under OTHER DEVICES.
     Tap 'pop-os'.

  3. iPhone will show a 6-digit pairing code.
     Pop!_OS will show the SAME code in a GNOME notification (top-right).
     Confirm on both sides.

  4. After pairing, on the iPhone, tap the (i) next to 'pop-os'.
     You should now see 'Show Notifications' AND 'Share System Notifications'.
     Toggle BOTH ON.

  5. Tell Claude 'paired' and we'll re-run the MAP test.

If the toggles still aren't there after re-pairing, that tells us iOS 26.5
gates them on something other than CoD, and we go to plan B.

Backup of main.conf: $BAK
Result log:          $LOG
================================================================
EOF
