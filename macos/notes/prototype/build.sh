#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
app="$HOME/Applications/Verdigris Checklist Probe.app"
mkdir -p "$app/Contents/MacOS"
xcrun swiftc ChecklistProbe.swift -o "$app/Contents/MacOS/ChecklistProbe" -framework AppKit -framework ApplicationServices
cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>dev.turbinebmw.Verdigris.ChecklistProbe</string>
<key>CFBundleName</key><string>Verdigris Checklist Probe</string>
<key>CFBundleExecutable</key><string>ChecklistProbe</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>1</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
codesign --force --sign - "$app"
open "$app"
