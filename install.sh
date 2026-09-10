#!/usr/bin/env bash
# User-local release install of Verdigris's native apps. No root needed.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$script_dir/rust/verdigris-apps/install.py" --release --copy "$@"
