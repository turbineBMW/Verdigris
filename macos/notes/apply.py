#!/usr/bin/env python3
"""Add the Notes extension to the existing iCloudBridge source checkout on a Mac.

Usage: python3 apply.py ~/Projects/verdigris-icloudbridge
This patches source only; build and restart the app separately.
"""
from pathlib import Path
import argparse
import plistlib
import shutil

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("checkout", type=Path)
args = parser.parse_args()
source = args.checkout / "Sources/iCloudBridge"
routes = source / "API/Routes.swift"
content = routes.read_text()
marker = "    try api.register(collection: ListsController("
if "NotesController()" not in content:
    if marker not in content:
        raise SystemExit("Unrecognized bridge routes; no files changed")
    content = content.replace(marker, "    try api.register(collection: NotesController())\n\n" + marker, 1)
plist = source / "Resources/Info.plist"
with plist.open("rb") as handle:
    info = plistlib.load(handle)
info["NSAppleEventsUsageDescription"] = "Allow the bridge to read, create, and edit Apple Notes from Verdigris on Linux."
for filename in ("NotesController.swift", "NotesStore.swift", "NotesEditing.swift", "NotesSharing.swift"):
    shutil.copyfile(Path(__file__).with_name(filename), source / "API" / filename)
routes.write_text(content)
with plist.open("wb") as handle:
    plistlib.dump(info, handle)
print("Notes extension applied. Use macos/notes/build.sh to build with persistent signing.")
