#!/usr/bin/env bash
# Fetch the openscad-wasm runtime the in-browser dimension preview uses.
#
# Kept out of git: openscad.wasm is 7.7MB and would sit in history forever.
# Without it the kiosk still works — the parameter editor falls back to the
# server-side OpenSCAD CLI, which is the same code path used when the browser
# is too old for WebAssembly. This only makes the preview local and cancellable.
#
# Release 2022.03.20 is the newest openscad-wasm has ever published. It reports
# itself as OpenSCAD 2022.03.13.
set -euo pipefail

REL="2022.03.20"
BASE="https://github.com/openscad/openscad-wasm/releases/download/${REL}"
DEST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/vendor"

mkdir -p "$DEST"
for f in openscad.wasm openscad.wasm.js; do
  echo "→ $f"
  curl -fL --progress-bar -o "$DEST/$f" "$BASE/$f"
done

echo
echo "Installed into $DEST:"
ls -lh "$DEST/openscad.wasm" "$DEST/openscad.wasm.js" | awk '{print "  " $5 "\t" $9}'
echo
echo "Optional: openscad.fonts.js (8.2MB) adds text() support to the in-browser"
echo "preview. Without it a model using text() renders on the server instead."
