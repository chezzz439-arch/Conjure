#!/bin/bash
set -e

echo "Installing OrcaSlicer for ARM64..."

cd ~

# Try downloading AppImage first
ORCA_VERSION="2.1.1"
ORCA_URL="https://github.com/SoftFever/OrcaSlicer/releases/download/v${ORCA_VERSION}/OrcaSlicer_Linux_ARM64_V${ORCA_VERSION}.AppImage"

mkdir -p ~/OrcaSlicer
cd ~/OrcaSlicer

echo "Downloading OrcaSlicer AppImage..."
wget -q --show-progress "$ORCA_URL" -O OrcaSlicer.AppImage || {
  echo "AppImage download failed — will use CuraEngine fallback"
  exit 1
}

chmod +x OrcaSlicer.AppImage

# Extract AppImage
./OrcaSlicer.AppImage --appimage-extract 2>/dev/null || true

if [ -f "./squashfs-root/usr/bin/orca-slicer" ]; then
  echo "OrcaSlicer extracted successfully"
  ln -sf ~/OrcaSlicer/squashfs-root/usr/bin/orca-slicer ~/OrcaSlicer/orca-slicer
  echo "OrcaSlicer available at: ~/OrcaSlicer/orca-slicer"
else
  echo "Extraction failed — checking AppImage directly"
  ln -sf ~/OrcaSlicer/OrcaSlicer.AppImage ~/OrcaSlicer/orca-slicer
fi

echo "Done."
