import os
import json
import sqlite3
import asyncio
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator

import requests
import aiofiles
from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

MESHY_API_KEY        = os.getenv("MESHY_API_KEY", "")
INSFORGE_API_KEY     = os.getenv("INSFORGE_API_KEY", "")
INSFORGE_BASE_URL    = os.getenv("INSFORGE_BASE_URL", "https://api.insforge.dev")
ORCASLICER_PATH      = os.getenv("ORCASLICER_PATH", "/usr/bin/orcaslicer")
PRINTER_PROFILE      = os.getenv("PRINTER_PROFILE", "neptune4pro")
USB_MOUNT_PATH       = os.getenv("USB_MOUNT_PATH", "/media/usb")
OUTPUT_DIR           = Path(os.getenv("OUTPUT_DIR", str(BASE_DIR / "output")))
DB_PATH              = BASE_DIR / "conjure.db"
ELEVENLABS_API_KEY   = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID  = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
(OUTPUT_DIR / "models").mkdir(exist_ok=True)
PROFILES_DIR = BASE_DIR / "profiles"
PROFILES_DIR.mkdir(exist_ok=True)

MESHY_BASE    = "https://api.meshy.ai"
MESHY_HEADERS = {
    "Authorization": f"Bearer {MESHY_API_KEY}",
    "Content-Type": "application/json",
}

# ---------------------------------------------------------------------------
# SQLite gallery
# ---------------------------------------------------------------------------
_db_lock = threading.Lock()


def init_db() -> None:
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS models (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt        TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                meshy_task_id TEXT,
                glb_path      TEXT,
                stl_path      TEXT
            )
        """)
        conn.commit()
        conn.close()


def db_insert_model(prompt: str, task_id: str) -> int:
    with _db_lock:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.execute(
            "INSERT INTO models (prompt, meshy_task_id, created_at) VALUES (?, ?, ?)",
            (prompt, task_id, now),
        )
        conn.commit()
        row_id = cur.lastrowid
        conn.close()
        return row_id


def db_update_model_paths(row_id: int, glb_path: str, stl_path: str) -> None:
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute(
            "UPDATE models SET glb_path=?, stl_path=? WHERE id=?",
            (glb_path, stl_path, row_id),
        )
        conn.commit()
        conn.close()


def db_list_models() -> list:
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, prompt, created_at, glb_path, stl_path FROM models ORDER BY id DESC"
        ).fetchall()
        conn.close()
        return [
            {
                "id": r["id"],
                "prompt": r["prompt"],
                "created_at": r["created_at"],
                "has_glb": bool(r["glb_path"] and Path(r["glb_path"]).exists()),
                "has_stl": bool(r["stl_path"] and Path(r["stl_path"]).exists()),
            }
            for r in rows
        ]


def db_get_model(row_id: int):
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, prompt, created_at, meshy_task_id, glb_path, stl_path FROM models WHERE id=?",
            (row_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None


# Initialize DB at startup
init_db()

# ---------------------------------------------------------------------------
# Pipeline state
# ---------------------------------------------------------------------------
pipeline_state: dict = {
    "status": "idle",        # idle | generating | model_ready | slicing | usb_ready | error
    "task_id": None,
    "model_id": None,
    "prompt": None,
    "meshy_progress": 0,
    "stl_path": None,
    "glb_path": None,
    "gcode_path": None,
    "error": None,
}

# ---------------------------------------------------------------------------
# SSE — per-subscriber queues with replay buffer for late joiners
# ---------------------------------------------------------------------------
_subscribers: list = []
_event_buffer: list = []
_MAX_BUFFER = 40


async def push_event(step: str, status: str, message: str, progress: int = 0) -> None:
    event = {"step": step, "status": status, "message": message, "progress": progress}
    _event_buffer.append(event)
    if len(_event_buffer) > _MAX_BUFFER:
        _event_buffer.pop(0)
    for q in list(_subscribers):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


def _clear_event_buffer() -> None:
    _event_buffer.clear()


# ---------------------------------------------------------------------------
# ElevenLabs TTS — non-blocking fire-and-forget
# ---------------------------------------------------------------------------
def speak(text: str) -> None:
    if not ELEVENLABS_API_KEY:
        print(f"[TTS] No ElevenLabs key — skipping: {text}")
        return
    try:
        r = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "text": text,
                "model_id": "eleven_monolingual_v1",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
            },
            timeout=15,
        )
        if r.status_code == 200:
            audio_path = "/tmp/conjure_speech.mp3"
            with open(audio_path, "wb") as f:
                f.write(r.content)
            os.system(f"mpg123 -q {audio_path} &")
        else:
            print(f"[TTS] ElevenLabs error {r.status_code}: {r.text[:100]}")
    except Exception as e:
        print(f"[TTS] speak() failed: {e}")


# ---------------------------------------------------------------------------
# USB detection — tries multiple mount points
# ---------------------------------------------------------------------------
def _find_usb():
    candidates = [USB_MOUNT_PATH, "/mnt/usb", "/media/usb", "/media/orangepi/usb"]
    for path in candidates:
        p = Path(path)
        if p.exists() and p.is_mount():
            return str(p)
    for parent in [Path("/media/orangepi"), Path("/media/pi"), Path("/media")]:
        if parent.exists() and parent.is_dir():
            for child in parent.iterdir():
                if child.is_mount():
                    return str(child)
    return None


# ---------------------------------------------------------------------------
# Generation background task
# ---------------------------------------------------------------------------
async def run_generation(prompt: str) -> None:
    _clear_event_buffer()
    try:
        # ── Step 1: Create Meshy task ──────────────────────────────────────
        await push_event("create", "active", "Sending prompt to Meshy AI...", 2)
        threading.Thread(target=speak, args=("Got it. Conjuring your model now.",), daemon=True).start()
        r = await asyncio.to_thread(
            requests.post,
            f"{MESHY_BASE}/v2/text-to-3d",
            headers=MESHY_HEADERS,
            json={
                "mode": "preview",
                "prompt": prompt,
                "art_style": "realistic",
                "negative_prompt": "low quality, low resolution, ugly, deformed",
            },
            timeout=20,
        )
        if r.status_code not in (200, 201, 202):
            raise Exception(f"Meshy create failed: HTTP {r.status_code} — {r.text[:200]}")

        task_id = r.json()["result"]
        pipeline_state["task_id"] = task_id

        # Insert DB row and create per-model directory
        row_id = db_insert_model(prompt, task_id)
        pipeline_state["model_id"] = row_id
        model_dir = OUTPUT_DIR / "models" / str(row_id)
        model_dir.mkdir(parents=True, exist_ok=True)

        await push_event("create", "complete", f"Task created — {task_id[:10]}...", 5)

        # ── Step 2: Poll until SUCCEEDED ──────────────────────────────────
        await push_event("poll", "active", "Waiting for model generation...", 5)
        glb_url = None
        stl_url = None

        while True:
            await asyncio.sleep(3)
            poll_r = await asyncio.to_thread(
                requests.get,
                f"{MESHY_BASE}/v2/text-to-3d/{task_id}",
                headers={"Authorization": f"Bearer {MESHY_API_KEY}"},
                timeout=15,
            )
            if poll_r.status_code != 200:
                raise Exception(f"Meshy poll HTTP {poll_r.status_code}")

            data     = poll_r.json()
            status   = data.get("status", "")
            progress = int(data.get("progress", 0))
            pipeline_state["meshy_progress"] = progress

            if status == "SUCCEEDED":
                urls    = data.get("model_urls", {})
                glb_url = urls.get("glb")
                stl_url = urls.get("stl")
                await push_event("poll", "complete", "Model ready — downloading...", 50)
                break
            elif status in ("FAILED", "EXPIRED"):
                detail = data.get("task_error", {})
                raise Exception(f"Meshy {status}: {detail}")
            else:
                scaled = max(5, progress // 2)
                await push_event("poll", "active", f"Generating model... {progress}%", scaled)

        # ── Step 3: Download GLB ──────────────────────────────────────────
        await push_event("download_glb", "active", "Downloading GLB model...", 55)
        if not glb_url:
            raise Exception("No GLB URL in Meshy response")

        glb_resp = await asyncio.to_thread(requests.get, glb_url, timeout=120)
        glb_resp.raise_for_status()

        # Save to per-model dir + active slot
        glb_model_path  = model_dir / "model.glb"
        glb_active_path = OUTPUT_DIR / "model.glb"
        async with aiofiles.open(glb_model_path, "wb") as f:
            await f.write(glb_resp.content)
        async with aiofiles.open(glb_active_path, "wb") as f:
            await f.write(glb_resp.content)

        pipeline_state["glb_path"] = str(glb_active_path)
        glb_kb = len(glb_resp.content) // 1024
        await push_event("download_glb", "complete", f"GLB saved — {glb_kb} KB", 65)

        # ── Step 4: STL (direct URL or convert from GLB) ─────────────────
        await push_event("download_stl", "active", "Preparing STL for slicing...", 65)
        stl_model_path  = model_dir / "model.stl"
        stl_active_path = OUTPUT_DIR / "model.stl"

        if stl_url:
            stl_resp = await asyncio.to_thread(requests.get, stl_url, timeout=120)
            stl_resp.raise_for_status()
            async with aiofiles.open(stl_model_path, "wb") as f:
                await f.write(stl_resp.content)
            async with aiofiles.open(stl_active_path, "wb") as f:
                await f.write(stl_resp.content)
            stl_kb = len(stl_resp.content) // 1024
            await push_event("download_stl", "complete", f"STL downloaded — {stl_kb} KB", 75)
        else:
            await push_event("download_stl", "active", "Converting GLB → STL via trimesh...", 68)

            def _convert_glb_to_stl() -> None:
                import trimesh
                mesh = trimesh.load(str(glb_model_path), force="mesh")
                mesh.export(str(stl_model_path))

            await asyncio.to_thread(_convert_glb_to_stl)
            shutil.copy2(str(stl_model_path), str(stl_active_path))
            stl_kb = stl_model_path.stat().st_size // 1024
            await push_event("download_stl", "complete", f"STL converted — {stl_kb} KB", 75)

        pipeline_state["stl_path"] = str(stl_active_path)

        # Persist final paths to DB
        db_update_model_paths(row_id, str(glb_model_path), str(stl_model_path))

        # ── Step 5: Upload STL to InsForge ────────────────────────────────
        await push_event("insforge", "active", "Uploading STL to InsForge...", 78)
        try:
            def _upload_insforge() -> requests.Response:
                with open(stl_active_path, "rb") as fh:
                    return requests.post(
                        f"{INSFORGE_BASE_URL}/v1/files/upload",
                        headers={"Authorization": f"Bearer {INSFORGE_API_KEY}"},
                        files={"file": ("model.stl", fh, "application/octet-stream")},
                        timeout=60,
                    )

            ins_r = await asyncio.to_thread(_upload_insforge)
            await push_event(
                "insforge", "complete",
                f"InsForge upload HTTP {ins_r.status_code}", 88,
            )
        except Exception as e:
            await push_event("insforge", "error", f"InsForge upload failed (non-fatal): {e}", 88)

        # ── Done ──────────────────────────────────────────────────────────
        pipeline_state["status"] = "model_ready"
        await push_event("complete", "complete", "Model ready — tap PRINT THIS to slice", 100)
        threading.Thread(target=speak, args=("Your model is ready. Tap Print This to slice it.",), daemon=True).start()

    except Exception as exc:
        pipeline_state["status"] = "error"
        pipeline_state["error"] = str(exc)
        await push_event("error", "error", str(exc), 0)
        threading.Thread(target=speak, args=("Something went wrong. Please try again.",), daemon=True).start()


# ---------------------------------------------------------------------------
# Slicing background task
# ---------------------------------------------------------------------------
async def run_slicing() -> None:
    _clear_event_buffer()
    try:
        stl_path     = OUTPUT_DIR / "model.stl"
        gcode_path   = OUTPUT_DIR / "model.gcode"
        profile_path = PROFILES_DIR / f"{PRINTER_PROFILE}.json"

        # ── Step 1: Validate inputs ────────────────────────────────────────
        await push_event("load_stl", "active", "Loading STL into OrcaSlicer CLI...", 0)

        if not stl_path.exists():
            raise Exception("model.stl not found — generate a model first")
        if not Path(ORCASLICER_PATH).exists():
            raise Exception(
                f"OrcaSlicer not found at {ORCASLICER_PATH} — "
                "install OrcaSlicer and set ORCASLICER_PATH in .env"
            )

        stl_kb = stl_path.stat().st_size // 1024
        await push_event("load_stl", "complete", f"STL loaded — {stl_kb} KB", 10)

        # ── Step 2: Slice with OrcaSlicer ─────────────────────────────────
        await push_event("slice", "active", f"Slicing with {PRINTER_PROFILE} profile...", 12)

        cmd = [ORCASLICER_PATH]
        if profile_path.exists():
            cmd += ["--load", str(profile_path)]
        else:
            cmd += ["--preset-name", PRINTER_PROFILE]
        cmd += ["--export-gcode", str(stl_path), "--output", str(gcode_path)]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            proc.kill()
            raise Exception("OrcaSlicer timed out after 5 minutes")

        if proc.returncode != 0:
            err_msg = stderr.decode("utf-8", errors="replace")[:500]
            raise Exception(f"OrcaSlicer exit {proc.returncode}: {err_msg}")

        if not gcode_path.exists() or gcode_path.stat().st_size < 1000:
            raise Exception("Gcode output empty — slicing may have failed silently")

        gcode_mb = gcode_path.stat().st_size / (1024 * 1024)
        pipeline_state["gcode_path"] = str(gcode_path)
        await push_event("slice", "complete", f"Gcode generated — {gcode_mb:.1f} MB", 50)

        # ── Step 3: Detect USB ─────────────────────────────────────────────
        await push_event("usb_check", "active", "Detecting USB drive...", 55)
        usb_path = _find_usb()
        if usb_path is None:
            raise Exception(
                f"USB drive not mounted. "
                f"Insert USB and ensure it mounts at {USB_MOUNT_PATH} or /mnt/usb"
            )
        await push_event("usb_check", "complete", f"USB found at {usb_path}", 65)

        # ── Step 4: Copy gcode to USB ─────────────────────────────────────
        await push_event("copy_usb", "active", "Copying gcode to USB drive...", 68)
        dest = Path(usb_path) / "conjure_print.gcode"
        await asyncio.to_thread(shutil.copy2, str(gcode_path), str(dest))

        sync_proc = await asyncio.create_subprocess_exec(
            "sync",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await sync_proc.wait()
        await asyncio.sleep(1)

        await push_event("copy_usb", "complete", "conjure_print.gcode written to USB", 92)

        # ── Done ──────────────────────────────────────────────────────────
        pipeline_state["status"] = "usb_ready"
        await push_event("usb_ready", "complete", "USB ready — safe to remove", 100)
        threading.Thread(target=speak, args=("Done. Remove the USB drive and insert it into your printer.",), daemon=True).start()

    except Exception as exc:
        pipeline_state["status"] = "error"
        pipeline_state["error"] = str(exc)
        await push_event("error", "error", str(exc), 0)
        threading.Thread(target=speak, args=("Something went wrong. Please try again.",), daemon=True).start()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Conjure Kiosk", version="2.0.0")


@app.on_event("startup")
async def startup_event() -> None:
    init_db()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "models").mkdir(parents=True, exist_ok=True)
    result = os.system("which mpg123 > /dev/null 2>&1")
    if result != 0:
        print("[TTS] mpg123 not found — attempting install")
        os.system("sudo apt install -y mpg123")


@app.get("/", response_class=HTMLResponse)
def get_index() -> HTMLResponse:
    return HTMLResponse(content=(BASE_DIR / "index.html").read_text())


class GenerateRequest(BaseModel):
    prompt: str


@app.post("/api/generate")
async def api_generate(req: GenerateRequest, background_tasks: BackgroundTasks) -> JSONResponse:
    if not req.prompt.strip():
        raise HTTPException(400, "Prompt cannot be empty")
    pipeline_state.update({
        "status":         "generating",
        "prompt":         req.prompt.strip(),
        "task_id":        None,
        "model_id":       None,
        "meshy_progress": 0,
        "error":          None,
        "stl_path":       None,
        "glb_path":       None,
        "gcode_path":     None,
    })
    background_tasks.add_task(run_generation, req.prompt.strip())
    return JSONResponse({"status": "started", "prompt": req.prompt.strip()})


@app.get("/api/status/{task_id}")
def api_task_status(task_id: str) -> JSONResponse:
    if not MESHY_API_KEY:
        raise HTTPException(500, "MESHY_API_KEY not configured in .env")
    try:
        r = requests.get(
            f"{MESHY_BASE}/v2/text-to-3d/{task_id}",
            headers={"Authorization": f"Bearer {MESHY_API_KEY}"},
            timeout=15,
        )
        return JSONResponse(r.json())
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/model/glb")
def api_serve_glb() -> FileResponse:
    p = OUTPUT_DIR / "model.glb"
    if not p.exists():
        raise HTTPException(404, "GLB not found — run generation first")
    return FileResponse(str(p), media_type="model/gltf-binary", filename="model.glb")


@app.get("/api/model/stl")
def api_serve_stl() -> FileResponse:
    p = OUTPUT_DIR / "model.stl"
    if not p.exists():
        raise HTTPException(404, "STL not found — run generation first")
    return FileResponse(str(p), media_type="application/octet-stream", filename="model.stl")


@app.post("/api/slice")
async def api_slice(background_tasks: BackgroundTasks) -> JSONResponse:
    if not (OUTPUT_DIR / "model.stl").exists():
        raise HTTPException(400, "No STL file — generate a model first")
    pipeline_state["status"] = "slicing"
    pipeline_state["error"]  = None
    background_tasks.add_task(run_slicing)
    return JSONResponse({"status": "started"})


@app.get("/api/usb/status")
def api_usb_status() -> JSONResponse:
    path = _find_usb()
    return JSONResponse({"mounted": path is not None, "path": path or USB_MOUNT_PATH})


@app.get("/api/state")
def api_state() -> JSONResponse:
    return JSONResponse({k: v for k, v in pipeline_state.items()})


@app.post("/api/reset")
async def api_reset() -> JSONResponse:
    pipeline_state.update({
        "status":         "idle",
        "task_id":        None,
        "model_id":       None,
        "prompt":         None,
        "meshy_progress": 0,
        "stl_path":       None,
        "glb_path":       None,
        "gcode_path":     None,
        "error":          None,
    })
    # Clear active working copies only — gallery data in output/models/ is preserved
    for fname in ("model.glb", "model.stl", "model.gcode"):
        p = OUTPUT_DIR / fname
        if p.exists():
            p.unlink()
    _clear_event_buffer()
    await push_event("reset", "complete", "System reset", 0)
    return JSONResponse({"status": "reset"})


# ---------------------------------------------------------------------------
# TTS endpoint — lets frontend trigger speech from browser
# ---------------------------------------------------------------------------

class SpeakRequest(BaseModel):
    text: str


@app.post("/api/speak")
async def api_speak(req: SpeakRequest) -> JSONResponse:
    if not req.text.strip():
        return JSONResponse({"ok": False, "error": "no text"})
    threading.Thread(target=speak, args=(req.text.strip(),), daemon=True).start()
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Gallery endpoints
# ---------------------------------------------------------------------------

@app.get("/api/models")
def api_list_models() -> JSONResponse:
    return JSONResponse(db_list_models())


@app.get("/api/models/{model_id}/glb")
def api_gallery_glb(model_id: int) -> FileResponse:
    m = db_get_model(model_id)
    if not m or not m.get("glb_path"):
        raise HTTPException(404, "GLB not found for this model")
    p = Path(m["glb_path"])
    if not p.exists():
        raise HTTPException(404, "GLB file missing from disk")
    return FileResponse(str(p), media_type="model/gltf-binary", filename="model.glb")


@app.get("/api/models/{model_id}/stl")
def api_gallery_stl(model_id: int) -> FileResponse:
    m = db_get_model(model_id)
    if not m or not m.get("stl_path"):
        raise HTTPException(404, "STL not found for this model")
    p = Path(m["stl_path"])
    if not p.exists():
        raise HTTPException(404, "STL file missing from disk")
    return FileResponse(str(p), media_type="application/octet-stream", filename="model.stl")


@app.post("/api/models/{model_id}/select")
def api_model_select(model_id: int) -> JSONResponse:
    m = db_get_model(model_id)
    if not m:
        raise HTTPException(404, "Model not found")
    if not m.get("glb_path") or not m.get("stl_path"):
        raise HTTPException(400, "Model files not ready yet")
    glb_src = Path(m["glb_path"])
    stl_src = Path(m["stl_path"])
    if not glb_src.exists() or not stl_src.exists():
        raise HTTPException(404, "Model files missing from disk")
    shutil.copy2(str(glb_src), str(OUTPUT_DIR / "model.glb"))
    shutil.copy2(str(stl_src), str(OUTPUT_DIR / "model.stl"))
    pipeline_state.update({
        "status":   "model_ready",
        "prompt":   m["prompt"],
        "task_id":  m.get("meshy_task_id"),
        "model_id": m["id"],
        "glb_path": str(OUTPUT_DIR / "model.glb"),
        "stl_path": str(OUTPUT_DIR / "model.stl"),
        "error":    None,
    })
    return JSONResponse({"ok": True, "prompt": m["prompt"]})


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------
@app.get("/events")
async def sse_events() -> StreamingResponse:
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers.append(q)

    async def generator() -> AsyncGenerator[str, None]:
        for event in list(_event_buffer):
            yield f"data: {json.dumps(event)}\n\n"
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            if q in _subscribers:
                _subscribers.remove(q)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection":    "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
