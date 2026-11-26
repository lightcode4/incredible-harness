#!/usr/bin/env bash
# Stage everything the Tauri build needs:
#   1. compile the Bun engine into a single-file binary named for the Rust target triple
#      (Tauri sidecars require the `-<triple>` suffix), and
#   2. copy the existing UI (myagent/static) in as the bundled `ui` resource.
set -euo pipefail
cd "$(dirname "$0")/.."

TRIPLE="$(rustc -Vv | sed -n 's/host: //p')"
EXT=""; [[ "$TRIPLE" == *windows* ]] && EXT=".exe"

echo "Target triple: $TRIPLE"
mkdir -p src-tauri/binaries src-tauri/ui

echo "Compiling engine -> src-tauri/binaries/lc4-engine-$TRIPLE$EXT"
bun build src/index.ts --compile --outfile "src-tauri/binaries/lc4-engine-$TRIPLE$EXT"

echo "Staging UI -> src-tauri/ui"
rm -rf src-tauri/ui
cp -R ../myagent/static src-tauri/ui

echo "Prepared."
