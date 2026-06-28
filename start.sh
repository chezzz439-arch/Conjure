#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Disable screen blanking and power management for kiosk
xset s off          2>/dev/null || true
xset s noblank      2>/dev/null || true
xset -dpms          2>/dev/null || true

# Kill any stale instances
pkill -f "uvicorn main:app" 2>/dev/null || true
pkill -f "chromium-browser.*localhost:8000" 2>/dev/null || true
sleep 1

# Start FastAPI backend
"$SCRIPT_DIR/.venv/bin/uvicorn" main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --log-level info \
  >> "$SCRIPT_DIR/logs/server.log" 2>&1 &

UVICORN_PID=$!
echo "uvicorn started — PID $UVICORN_PID"

# Wait for server to accept connections
for i in $(seq 1 10); do
  if curl -sf http://localhost:8000/ > /dev/null 2>&1; then
    echo "Server ready after ${i}s"
    break
  fi
  sleep 1
done

# Chromium kiosk mode flags
chromium-browser \
  --kiosk \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --noerrdialogs \
  --disable-translate \
  --disable-features=TranslateUI \
  --disable-pinch \
  --overscroll-history-navigation=0 \
  --disable-back-forward-cache \
  --autoplay-policy=no-user-gesture-required \
  --disable-popup-blocking \
  --check-for-update-interval=604800 \
  --app=http://localhost:8000 \
  http://localhost:8000

# When Chromium exits (e.g. user kills it), stop the server
echo "Chromium exited — stopping uvicorn"
kill "$UVICORN_PID" 2>/dev/null || true
