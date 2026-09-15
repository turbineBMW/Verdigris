#!/bin/bash
set -euo pipefail
source_dir="$(cd "$(dirname "$0")" && pwd)"
checkout="${1:?Usage: build.sh /path/to/icloudbridge}"
python3 "$source_dir/../push/apply.py" "$checkout"
python3 "$source_dir/apply.py" "$checkout"
bash "$checkout/scripts/build.sh"
python3 "$source_dir/../signing/sign.py" "$checkout/build/iCloudBridge.app"
