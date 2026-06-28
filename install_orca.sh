#!/bin/bash
# Install OrcaSlicer for the Conjure kiosk.
#
# NOTE (verified against GitHub Releases): OrcaSlicer publishes NO ARM64 Linux
# AppImage before v2.4.0, and the v2.4.x aarch64 AppImages are built for
# Ubuntu 24.04 (glibc 2.39) — they will NOT launch on Ubuntu 22.04 (glibc 2.35).
# So on an Orange Pi 5 Pro running Ubuntu 22.04 there may be no working
# OrcaSlicer binary at all. This script tries to fetch the best arch-matched
# build, verifies it actually launches, and otherwise tells you to use the
# CuraEngine fallback (which the kiosk supports automatically).
set -uo pipefail

ARCH="$(uname -m)"
echo "Detected architecture: $ARCH"

OWNER_REPO="OrcaSlicer/OrcaSlicer"
mkdir -p ~/OrcaSlicer
cd ~/OrcaSlicer || exit 1

cura_hint() {
  echo ""
  echo ">> The kiosk will fall back to CuraEngine automatically."
  echo ">> Install it on the Orange Pi with:"
  echo "       sudo apt update && sudo apt install -y cura-engine"
  echo ">> Then leave ORCASLICER_PATH pointing at a non-existent path (or unset"
  echo "   it) so run_slicing() uses the CuraEngine path."
}

echo "Querying GitHub for an OrcaSlicer Linux AppImage matching $ARCH..."
ASSET_URL="$(curl -fsSL "https://api.github.com/repos/${OWNER_REPO}/releases?per_page=20" 2>/dev/null \
  | python3 -c '
import sys, json
arch = sys.argv[1].lower()
want = ("aarch64", "arm64") if arch in ("aarch64", "arm64") else ("x86_64", "amd64")
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for rel in data:
    for a in rel.get("assets", []):
        n = a.get("name", "").lower()
        if n.endswith(".appimage") and "linux" in n and any(w in n for w in want):
            print(a["browser_download_url"]); sys.exit(0)
sys.exit(1)
' "$ARCH")"

if [ -z "${ASSET_URL:-}" ]; then
  echo "No OrcaSlicer Linux AppImage found for $ARCH in recent releases."
  if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
    echo "(OrcaSlicer ships aarch64 AppImages only from v2.4.0+, and those target"
    echo " Ubuntu 24.04 / glibc 2.39 — incompatible with Ubuntu 22.04.)"
  fi
  cura_hint
  exit 2
fi

echo "Found asset: $ASSET_URL"
echo "Downloading..."
if ! wget -q --show-progress "$ASSET_URL" -O OrcaSlicer.AppImage; then
  echo "Download failed."
  cura_hint
  exit 2
fi
chmod +x OrcaSlicer.AppImage

# Extract so we don't need a runtime FUSE mount on the kiosk.
./OrcaSlicer.AppImage --appimage-extract >/dev/null 2>&1 || true

if [ -x "./squashfs-root/usr/bin/orca-slicer" ]; then
  BIN="$HOME/OrcaSlicer/squashfs-root/usr/bin/orca-slicer"
elif [ -x "./squashfs-root/AppRun" ]; then
  BIN="$HOME/OrcaSlicer/squashfs-root/AppRun"
else
  BIN="$HOME/OrcaSlicer/OrcaSlicer.AppImage"
fi
ln -sf "$BIN" ~/OrcaSlicer/orca-slicer
echo "Linked binary: ~/OrcaSlicer/orca-slicer -> $BIN"

# Verify it actually launches on THIS box (catches glibc / arch mismatches).
echo "Verifying OrcaSlicer can launch..."
if ~/OrcaSlicer/orca-slicer --help >/dev/null 2>&1; then
  echo "OK — OrcaSlicer launches."
  echo ">> Set in .env:  ORCASLICER_PATH=$HOME/OrcaSlicer/orca-slicer"
  echo "Done."
else
  echo "WARNING: OrcaSlicer downloaded but FAILED to launch on this system."
  echo "(Most often a glibc mismatch: aarch64 AppImages are built for Ubuntu 24.04.)"
  cura_hint
  exit 3
fi
