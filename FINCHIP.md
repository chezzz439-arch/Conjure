# Conjure Kiosk — FinChip Deployment Guide

## What This Is

Conjure Kiosk is a voice-to-3D-print kiosk. A user speaks a description, an AI generates a 3D model, the model is sliced, and the G-code is written to a USB drive for direct insertion into a Neptune 4 Pro printer.

**Stack:**
- Backend: FastAPI (Python 3.12) on port 8001
- Frontend: Single-page HTML/JS served by FastAPI at `/`
- 3D generation: Meshy AI text-to-3D API (preview mode)
- Slicing: OrcaSlicer (primary) → CuraEngine (fallback)
- TTS: ElevenLabs (primary) → `espeak` (fallback)
- Storage: InsForge S3-compatible object storage (non-blocking; skipped if file > 20 MB)
- Database: SQLite at `conjure.db` (model gallery)
- Target hardware: Orange Pi 5 Pro running Ubuntu

---

## Hardware Setup

| Component | Spec |
|-----------|------|
| SBC | Orange Pi 5 Pro (8 GB RAM) |
| OS | Ubuntu 22.04 (arm64) |
| Printer | Elegoo Neptune 4 Pro |
| USB | FAT32-formatted drive in any USB-A port |
| Display | 1080p touchscreen via HDMI |
| Browser | Chromium kiosk mode, fullscreen |

---

## First-Time Setup on Orange Pi

```bash
# 1. Clone repo
git clone https://github.com/chezzz439-arch/Conjure.git conjure-kiosk
cd conjure-kiosk

# 2. Python venv
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Copy and fill in secrets
cp .env.example .env
nano .env

# 4. Install OrcaSlicer (arm64 AppImage)
wget <orca-slicer-arm64-appimage-url> -O OrcaSlicer.AppImage
chmod +x OrcaSlicer.AppImage
# Set ORCASLICER_PATH in .env to point here

# 5. Install espeak (TTS fallback)
sudo apt install -y espeak

# 6. Create the slicing profile directory
mkdir -p profiles
# Copy neptune4pro_orca.json into profiles/
```

---

## Environment Variables (`.env`)

```
# Required
MESHY_API_KEY=msy_...

# TTS (optional — falls back to espeak if missing or expired)
ELEVENLABS_API_KEY=sk_...
ELEVENLABS_VOICE_ID=21m00Tcm4TlvDq8ikWAM

# Cloud storage (optional — upload skipped if missing or file > 20 MB)
INSFORGE_API_KEY=ik_...
INSFORGE_BASE_URL=https://...insforge.app
INSFORGE_STORAGE_BUCKET_CONJURE=conjure-models

# Slicing (Pi paths)
ORCASLICER_PATH=/home/orangepi/conjure-kiosk/OrcaSlicer.AppImage
ORCASLICER_PROFILE=/home/orangepi/conjure-kiosk/profiles/neptune4pro_orca.json
CURAENGINE_PATH=/usr/bin/CuraEngine
CURA_RESOURCES_PATH=/usr/share/cura/resources
PRINTER_PROFILE=neptune4pro

# Paths
USB_MOUNT_PATH=/media/usb
OUTPUT_DIR=/home/orangepi/conjure-kiosk/output
```

---

## Running the Server

```bash
cd ~/conjure-kiosk
.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8001
```

Health check:
```bash
curl http://localhost:8001/api/health
# {"status":"ok","db":true,"model_count":3,"disk_free_gb":42.1,"api_keys":{...},"pipeline":"idle"}
```

---

## Autostart (systemd)

```ini
# /etc/systemd/system/conjure.service
[Unit]
Description=Conjure Kiosk
After=network.target

[Service]
User=orangepi
WorkingDirectory=/home/orangepi/conjure-kiosk
ExecStart=/home/orangepi/conjure-kiosk/.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8001
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable conjure
sudo systemctl start conjure
```

---

## Chromium Kiosk Mode

```bash
# /etc/xdg/autostart/conjure-kiosk.desktop
[Desktop Entry]
Type=Application
Name=Conjure Kiosk
Exec=chromium-browser --kiosk --noerrdialogs --disable-infobars --app=http://localhost:8001
```

Or add to `~/.bashrc` for a quick manual start:
```bash
alias kiosk='chromium-browser --kiosk --app=http://localhost:8001'
```

---

## USB Drive Notes

- Must be FAT32 formatted and mounted under `/media/` (auto-mounted by `udisks2`)
- The app calls `diskutil info` / `lsblk` to verify it's a real USB drive — optical drives and phone MTP mounts are filtered out
- Output file is always `conjure_model.stl` in the root of the USB drive
- On the Pi: ensure the `orangepi` user is in the `disk` and `plugdev` groups: `sudo usermod -aG plugdev,disk orangepi`

---

## Updating

```bash
cd ~/conjure-kiosk
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart conjure
```

---

## API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Kiosk UI (index.html) |
| `/api/health` | GET | DB status, disk, API key presence, pipeline state |
| `/api/state` | GET | Current pipeline state dict |
| `/api/generate` | POST | Start generation `{"prompt": "..."}` |
| `/api/model/glb` | GET | Download current GLB |
| `/api/model/stl` | GET | Download current STL |
| `/api/slice` | POST | Start slicing pipeline |
| `/api/copy-stl` | POST | Copy STL to detected USB drive |
| `/api/usb/status` | GET | USB detection status |
| `/api/reset` | POST | Reset pipeline state and clear working files |
| `/api/speak` | POST | TTS `{"text": "..."}` |
| `/api/models` | GET | List gallery (all generated models) |
| `/api/models/{id}/select` | POST | Load gallery model as active |
| `/events` | GET | SSE stream for pipeline progress |

---

## Troubleshooting

**Voice not working in browser**
Chromium blocks microphone on non-HTTPS origins unless the page is `localhost`. Serving via `http://localhost:8001` is fine. Do not open `file://` URLs.

**Model generates as wrong shape**
The `build_meshy_prompt()` function in `main.py` strips filler words and adds structural hints. Check the server logs (`[Meshy]` prefix) to see the exact prompt sent to the API.

**Slicing fails**
Verify `ORCASLICER_PATH` in `.env` points to the correct binary and the profile JSON exists at `ORCASLICER_PROFILE`. CuraEngine is the fallback — install with `sudo apt install cura-engine`.

**STL too large for cloud upload**
Files over 20 MB skip InsForge upload silently. The STL is still available locally via `/api/model/stl` and the `↓ USB STL` button in the viewer.

**ElevenLabs 401**
The key has expired. Renew subscription at elevenlabs.io, update `ELEVENLABS_API_KEY` in `.env`, restart service. Until then, `espeak` is used automatically.
