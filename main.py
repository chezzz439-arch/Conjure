import os
import re
import math
import json
import time
import hashlib
import logging
import sqlite3
import asyncio
import shutil
import tempfile
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

import requests
import aiofiles
import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse
from pydantic import BaseModel
from supabase import create_client, Client

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("conjure")

MESHY_API_KEY        = os.getenv("MESHY_API_KEY", "")
INSFORGE_API_KEY     = os.getenv("INSFORGE_API_KEY", "")
INSFORGE_BASE_URL    = os.getenv("INSFORGE_BASE_URL", "https://api.insforge.dev")
ORCASLICER_PATH      = os.getenv("ORCASLICER_PATH", "/usr/bin/orcaslicer")
ORCASLICER_PROFILE   = os.getenv("ORCASLICER_PROFILE", str(BASE_DIR / "profiles" / "neptune4plus_orca.json"))
CURAENGINE_PATH      = os.getenv("CURAENGINE_PATH", "/usr/bin/CuraEngine")
CURA_RESOURCES_PATH  = os.getenv("CURA_RESOURCES_PATH", "/usr/share/cura/resources")
PRINTER_PROFILE      = os.getenv("PRINTER_PROFILE", "neptune4plus")
USB_MOUNT_PATH       = os.getenv("USB_MOUNT_PATH", "/media/usb")
OUTPUT_DIR           = Path(os.getenv("OUTPUT_DIR", str(BASE_DIR / "output")))
DB_PATH              = BASE_DIR / "conjure.db"
ELEVENLABS_API_KEY   = os.getenv("ELEVENLABS_API_KEY", "")
# Default voice: Sarah (premade) — Rachel (21m00Tcm4TlvDq8ikWAM) became a
# library voice, which free-tier API keys can no longer use (HTTP 402).
ELEVENLABS_VOICE_ID  = os.getenv("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")
SUPABASE_URL         = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY    = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_BUCKET      = os.getenv("SUPABASE_BUCKET", "conjure-models")

# ── Engineer-mode config (research + parametric CAD pipeline) ──────────────
# CURAENGINE_PATH and CURA_RESOURCES_PATH are already defined above.
ANTHROPIC_API_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL      = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
OPENSCAD_PATH        = os.getenv("OPENSCAD_PATH", "/usr/bin/openscad")
PRINTER_IP           = os.getenv("PRINTER_IP", "")
MOONRAKER_PORT       = os.getenv("MOONRAKER_PORT", "7125")
# Fluidd is only a web UI — the thing that actually accepts files and starts
# prints is Moonraker underneath it, so that is what the kiosk talks to.
# MOONRAKER_URL overrides IP+port outright, for a reverse proxy or https.
MOONRAKER_URL        = os.getenv("MOONRAKER_URL", "").rstrip("/")
MOONRAKER_API_KEY    = os.getenv("MOONRAKER_API_KEY", "")
MOONRAKER_TIMEOUT    = float(os.getenv("MOONRAKER_TIMEOUT", "20"))

# ── Local speech-to-text (whisper.cpp) ─────────────────────────────────────
# The kiosk browser is snap Chromium, which ships no Google Speech API key, so
# the Web Speech API (webkitSpeechRecognition) fails with a hard `network`
# error — voice input never worked through the browser. We transcribe on-device
# instead: the browser records the mic and POSTs the clip to /api/transcribe,
# which runs it through whisper.cpp. whisper-server keeps the model resident so
# each utterance is fast; whisper-cli is the cold fallback if the server is down.
WHISPER_DIR        = Path(os.getenv("WHISPER_DIR", "/home/watcher/whisper.cpp"))
# base.en is the kiosk default: ~2-3x faster than small.en on this ARM CPU with
# near-identical accuracy on short spoken prompts. Set WHISPER_MODEL to the
# ggml-small.en.bin path if you want maximum accuracy at the cost of latency.
WHISPER_MODEL      = os.getenv("WHISPER_MODEL", str(WHISPER_DIR / "models" / "ggml-base.en.bin"))
WHISPER_HOST       = os.getenv("WHISPER_HOST", "127.0.0.1")
WHISPER_PORT       = int(os.getenv("WHISPER_PORT", "8181"))
WHISPER_THREADS    = os.getenv("WHISPER_THREADS", str(os.cpu_count() or 4))
FFMPEG_PATH        = os.getenv("FFMPEG_PATH", "/usr/bin/ffmpeg")
_WHISPER_BIN_DIR   = WHISPER_DIR / "build" / "bin"
WHISPER_SERVER_BIN = _WHISPER_BIN_DIR / "whisper-server"
WHISPER_CLI_BIN    = _WHISPER_BIN_DIR / "whisper-cli"
WHISPER_URL        = f"http://{WHISPER_HOST}:{WHISPER_PORT}/inference"

_whisper_proc = None
_whisper_lock = threading.Lock()


def _whisper_server_healthy() -> bool:
    try:
        return requests.get(f"http://{WHISPER_HOST}:{WHISPER_PORT}/", timeout=1).status_code < 500
    except Exception:
        return False


def ensure_whisper_server() -> bool:
    """Start whisper-server (model resident) if it isn't already up. Returns
    True when the server answers. Best-effort: /api/transcribe falls back to
    whisper-cli if this never comes up, so a failure here is not fatal."""
    global _whisper_proc
    if not WHISPER_SERVER_BIN.exists():
        return False
    with _whisper_lock:
        if _whisper_proc and _whisper_proc.poll() is None:
            return _whisper_server_healthy()
        env = dict(os.environ, LD_LIBRARY_PATH=str(_WHISPER_BIN_DIR))
        try:
            _whisper_proc = subprocess.Popen(
                [str(WHISPER_SERVER_BIN), "-m", WHISPER_MODEL,
                 "--host", WHISPER_HOST, "--port", str(WHISPER_PORT),
                 "-t", str(WHISPER_THREADS), "-l", "en"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
            )
            log.info("whisper-server starting on %s:%s (model %s)",
                     WHISPER_HOST, WHISPER_PORT, WHISPER_MODEL)
        except Exception as e:
            log.warning("whisper-server failed to start: %s", e)
            return False
    # Model load takes a few seconds; poll until it binds and is ready.
    for _ in range(60):
        if _whisper_server_healthy():
            return True
        time.sleep(0.5)
    return _whisper_server_healthy()


def stop_whisper_server() -> None:
    global _whisper_proc
    if _whisper_proc and _whisper_proc.poll() is None:
        _whisper_proc.terminate()
        try:
            _whisper_proc.wait(timeout=5)
        except Exception:
            _whisper_proc.kill()
    _whisper_proc = None


def _ffmpeg_to_wav(src_path: str, wav_path: str) -> None:
    """Normalise any browser-recorded clip (webm/ogg/opus) to 16 kHz mono WAV,
    the format both whisper-server and whisper-cli expect."""
    subprocess.run(
        [FFMPEG_PATH, "-y", "-i", src_path, "-ar", "16000", "-ac", "1", "-f", "wav", wav_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=60,
    )


def _transcribe_via_server(wav_path: str) -> str | None:
    try:
        with open(wav_path, "rb") as f:
            r = requests.post(
                WHISPER_URL,
                files={"file": ("audio.wav", f, "audio/wav")},
                data={"response_format": "json", "temperature": "0", "language": "en"},
                timeout=60,
            )
        if r.status_code != 200:
            return None
        try:
            return (r.json().get("text") or "").strip()
        except ValueError:
            return r.text.strip()
    except Exception:
        return None


def _transcribe_via_cli(wav_path: str) -> str:
    env = dict(os.environ, LD_LIBRARY_PATH=str(_WHISPER_BIN_DIR))
    out = subprocess.run(
        [str(WHISPER_CLI_BIN), "-m", WHISPER_MODEL, "-f", wav_path,
         "-nt", "-np", "-l", "en", "-t", str(WHISPER_THREADS)],
        capture_output=True, text=True, env=env, timeout=120,
    )
    return out.stdout.strip()


OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
(OUTPUT_DIR / "models").mkdir(exist_ok=True)
PROFILES_DIR = BASE_DIR / "profiles"
PROFILES_DIR.mkdir(exist_ok=True)

MESHY_BASE    = "https://api.meshy.ai"
# Meshy occasionally stalls a request for well over 15 s while a generation is in
# flight. A single stall used to abort the whole build ("Read timed out") two
# minutes into a job that was still running fine server-side, so every call gets
# a longer ceiling and transient failures are retried instead of surfaced.
MESHY_TIMEOUT = float(os.getenv("MESHY_TIMEOUT", "60"))
MESHY_RETRIES = int(os.getenv("MESHY_RETRIES", "4"))
MESHY_HEADERS = {
    "Authorization": f"Bearer {MESHY_API_KEY}",
    "Content-Type": "application/json",
}


def meshy_request(method: str, url: str, **kw):
    """Meshy call that survives a transient network hiccup.

    Retries timeouts, connection drops and 5xx/429 with a backoff; raises
    immediately on 4xx, which means the request itself is wrong and retrying
    would only burn the user's time on a spinner.
    """
    kw.setdefault("timeout", MESHY_TIMEOUT)
    last = None
    for attempt in range(1, MESHY_RETRIES + 1):
        try:
            resp = requests.request(method, url, **kw)
            if resp.status_code < 500 and resp.status_code != 429:
                return resp
            last = Exception(f"HTTP {resp.status_code} — {resp.text[:160]}")
        except (requests.Timeout, requests.ConnectionError) as e:
            last = e
        if attempt < MESHY_RETRIES:
            backoff = 2 ** attempt          # 2s, 4s, 8s
            log.warning("[Meshy] %s %s failed (%s) — retry %d/%d in %ds",
                        method, url.rsplit("/", 1)[-1], last, attempt, MESHY_RETRIES, backoff)
            time.sleep(backoff)
    raise Exception(f"Meshy unreachable after {MESHY_RETRIES} attempts: {last}")

# ---------------------------------------------------------------------------
# Prompt cleaner — strips voice filler, extracts the object, builds Meshy prompt
# ---------------------------------------------------------------------------
# Appended to every generation prompt. Text-to-3D happily produces display
# geometry — hair-thin bowstrings, floating accessories — that is impossible to
# extrude, so the print constraints are stated up front rather than discovered
# at the slicer.
PRINTABILITY_CLAUSE = (
    "designed as one solid connected piece with thick sturdy walls at least 3mm thick, "
    "a flat stable base that sits on the print bed, no thin strings wires or hairs, "
    "no separate floating parts, no fragile spikes, minimal overhangs"
)
_FILLER = re.compile(
    r"^\s*(?:"
    r"(?:can|could|would|will) you (?:please )?(?:make|create|generate|build|design|print|give me|show me)\s+(?:me\s+)?"
    r"|please (?:make|create|generate|build|design|print)\s+(?:me\s+)?"
    r"|you (?:can\s+)?(?:make|create|generate|build|design|print)\s+(?:me\s+)?"
    r"|i (?:want|need|would like) you to (?:make|create|generate|build|design|print)\s+(?:me\s+)?"
    r"|(?:make|create|generate|build|design|print)\s+(?:me\s+)?"
    r"|i (?:want|need|would like)\s+(?:a\s+|an\s+|to have\s+a\s+|to have\s+an\s+)?"
    r"|give me\s+(?:a\s+|an\s+)?"
    r"|show me\s+(?:a\s+|an\s+)?"
    r")",
    re.IGNORECASE,
)

# Dictated speech rarely starts cleanly. _FILLER is anchored at the start of the
# string, so one leading "uhh" used to defeat the entire cleaner: "uhh can you
# make me a wolf figurine" survived intact, got truncated to its first six words
# and searched Printables for "can you make" — which returns Lego bricks and
# barrels, not wolves. This runs first, and both are applied repeatedly because
# the phrases layer ("ok so can you please make me a ...").
_LEAD_NOISE = re.compile(
    r"^\s*(?:uh+|um+|erm?|hm+|ah+|oh+|ok(?:ay)?|so|well|like|hey|hi|hello|"
    r"yeah|yep|yes|please|now|just|actually|maybe|i think|let'?s)\b[\s,.]*",
    re.IGNORECASE,
)
_LEAD_ARTICLE = re.compile(r"^\s*(?:a|an|the|some)\s+", re.IGNORECASE)


def _strip_request_prefix(text: str) -> str:
    """Peels leading disfluency, politeness and 'make me a' phrasing, repeatedly."""
    prev = None
    while prev != text:
        prev = text
        for pattern in (_LEAD_NOISE, _FILLER, _LEAD_ARTICLE):
            text = pattern.sub("", text).lstrip()
    return text.strip().rstrip(".,!?")


_STAND_RE = re.compile(
    r"\b(stand|holder|mount|rack|dock|cradle|tray|organizer|hanger|hook)\b",
    re.IGNORECASE,
)
_STAND_ITEM_RE = re.compile(
    r"^([\w\s]+?)\s+(?:stand|holder|mount|rack|dock|cradle|tray|organizer|hanger|hook)\b",
    re.IGNORECASE,
)
_WITH_RE = re.compile(r"\s+with\b.+$", re.IGNORECASE)

# "a phone case for my iPhone 17 Pro Max" wraps four words of glue around the two
# things that actually matter. Left in, the six-word keyword cap spent its budget
# on "for my" and truncated to "phone case for my iPhone 17" — dropping the exact
# model designation the listing titles are keyed on, which found one unrelated
# dock. Removing the glue gives "phone case iPhone 17 Pro Max", which matches.
_OWNER_GLUE = re.compile(
    r"\s+\b(?:for|to\s+fit|that\s+fits|which\s+fits|fitting)\s+"
    r"(?:my|me|a|an|the|our|his|her|their)\b\s*",
    re.IGNORECASE,
)

# Strips trailing noise phrases that users append when speaking naturally
_DECO_NOISE = re.compile(
    r"\s*\b(?:on\s+(?:it|them|the\s+\w+)|(?:in|as|for)\s+the\s+design|on\s+the\s+design|as\s+(?:a\s+)?design|as\s+decoration)\b.*$",
    re.IGNORECASE,
)

# A "with X" clause is treated as flat surface decoration only when it names an
# actual surface treatment (pattern, texture, engraving, logo, stars…). Anything
# else — "with shark fins on the sides", "with a handle" — is a structural
# feature the user wants built as real 3D geometry.
_SURFACE_DECO_RE = re.compile(
    r"\b(?:patterns?|textures?|textured|engrav\w+|emboss\w+|etch\w+|designs?|motifs?"
    r"|logos?|prints?|stars?|dots?|stripes?|geometric|floral|swirls?|filigree|inlays?)\b",
    re.IGNORECASE,
)

# Structural descriptions for common objects so Meshy understands the form first
_OBJECT_SHAPES = {
    # Furniture
    "dining table":     "rectangular flat tabletop supported by four vertical legs, furniture",
    "coffee table":     "low rectangular tabletop supported by four short legs, furniture",
    "side table":       "small square tabletop on four legs, furniture",
    "table":            "flat rectangular tabletop supported by four vertical legs, furniture piece, NOT a tile or plaque",
    "desk":             "wide flat rectangular surface on four legs with a drawer, furniture",
    "chair":            "seat with a flat seat surface, backrest, and four legs, furniture",
    "stool":            "round seat on three or four legs, no backrest, furniture",
    "shelf":            "horizontal flat rectangular board mounted on a wall bracket, furniture",
    "bookshelf":        "tall rectangular unit with multiple horizontal shelves, furniture",
    "cabinet":          "rectangular box with a door on the front, furniture",
    "drawer":           "rectangular box that slides in and out of a frame",
    "bench":            "long flat seat on four legs, no backrest, furniture",
    "bed frame":        "rectangular frame with headboard, footboard, and side rails, furniture",
    "nightstand":       "small box-shaped bedside table on four legs with a drawer, furniture",
    # Storage / containers
    "phone holder":     "vertical stand with a slot or groove to hold a phone upright",
    "phone stand":      "vertical stand with a slot or groove to hold a phone upright",
    "headphone stand":  "tall stand with an arch or hook at the top to hang headphones",
    "headphone holder": "tall stand with an arch or hook at the top to hang headphones",
    "pen holder":       "cylindrical cup open at the top to hold pens and pencils",
    "pen cup":          "cylindrical cup open at the top to hold pens and pencils",
    "vase":             "hollow vessel with a narrow opening at the top to hold flowers",
    "mug":              "cylindrical cup with a handle on the side",
    "cup":              "cylindrical open-top drinking vessel",
    "bowl":             "round open-top container, wider than it is tall",
    "box":              "hollow rectangular container with a flat lid",
    "ring holder":      "cone or finger-shaped stand to hold rings upright",
    "cable organizer":  "flat tray with slots or hooks for organizing cables",
    "planter":          "hollow pot open at the top for holding soil and plants",
    "pot":              "hollow cylindrical open-top container",
    "basket":           "open-top woven container with a handle",
    "tray":             "flat shallow rectangular container with raised edges",
    # Tools / accessories
    "lamp":             "vertical pole on a flat base with a shade at the top",
    "bottle":           "narrow-necked cylindrical container with a cap",
    "can":              "cylindrical metal container with a flat bottom and top",
    "hook":             "curved metal peg for hanging items on a wall",
    "key holder":       "flat panel with protruding pegs or hooks for hanging keys",
    "coat hook":        "wall-mounted peg with a curved tip for hanging coats",
    "name tag":         "small flat rectangular badge with raised lettering",
    "coaster":          "small flat circular disc used under a drink",
    "plate":            "flat circular disc with a shallow raised rim",
    "jar":              "cylindrical container with a wide mouth and screw-top lid",
    "funnel":           "cone-shaped object with a narrow spout at the bottom",
    "bracket":          "L-shaped flat support for mounting shelves on walls",
}


def build_meshy_prompt(raw: str) -> str:
    cleaned = _strip_request_prefix(raw)
    if not cleaned:
        cleaned = raw.strip()

    # Split off "with X decoration" clause from the base object name
    with_match = _WITH_RE.search(cleaned)
    decoration = with_match.group(0).strip() if with_match else ""
    base_object = _WITH_RE.sub("", cleaned).strip() if with_match else cleaned

    # Look up a structural description — match longest key first to avoid partial hits
    # Whole words only. A plain substring test matched "box" inside "gearbox"
    # and told Meshy a planetary gearbox was a "hollow rectangular container
    # with a flat lid" — actively describing the wrong object. The optional
    # plural keeps "boxes"/"vases" matching.
    shape_hint = ""
    base_lower = base_object.lower()
    for key in sorted(_OBJECT_SHAPES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(key)}(?:e?s)?\b", base_lower):
            shape_hint = _OBJECT_SHAPES[key]
            break

    parts = []

    # Lead with the structural form — shape hint forces Meshy to build the right silhouette
    if shape_hint:
        parts.append(f"3D printable {base_object}: {shape_hint}")
    else:
        parts.append(f"3D printable {base_object}")

    # Decoration / added-feature clause. Two distinct cases:
    #   • Surface decoration (pattern, texture, engraving, stars, logo…) — emboss
    #     it flat and explicitly forbid shape changes.
    #   • Structural feature ("with shark fins on the sides", "with a handle") —
    #     the user wants real geometry, so ask for it as actual 3D form and keep
    #     the location words. Skipped for stands/holders, whose "with X" usually
    #     means the item being held (handled by the stand branch below).
    if decoration:
        deco_raw = re.sub(r"^\s*with\s+", "", decoration, flags=re.IGNORECASE).strip()
        is_stand = bool(_STAND_RE.search(base_object))
        if _SURFACE_DECO_RE.search(deco_raw):
            deco_clean = _DECO_NOISE.sub("", deco_raw).strip()
            if deco_clean:
                # Avoid doubling: "geometric patterns pattern" — only append " pattern" if not already there
                suffix = "" if re.search(r"\bpatterns?\b", deco_clean, re.IGNORECASE) else " pattern"
                parts.append(
                    f"{deco_clean}{suffix} embossed on the surface as decoration only, "
                    f"do NOT change the overall shape of the object"
                )
        elif not is_stand and deco_raw:
            parts.append(
                f"with {deco_raw}, modeled as actual raised 3D geometry that is part "
                f"of the object, not a flat surface texture"
            )

    # For stands/holders: explicitly empty so nothing sits on top
    if _STAND_RE.search(base_object):
        item_match = _STAND_ITEM_RE.match(base_object)
        if item_match:
            item = item_match.group(1).strip()
            parts.append(f"empty stand, no {item} placed on it")
        else:
            parts.append("empty stand with nothing placed on it")

    # These two are ours, not the user's, so they yield first when space is tight.
    return _fit_meshy_prompt(parts, [
        "single isolated object, clean manifold mesh, no scene, "
        "no background objects, suitable for FDM 3D printing",
        PRINTABILITY_CLAUSE,
    ])


# Meshy rejects anything longer than this outright (HTTP 400), and our own
# boilerplate is ~400 of those characters — so a detailed request like a
# planetary gearbox blew the limit on the suffix, not on the user's own words.
MESHY_PROMPT_MAX = 800


def _fit_meshy_prompt(essential: list[str], optional: list[str] = ()) -> str:
    """Joins the prompt clauses so the result always fits Meshy's cap.

    `essential` is what the user actually asked for and is never dropped — only
    truncated, at a word boundary, if it alone overflows. `optional` is our own
    printability boilerplate, added only while there is room.

    Deliberately fills the budget rather than shedding until it fits: dropping a
    whole 700-character description to satisfy an 800-character limit throws away
    the request to save the hints about how to print it, which is backwards.
    """
    text = ", ".join(p for p in essential if p)
    if len(text) > MESHY_PROMPT_MAX:
        hard = text[:MESHY_PROMPT_MAX]
        word = hard.rsplit(" ", 1)[0].rstrip(" ,;-—")
        # Backing up to a word boundary is nicer to read, but one unbroken
        # 1200-character "word" would back all the way up to "3D printable".
        # Below half the budget, keep the blunt cut — more of the request
        # survives, and Meshy tolerates a clipped final token.
        text = word if len(word) >= MESHY_PROMPT_MAX // 2 else hard
        log.warning("[Meshy] description alone exceeds %d chars — truncated to %d",
                    MESHY_PROMPT_MAX, len(text))
        return text

    for clause in optional:
        if not clause:
            continue
        if len(text) + 2 + len(clause) <= MESHY_PROMPT_MAX:
            text += ", " + clause
        else:
            log.info("[Meshy] no room for printability clause (%d chars used) — dropped %r",
                     len(text), clause[:60])
    return text


# ---------------------------------------------------------------------------
# LLM metaprompt — Claude rewrites the raw transcript into a strong Meshy
# prompt + a short search query for the model-library lookup.
# Falls back to the regex build_meshy_prompt() pipeline when the key is
# missing or the API call fails, so voice → print never breaks on LLM outages.
# ---------------------------------------------------------------------------
_QUERY_SYSTEM = """You read raw voice transcripts from a 3D-printing kiosk and name the object.

"search_query" — 2-4 plain keywords for searching 3D model libraries like Printables
(e.g. "phone stand", "dragon planter", "bow and arrow"). Strip filler ("uhh", "can you
make me..."), collapse any stuttered repetition, and drop decoration adjectives unless
they are essential to what the object IS. Library titles name the object, not its
modifiers, so keep it short.

"object_name" — the same thing as a short human-readable label (e.g. "Phone stand")."""

_MESHY_SYSTEM = """You turn a raw voice transcript from a 3D-printing kiosk into a prompt
for the Meshy text-to-3D API. Rules:
 - Describe ONE single isolated object with its structural form spelled out
   (e.g. "phone stand: vertical back support with a front lip groove to hold a phone upright").
 - Strip filler ("can you make me...", "I want..."); collapse stuttered repetition.
 - Keep every functional/decorative detail the user asked for. Surface decoration
   (patterns, logos, engravings) should be described as embossed on the surface without
   changing the overall shape; structural features ("with a handle") as real 3D geometry.
 - It must describe a PRINTABLE PART, not just a nice 3D model. Re-express anything
   that cannot be FDM printed: a bow's string becomes a thick solid bar joining the
   limbs, an arrow is fused to the bow or omitted, thin blades/spikes become chunky.
   Never describe strings, wires, hairs, cloth, separate floating pieces or hollow shells.
 - End with: "single isolated object, clean manifold mesh, no scene, no background objects,
   suitable for FDM 3D printing, one solid connected piece with walls at least 3mm thick,
   flat stable base, no thin strings or floating parts"."""


# The kiosk speaks while this runs, so it is latency-sensitive but short and
# well-scoped — low effort keeps it quick without dropping to a smaller model.
_QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "search_query": {"type": "string"},
        "object_name":  {"type": "string"},
    },
    "required": ["search_query", "object_name"],
    "additionalProperties": False,
}

_MESHY_SCHEMA = {
    "type": "object",
    "properties": {"meshy_prompt": {"type": "string"}},
    "required": ["meshy_prompt"],
    "additionalProperties": False,
}

_anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

# USD per token, input/output. Anything not listed falls back to Sonnet pricing
# and is flagged in the response so an unpriced model reads as an estimate rather
# than silently reporting a wrong number.
MODEL_PRICING = {
    "claude-opus-4-8":   (5.00 / 1e6, 25.00 / 1e6),
    "claude-opus-4-7":   (5.00 / 1e6, 25.00 / 1e6),
    "claude-opus-4-6":   (5.00 / 1e6, 25.00 / 1e6),
    "claude-sonnet-4-6": (3.00 / 1e6, 15.00 / 1e6),
    "claude-haiku-4-5":  (1.00 / 1e6,  5.00 / 1e6),
}
_DEFAULT_PRICING = MODEL_PRICING["claude-sonnet-4-6"]


def _llm_cost(model: str, tokens_in: int, tokens_out: int) -> tuple[float, bool]:
    """Returns (usd, priced) — priced is False when the model isn't in the table."""
    pricing = MODEL_PRICING.get(model)
    rate_in, rate_out = pricing or _DEFAULT_PRICING
    return tokens_in * rate_in + tokens_out * rate_out, pricing is not None


def record_llm_usage(label: str, model: str, tokens_in: int, tokens_out: int,
                     cached_in: int = 0) -> None:
    """Appends one call to the usage ledger.

    Deliberately never raises: a bookkeeping failure must not take down a
    generation the user is waiting on.
    """
    cost, priced = _llm_cost(model, tokens_in, tokens_out)
    log.info("[Usage] %s %s — in=%d out=%d cached=%d $%.6f%s",
             label, model, tokens_in, tokens_out, cached_in, cost,
             "" if priced else " (est: unpriced model)")
    try:
        with _db_lock:
            conn = sqlite3.connect(str(DB_PATH))
            conn.execute(
                "INSERT INTO llm_usage (ts, label, model, tokens_in, tokens_out, "
                "cached_in, cost_usd) VALUES (?,?,?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), label, model,
                 tokens_in, tokens_out, cached_in, cost),
            )
            conn.commit()
            conn.close()
    except Exception as e:
        log.warning("[Usage] could not record: %s", e)


def _llm_json(system: str, raw: str, schema: dict, label: str) -> dict | None:
    """One structured-output call. Returns the parsed object, or None on any
    failure so every caller can fall back to the regex pipeline."""
    if not _anthropic_client:
        log.info("[%s] no ANTHROPIC_API_KEY — using regex fallback", label)
        return None
    try:
        response = _anthropic_client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=1000,
            system=system,
            messages=[{"role": "user", "content": raw.strip()}],
            output_config={
                "format": {"type": "json_schema", "schema": schema},
                "effort": "low",
            },
        )
        # Billed whether or not the body parses, so record before anything can
        # return early — otherwise the ledger quietly under-reports on failures.
        u = response.usage
        record_llm_usage(label, ANTHROPIC_MODEL, u.input_tokens, u.output_tokens,
                         getattr(u, "cache_read_input_tokens", 0) or 0)
        text = next((b.text for b in response.content if b.type == "text"), "")
        if not text:
            log.warning("[%s] empty reply (stop_reason=%s)", label, response.stop_reason)
            return None
        return json.loads(text)
    except Exception as e:
        log.warning("[%s] failed (%s) — using regex fallback", label, e)
        return None


def _llm_text(system: str, user: str, label: str, max_tokens: int = 2000) -> str | None:
    """A plain-text completion. Same accounting as _llm_json, but the engineer
    pipeline wants OpenSCAD source, which is code — not a JSON payload."""
    if not _anthropic_client:
        log.info("[%s] no ANTHROPIC_API_KEY", label)
        return None
    try:
        response = _anthropic_client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user.strip()}],
        )
        u = response.usage
        record_llm_usage(label, ANTHROPIC_MODEL, u.input_tokens, u.output_tokens,
                         getattr(u, "cache_read_input_tokens", 0) or 0)
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            log.warning("[%s] empty reply (stop_reason=%s)", label, response.stop_reason)
            return None
        return text
    except Exception as e:
        log.warning("[%s] failed (%s)", label, e)
        return None


def llm_search_query(raw: str) -> dict | None:
    """Kiosk-blocking call: the user is watching a spinner, so this asks for
    only the few keywords the library search needs. Keeping the paragraph-long
    meshy_prompt out of this response is what makes the results screen fast."""
    out = _llm_json(_QUERY_SYSTEM, raw, _QUERY_SCHEMA, "SearchQuery")
    if not out or not out.get("search_query"):
        return None
    log.info("[SearchQuery] %r → %r", raw[:60], out["search_query"])
    return out


def llm_meshy_prompt(raw: str) -> str | None:
    """Only needed once the user opts into AI generation, where it is hidden
    inside the ~2 minute Meshy wait — so it can take its time and be verbose."""
    out = _llm_json(_MESHY_SYSTEM, raw, _MESHY_SCHEMA, "MeshyPrompt")
    if not out or not out.get("meshy_prompt"):
        return None
    log.info("[MeshyPrompt] %r → %s", raw[:40], out["meshy_prompt"][:80])
    return out["meshy_prompt"]


# ---------------------------------------------------------------------------
# Model-library search — checked BEFORE Meshy so the kiosk can print a proven
# community design instead of generating from scratch.
#   • Printables — keyless unofficial GraphQL (operation names pinned below;
#     they are undocumented and may change — every call degrades gracefully).
#   • Thingiverse — optional, needs THINGIVERSE_APP_TOKEN in .env
#     (free: https://www.thingiverse.com/developers).
# ---------------------------------------------------------------------------
THINGIVERSE_APP_TOKEN = os.getenv("THINGIVERSE_APP_TOKEN", "")

PRINTABLES_GQL   = "https://api.printables.com/graphql/"
PRINTABLES_MEDIA = "https://media.printables.com/"
_LIBRARY_UA      = {"User-Agent": "ConjureKiosk/1.0", "Content-Type": "application/json"}

_PRINTABLES_SEARCH_Q = """query SearchModels($query: String!, $limit: Int, $ordering: SearchChoicesEnum) {
  result: searchPrints2(query: $query, printType: print, limit: $limit, ordering: $ordering) {
    items { id name slug ratingAvg likesCount downloadCount user { publicUsername } image { filePath } }
  }
}"""
_PRINTABLES_FILES_Q = """query ModelFiles($id: ID!) {
  model: print(id: $id) { id stls { id name fileSize } }
}"""
_PRINTABLES_LINK_M = """mutation GetDownloadLink($id: ID!, $modelId: ID!, $fileType: DownloadFileTypeEnum!, $source: DownloadSourceEnum!) {
  getDownloadLink(id: $id, printId: $modelId, fileType: $fileType, source: $source) {
    ok errors { field messages } output { link count ttl }
  }
}"""


def _printables_gql(operation: str, query: str, variables: dict) -> dict | None:
    try:
        r = requests.post(
            PRINTABLES_GQL,
            headers=_LIBRARY_UA,
            json={"operationName": operation, "query": query, "variables": variables},
            timeout=15,
        )
        if r.status_code != 200:
            log.warning("[Printables] HTTP %s: %s", r.status_code, r.text[:150])
            return None
        body = r.json()
        if body.get("errors"):
            log.warning("[Printables] GraphQL errors: %s", str(body["errors"])[:200])
            return None
        return body.get("data")
    except Exception as e:
        log.warning("[Printables] request failed: %s", e)
        return None


def printables_search(query: str, limit: int = 6) -> list[dict]:
    data = _printables_gql(
        "SearchModels", _PRINTABLES_SEARCH_Q,
        {"query": query, "limit": limit, "ordering": "best_match"},
    )
    items = ((data or {}).get("result") or {}).get("items") or []
    results = []
    for it in items:
        img = (it.get("image") or {}).get("filePath") or ""
        results.append({
            "source":    "printables",
            "model_id":  str(it["id"]),
            "name":      it.get("name") or "Untitled",
            "author":    (it.get("user") or {}).get("publicUsername") or "",
            "thumbnail": (PRINTABLES_MEDIA + img) if img else None,
            "likes":     it.get("likesCount") or 0,
            "downloads": it.get("downloadCount") or 0,
            "rating":    round(float(it.get("ratingAvg") or 0), 1),
            "url":       f"https://www.printables.com/model/{it['id']}-{it.get('slug', '')}",
        })
    return results


def printables_stl_files(model_id: str) -> list[dict]:
    data = _printables_gql("ModelFiles", _PRINTABLES_FILES_Q, {"id": model_id})
    return ((data or {}).get("model") or {}).get("stls") or []


def printables_download_url(file_id: str, model_id: str) -> str | None:
    data = _printables_gql(
        "GetDownloadLink", _PRINTABLES_LINK_M,
        {"id": file_id, "modelId": model_id, "fileType": "stl", "source": "model_detail"},
    )
    out = ((data or {}).get("getDownloadLink") or {})
    if out.get("ok") and out.get("output", {}).get("link"):
        return out["output"]["link"]
    log.warning("[Printables] getDownloadLink failed: %s", str(out)[:200])
    return None


def thingiverse_search(query: str, limit: int = 6) -> list[dict]:
    if not THINGIVERSE_APP_TOKEN:
        return []
    try:
        r = requests.get(
            f"https://api.thingiverse.com/search/{requests.utils.quote(query)}/",
            params={"type": "things", "per_page": limit},
            headers={"Authorization": f"Bearer {THINGIVERSE_APP_TOKEN}"},
            timeout=15,
        )
        if r.status_code != 200:
            log.warning("[Thingiverse] HTTP %s: %s", r.status_code, r.text[:150])
            return []
        hits = (r.json() or {}).get("hits") or []
        return [{
            "source":    "thingiverse",
            "model_id":  str(h["id"]),
            "name":      h.get("name") or "Untitled",
            "author":    (h.get("creator") or {}).get("name") or "",
            "thumbnail": h.get("thumbnail"),
            "likes":     h.get("like_count") or 0,
            "downloads": h.get("download_count") or 0,
            "rating":    0,
            "url":       h.get("public_url") or "",
        } for h in hits]
    except Exception as e:
        log.warning("[Thingiverse] search failed: %s", e)
        return []


def thingiverse_stl_url(thing_id: str) -> tuple[str | None, str | None]:
    """Returns (download_url, file_name) for the first STL of a thing."""
    try:
        r = requests.get(
            f"https://api.thingiverse.com/things/{thing_id}/files",
            headers={"Authorization": f"Bearer {THINGIVERSE_APP_TOKEN}"},
            timeout=15,
        )
        if r.status_code != 200:
            return None, None
        for f in r.json() or []:
            if (f.get("name") or "").lower().endswith(".stl"):
                return f.get("download_url"), f.get("name")
        return None, None
    except Exception as e:
        log.warning("[Thingiverse] files failed: %s", e)
        return None, None


def search_model_libraries(query: str, limit: int = 6) -> tuple[list[dict], list[dict]]:
    """Searches every library and reports per-source status for the kiosk UI.

    Returns (results, sources) where sources is
    [{"name","status","count"}] with status ok | empty | error | no_key.
    """
    sources: list[dict] = []

    try:
        p_results = printables_search(query, limit=limit)
        sources.append({"name": "Printables", "status": "ok" if p_results else "empty",
                        "count": len(p_results)})
    except Exception as e:
        log.warning("[Library] Printables failed: %s", e)
        p_results = []
        sources.append({"name": "Printables", "status": "error", "count": 0})

    remaining = max(0, limit - len(p_results))
    if not THINGIVERSE_APP_TOKEN:
        t_results = []
        sources.append({"name": "Thingiverse", "status": "no_key", "count": 0})
    else:
        try:
            t_results = thingiverse_search(query, limit=remaining) if remaining else []
            sources.append({"name": "Thingiverse", "status": "ok" if t_results else "empty",
                            "count": len(t_results)})
        except Exception as e:
            log.warning("[Library] Thingiverse failed: %s", e)
            t_results = []
            sources.append({"name": "Thingiverse", "status": "error", "count": 0})

    return p_results + t_results, sources


_TRAILING_STOPWORDS = {"for", "with", "to", "my", "the", "a", "an", "of", "in",
                       "on", "that", "and", "or", "it", "me"}


def _trim_stopwords(text: str) -> str:
    words = text.split()
    while words and words[-1].lower() in _TRAILING_STOPWORDS:
        words.pop()
    return " ".join(words)


def _query_variants(query: str) -> list[str]:
    """Progressively shorter queries, so a wordy request still finds the
    standard object ("miniature bow and arrow for a toy" → "bow and arrow")."""
    q = _trim_stopwords(_dedupe_phrases(query))
    variants = [q]
    words = q.split()
    # Drop leading adjectives one at a time, then keep only the last two words —
    # library titles are named after the object, not its modifiers.
    for start in range(1, min(len(words), 4)):
        tail = _trim_stopwords(" ".join(words[start:]))
        if tail and tail not in variants:
            variants.append(tail)
    if len(words) > 2:
        tail2 = _trim_stopwords(" ".join(words[-2:]))
        if tail2 and tail2 not in variants:
            variants.append(tail2)
    return variants


def search_with_fallback(query: str, limit: int = 6) -> tuple[list[dict], list[dict], str]:
    """Tries progressively shorter queries until a library returns something."""
    sources: list[dict] = []
    for attempt in _query_variants(query):
        results, sources = search_model_libraries(attempt, limit=limit)
        if results:
            if attempt != query:
                log.info("[Library] %r found nothing — matched on %r", query, attempt)
            return results, sources, attempt
    return [], sources, query


def _dedupe_phrases(text: str) -> str:
    """Collapses an immediately repeated phrase, e.g. a stuttering speech engine
    emitting "make a bow and arrowmake a bow and arrow"."""
    s = " ".join(text.split())
    # Whole string is the same phrase twice, with or without a space between.
    m = re.fullmatch(r"(.{3,}?)\s*\1", s, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Same phrase repeated back-to-back inside a longer string.
    words = s.split()
    for size in range(len(words) // 2, 1, -1):
        for i in range(len(words) - 2 * size + 1):
            a = [w.lower() for w in words[i:i + size]]
            b = [w.lower() for w in words[i + size:i + 2 * size]]
            if a == b:
                return " ".join(words[:i + size] + words[i + 2 * size:])
    return s


def _fallback_search_query(raw: str) -> str:
    """Regex-only search keywords when the LLM metaprompt is unavailable."""
    cleaned = _strip_request_prefix(_dedupe_phrases(raw))
    cleaned = _WITH_RE.sub("", cleaned).strip()
    cleaned = _OWNER_GLUE.sub(" ", cleaned).strip()
    # Long dictated sentences never match library titles — keep it to keywords.
    words = cleaned.split()
    if len(words) > 6:
        cleaned = " ".join(words[:6])
    cleaned = _trim_stopwords(cleaned)
    return cleaned or raw.strip()


# Meshy is a decorative text-to-3D generator: it has no notion of a real device's
# dimensions, so "phone case for my iPhone 17 Pro Max" comes back as a solid
# phone, not a case that fits one. No prompt wording fixes that. These parts only
# work when someone has measured the thing they clip onto, which is exactly what
# the community libraries have and Meshy doesn't — so say so before the user
# spends Meshy credits on a model that cannot fit.
_FITTED_PART = re.compile(
    r"\b(?:case|cover|sleeve|holster|bracket|mount|adapter|enclosure|lid|cap|"
    r"gasket|insert|clip|shim|spacer|bushing|coupler|thread|screw|bolt|nut)\b",
    re.IGNORECASE,
)


def fitted_part_warning(raw: str) -> str | None:
    """A caution for parts whose whole job is matching another object's size."""
    if not _FITTED_PART.search(raw or ""):
        return None
    return ("A part like this has to match real measurements to fit, and AI "
            "generation only guesses at shape — pick a library match if there "
            "is one, since those were modelled from the actual dimensions.")


# ---------------------------------------------------------------------------
# Printability — makes sure what leaves the kiosk is a printable PART, not just
# a 3D model. Meshy emits arbitrary units, and the slicer only ever scales DOWN
# to fill the bed, so a generated bow arrived 1.9 m tall and got squashed to fit
# — leaving a sub-nozzle bowstring that no printer can extrude. Generated meshes
# are therefore repaired and normalized to a real physical size here, then
# measured; library STLs are already authored in mm and keep their scale.
# ---------------------------------------------------------------------------
# Elegoo Neptune 4 Plus. Elegoo's own OrcaSlicer profile declares 325×325×385;
# a few mm are shaved off each axis so a part that measures exactly bed-size
# doesn't collide with the clips at the edge.
BED_X_MM = float(os.getenv("BED_X_MM", "320"))
BED_Y_MM = float(os.getenv("BED_Y_MM", "320"))
BED_Z_MM = float(os.getenv("BED_Z_MM", "380"))
NOZZLE_MM        = 0.4
MIN_WALL_MM      = 2 * NOZZLE_MM       # two perimeters — below this won't extrude
TARGET_LONGEST_MM = float(os.getenv("TARGET_LONGEST_MM", "120"))

# A community STL whose longest edge lands outside this band is almost certainly
# not authored in mm — Printables hosts plenty of files exported in cm, inches or
# metres. Below the floor nothing is printable at all (a 3 mm wolf is a speck);
# above the ceiling nothing fits any bed.
SANE_MIN_MM = 15.0
SANE_MAX_MM = 1000.0

# Tried largest-first: an inch-authored file and a cm-authored one can both land
# in the sane band, and inches is by far the more common export mistake.
UNIT_GUESSES = [("inches", 25.4), ("cm", 10.0), ("metres", 1000.0)]


# glTF/GLB is Y-up; STL is Z-up. Neither format records which convention its
# author used, so trimesh copies vertices across untouched and the 90° is lost.
# That is why library models showed up lying on their side in the viewer while
# Meshy models (already Y-up) looked fine — and why a Meshy GLB converted to STL
# reaches the slicer tipped over. Both directions have to be applied explicitly.
def _rotate_x(mesh, degrees: float):
    import trimesh
    mesh.apply_transform(
        trimesh.transformations.rotation_matrix(math.radians(degrees), [1, 0, 0])
    )
    return mesh


def _z_up_to_y_up(mesh):
    """STL/slicer convention → viewer convention. (x, y, z) → (x, z, -y)."""
    return _rotate_x(mesh, -90.0)


def _y_up_to_z_up(mesh):
    """Viewer/Meshy convention → STL convention. (x, y, z) → (x, -z, y)."""
    return _rotate_x(mesh, 90.0)


def _mesh_report(mesh, scale_note: str, generated: bool) -> dict:
    """Measures a mesh (already in mm) and flags what will not print.

    Severity depends on provenance. A community design with thousands of
    successful prints is allowed to be multi-part and slightly leaky — slicers
    handle both routinely — so those are notes, not warnings. The same traits
    in a freshly generated mesh are real defects.
    """
    warnings: list[str] = []
    notes: list[str] = []
    ext = [round(float(v), 1) for v in mesh.extents]

    # Mean wall thickness: for a slab of thickness t, area ≈ 2×face area, so
    # 2·V/A ≈ t. Cheap, dependency-free, and it cleanly separates a hair-thin
    # bowstring (~0.4 mm) from a phone stand (~3 mm) or a figurine (~12 mm).
    thickness = (2.0 * float(mesh.volume) / float(mesh.area)) if mesh.area > 0 else 0.0

    if thickness < MIN_WALL_MM:
        warnings.append(
            f"Very thin walls (~{thickness:.1f} mm). The nozzle is {NOZZLE_MM} mm, "
            f"so the thinnest parts may not print at all."
        )
    if ext[0] > BED_X_MM or ext[1] > BED_Y_MM or ext[2] > BED_Z_MM:
        warnings.append(f"Larger than the build plate ({ext[0]}×{ext[1]}×{ext[2]} mm).")
    if not mesh.is_watertight:
        (warnings if generated else notes).append(
            "Mesh has holes — the slicer will close small ones automatically."
        )

    parts = 1
    try:
        # Disconnected chunks = pieces that aren't joined (the arrow floating
        # beside the bow). Needs a trimesh graph engine; skip if unavailable.
        comps = mesh.split(only_watertight=False)
        parts = len(comps)
        if parts > 1:
            loose = [c for c in comps if float(c.extents.min()) < MIN_WALL_MM]
            if generated:
                warnings.append(
                    f"{parts} separate pieces that aren't joined — they print as "
                    f"loose parts" + (f", {len(loose)} too thin to print" if loose else "")
                )
            else:
                notes.append(f"Multi-part design — {parts} pieces on the plate.")
    except Exception as e:
        log.info("[Printability] component split unavailable: %s", e)

    return {
        "dimensions_mm": ext,
        "thickness_mm":  round(thickness, 2),
        "watertight":    bool(mesh.is_watertight),
        "parts":         parts,
        "scale_note":    scale_note,
        "warnings":      warnings,
        "notes":         notes,
        "printable":     not warnings,
    }


def make_printable(stl_path: Path, generated: bool) -> dict:
    """Repairs, scales and seats an STL on the bed, rewriting it in place.

    generated=True  — Meshy output in arbitrary units: normalize the longest
                      edge to TARGET_LONGEST_MM so the part is a sensible object
                      instead of a plate-filling blow-up.
    generated=False — community STL already authored in mm: keep the designer's
                      scale, only shrink if it overflows the bed.
    """
    import trimesh

    mesh = trimesh.load(str(stl_path), force="mesh")
    if mesh.is_empty or len(mesh.faces) == 0:
        raise Exception("Mesh is empty")

    # Repair — cheap fixes that make a mesh sliceable.
    mesh.merge_vertices()
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.update_faces(mesh.unique_faces())
    mesh.remove_infinite_values()
    try:
        # fix_normals() reaches for scipy via body_count; networkx alone doesn't
        # satisfy it. Winding is a nicety, not a blocker — skip it if unavailable.
        mesh.fix_normals()
    except Exception as e:
        log.info("[Printability] fix_normals skipped: %s", e)
    if not mesh.is_watertight:
        try:
            mesh.fill_holes()
        except Exception as e:
            log.info("[Printability] fill_holes skipped: %s", e)

    longest = float(max(mesh.extents))
    if longest <= 0:
        raise Exception("Mesh has zero size")

    if generated:
        factor = TARGET_LONGEST_MM / longest
        scale_note = f"scaled to {TARGET_LONGEST_MM:.0f} mm longest edge"
    elif longest < SANE_MIN_MM or longest > SANE_MAX_MM:
        # "Community STLs are authored in mm" is only mostly true. A Printables
        # wolf figurine arrived 2.4 × 1.8 × 3.0 — kept at "original designer
        # scale" it sliced into a 3 mm speck with sub-nozzle walls. Recover the
        # intended size by testing the usual wrong units, and fall back to the
        # generated-model normalization when none of them fit.
        for unit_name, mult in UNIT_GUESSES:
            if SANE_MIN_MM <= longest * mult <= SANE_MAX_MM:
                factor = mult
                scale_note = f"rescaled from {unit_name} — the file wasn't authored in mm"
                break
        else:
            factor = TARGET_LONGEST_MM / longest
            scale_note = f"odd units — normalized to {TARGET_LONGEST_MM:.0f} mm longest edge"
        log.info("[Printability] %s: longest %.2f mm is implausible — %s",
                 stl_path.name, longest, scale_note)
    else:
        # Only intervene when a community model genuinely won't fit.
        ex, ey, ez = (float(v) for v in mesh.extents)
        factor = min(BED_X_MM / ex, BED_Y_MM / ey, BED_Z_MM / ez, 1.0)
        scale_note = "original designer scale" if factor == 1.0 else "shrunk to fit the build plate"

    if factor != 1.0:
        mesh.apply_scale(factor)

    # Unit recovery multiplies by up to 1000, so re-check the plate afterwards —
    # otherwise a metre-authored file trades "3 mm speck" for "won't fit".
    ex, ey, ez = (float(v) for v in mesh.extents)
    fit = min(BED_X_MM / ex, BED_Y_MM / ey, BED_Z_MM / ez, 1.0)
    if fit < 1.0:
        mesh.apply_scale(fit)
        scale_note += ", shrunk to fit the build plate"

    # Seat on the bed and centre in XY so the slicer starts from a sane pose.
    bounds = mesh.bounds
    mesh.apply_translation([
        -(bounds[0][0] + bounds[1][0]) / 2.0,
        -(bounds[0][1] + bounds[1][1]) / 2.0,
        -bounds[0][2],
    ])

    mesh.export(str(stl_path))
    report = _mesh_report(mesh, scale_note, generated)
    log.info("[Printability] %s — %s mm, wall≈%s mm, parts=%s, warnings=%d",
             stl_path.name, report["dimensions_mm"], report["thickness_mm"],
             report["parts"], len(report["warnings"]))
    return report


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
        # One row per Anthropic call. Kept in the gallery DB so the running total
        # survives restarts — the log alone resets every time uvicorn reloads.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS llm_usage (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         TEXT    NOT NULL,
                label      TEXT    NOT NULL,
                model      TEXT    NOT NULL,
                tokens_in  INTEGER NOT NULL,
                tokens_out INTEGER NOT NULL,
                cached_in  INTEGER NOT NULL DEFAULT 0,
                cost_usd   REAL    NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts)")
        # Kiosk settings that have to outlive a restart. The selected printer
        # lives here rather than in memory because the kiosk is a machine that
        # gets power-cycled, and re-picking your printer every boot is not a
        # thing anyone would tolerate.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()


def db_get_setting(key: str) -> str | None:
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        conn.close()
    return row[0] if row else None


def db_set_setting(key: str, value: str | None) -> None:
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH))
        if value is None:
            conn.execute("DELETE FROM settings WHERE key=?", (key,))
        else:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        conn.commit()
        conn.close()


def db_insert_model(prompt: str, task_id: str) -> int:
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with _db_lock:
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
            """SELECT id, prompt, created_at, meshy_task_id, glb_path, stl_path
               FROM models
               ORDER BY created_at DESC"""
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]


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


def db_delete_model(row_id: int):
    """Delete a model row. Returns (row, orphaned_paths): the deleted row as a
    dict plus the file paths no surviving row still references, or (None, [])
    when the id doesn't exist. Engineer runs share output paths, so a file is
    only reported orphaned once its last referencing row is gone."""
    with _db_lock:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, prompt, created_at, meshy_task_id, glb_path, stl_path FROM models WHERE id=?",
            (row_id,),
        ).fetchone()
        if row is None:
            conn.close()
            return None, []
        conn.execute("DELETE FROM models WHERE id=?", (row_id,))
        conn.commit()
        orphaned = []
        for key in ("glb_path", "stl_path"):
            p = row[key]
            if not p:
                continue
            (refs,) = conn.execute(
                "SELECT COUNT(*) FROM models WHERE glb_path=? OR stl_path=?",
                (p, p),
            ).fetchone()
            if refs == 0 and p not in orphaned:
                orphaned.append(p)
        conn.close()
    return dict(row), orphaned


# Initialize DB at startup
init_db()


# ---------------------------------------------------------------------------
# Supabase SDK integration — all calls non-blocking via daemon threads
# ---------------------------------------------------------------------------

def get_supabase_client():
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        return None
    try:
        return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    except Exception as e:
        log.warning("[Supabase] Client init failed: %s", e)
        return None


def upload_to_supabase(file_path: Path, folder: str = "models") -> str | None:
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        return None
    client = get_supabase_client()
    if not client:
        return None
    try:
        filename = f"{folder}/{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file_path.name}"
        with open(file_path, "rb") as f:
            file_bytes = f.read()
        mime = "model/gltf-binary" if file_path.suffix == ".glb" else "application/octet-stream"
        client.storage.from_(SUPABASE_BUCKET).upload(
            path=filename,
            file=file_bytes,
            file_options={"content-type": mime, "upsert": "true"}
        )
        public_url = client.storage.from_(SUPABASE_BUCKET).get_public_url(filename)
        log.info("[Supabase] Uploaded %s → %s", file_path.name, public_url)
        return public_url
    except Exception as e:
        log.warning("[Supabase] Upload failed for %s: %s", file_path.name, e)
        return None


def save_model_to_supabase(
    prompt: str,
    task_id: str,
    glb_path: Path,
    stl_path: Path,
    glb_url: str | None,
    stl_url: str | None
) -> int | None:
    """Insert a model row in Supabase and return the Supabase-assigned id.

    Returns None on failure or when Supabase is not configured. Callers that
    log events against this model MUST use the returned Supabase id — the
    local SQLite id is a different sequence and violates events_model_id_fkey.
    """
    try:
        client = get_supabase_client()
        if not client:
            return None
        res = client.table("models").insert({
            "prompt": prompt,
            "meshy_task_id": task_id,
            "glb_path": str(glb_path),
            "stl_path": str(stl_path),
            "glb_url": glb_url,
            "stl_url": stl_url,
            "status": "complete",
        }).execute()
        supabase_id = res.data[0]["id"] if res.data else None
        log.info("[Supabase] Model record inserted (supabase id %s) for prompt: %s", supabase_id, prompt[:50])
        return supabase_id
    except Exception as e:
        log.warning("[Supabase] Model record insert failed: %s", e)
        return None


def log_supabase_event(
    event_type: str,
    model_id: int | None = None,
    message: str = "",
    metadata: dict | None = None
) -> None:
    try:
        client = get_supabase_client()
        if not client:
            return
        client.table("events").insert({
            "event_type": event_type,
            "model_id": model_id,
            "message": message,
            "metadata": metadata or {}
        }).execute()
        log.info("[Supabase] Event logged: %s", event_type)
    except Exception as e:
        log.warning("[Supabase] Event log failed: %s", e)


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
    "usb_used": None,     # None = not sliced yet; False = download-only run
    "sent_to_printer": False,
    "error": None,
    "printability": None,    # report from make_printable() for the current model
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
# ElevenLabs TTS — returns a status dict so the frontend can show real errors
# ---------------------------------------------------------------------------
def speak(text: str) -> dict:
    """
    Attempts to speak text via ElevenLabs. Returns a dict describing what happened
    so the frontend can show the real status instead of assuming success.
    """
    if not ELEVENLABS_API_KEY:
        print(f"[TTS] No ElevenLabs key — skipping: {text}")
        return {"ok": False, "reason": "no_key", "message": "Voice not configured"}
    try:
        r = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "text": text,
                # eleven_monolingual_v1 was deprecated and removed from the
                # free tier (ElevenLabs 401 model_deprecated_free_tier).
                "model_id": "eleven_flash_v2_5",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}
            },
            timeout=15
        )
        if r.status_code == 200:
            audio_path = "/tmp/conjure_speech.mp3"
            with open(audio_path, "wb") as f:
                f.write(r.content)
            os.system(f"(mpg123 -q {audio_path} 2>/dev/null || afplay {audio_path}) &")
            return {"ok": True, "reason": None, "message": None}
        elif r.status_code == 401:
            # ElevenLabs reports exhausted free credits as a 401 with a
            # quota_exceeded detail body — distinguish that from a bad key.
            body = r.text.lower()
            if "quota" in body or "credit" in body:
                print(f"[TTS] ElevenLabs 401 — out of credits: {r.text[:200]}")
                return {"ok": False, "reason": "quota_exceeded", "message": "Sorry, cannot use voice at this moment"}
            print(f"[TTS] ElevenLabs 401 — invalid or expired key")
            return {"ok": False, "reason": "invalid_key", "message": "Voice unavailable — invalid API key"}
        elif r.status_code == 429:
            print(f"[TTS] ElevenLabs 429 — rate limited or out of credits")
            return {"ok": False, "reason": "quota_exceeded", "message": "Sorry, cannot use voice at this moment"}
        else:
            print(f"[TTS] ElevenLabs error {r.status_code}: {r.text[:200]}")
            reason = "quota_exceeded" if "credit" in r.text.lower() or "quota" in r.text.lower() else "unknown"
            msg = "Sorry, cannot use voice at this moment" if reason == "quota_exceeded" else "Voice temporarily unavailable"
            return {"ok": False, "reason": reason, "message": msg}
    except Exception as e:
        print(f"[TTS] speak() failed: {e}")
        return {"ok": False, "reason": "network_error", "message": "Voice temporarily unavailable"}


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
    # macOS: find external removable USB volumes under /Volumes
    volumes = Path("/Volumes")
    if volumes.exists():
        import subprocess
        for vol in sorted(volumes.iterdir()):
            if not vol.is_dir() or vol.name in (".localized",):
                continue
            info = subprocess.run(
                ["diskutil", "info", str(vol)],
                capture_output=True, text=True
            )
            info_text = info.stdout
            is_removable = "Removable Media:          Yes" in info_text or "Removable Media:  Yes" in info_text
            is_usb = "Protocol:                 USB" in info_text or "Bus Protocol:             USB" in info_text
            is_internal = "Solid State:              Yes" in info_text and not is_usb
            if (is_removable or is_usb) and not is_internal:
                return str(vol)
    return None


def _usb_drive_info(path: str) -> dict:
    """One drive's display name and free space. Never raises — a drive that
    was yanked between enumeration and stat must not 500 the listing."""
    entry = {"path": path, "name": Path(path).name or path,
             "free_bytes": None, "free_mb": None, "writable": False}
    try:
        usage = shutil.disk_usage(path)
        entry["free_bytes"] = usage.free
        entry["free_mb"] = round(usage.free / (1024 * 1024), 1)
    except Exception:
        pass
    try:
        entry["writable"] = os.access(path, os.W_OK)
    except Exception:
        pass
    return entry


def _list_usb_drives() -> list:
    """Every mounted removable target, not just the first one.

    Deliberately a sibling of _find_usb() rather than a replacement. _find_usb()
    is what the Speak-mode slice pipeline calls to auto-copy, it works, and its
    first-match ordering is load-bearing there. Rewriting it to delegate here
    would put a working print path at risk to save a dozen lines, so the
    enumeration is duplicated on purpose.
    """
    seen, drives = set(), []

    def add(p: str) -> None:
        rp = str(Path(p))
        if rp in seen:
            return
        seen.add(rp)
        drives.append(_usb_drive_info(rp))

    for path in [USB_MOUNT_PATH, "/mnt/usb", "/media/usb", "/media/orangepi/usb"]:
        p = Path(path)
        try:
            if p.exists() and p.is_mount():
                add(str(p))
        except Exception:
            continue

    for parent in [Path("/media/orangepi"), Path("/media/pi"), Path("/media")]:
        try:
            if parent.exists() and parent.is_dir():
                for child in sorted(parent.iterdir()):
                    if child.is_mount():
                        add(str(child))
        except Exception:
            continue

    volumes = Path("/Volumes")
    if volumes.exists():
        import plistlib
        try:
            vols = sorted(volumes.iterdir())
        except Exception:
            vols = []
        for vol in vols:
            try:
                if not vol.is_dir() or vol.name in (".localized",):
                    continue
                # -plist, NOT the human-readable text. _find_usb() scrapes that
                # text with fixed-width literals like "Removable Media:          Yes",
                # and on this macOS diskutil actually prints eleven spaces and the
                # word "Removable" — so the match silently fails and a genuinely
                # mounted stick reads as absent. Measured 2026-08-03:
                #   actual   '   Removable Media:           Removable'
                #   expected 'Removable Media:          Yes'
                # The plist gives the same facts as real booleans, which cannot
                # drift with a formatting change.
                info = plistlib.loads(subprocess.run(
                    ["diskutil", "info", "-plist", str(vol)],
                    capture_output=True, timeout=10,
                ).stdout)
                if info.get("Internal") is True:
                    continue
                removable = (info.get("RemovableMedia")
                             or info.get("Ejectable")
                             or info.get("RemovableMediaOrExternalDevice"))
                if removable:
                    add(str(vol))
            except Exception:
                continue

    return drives


# ---------------------------------------------------------------------------
# InsForge storage upload — real bucket/object REST pattern
# ---------------------------------------------------------------------------
INSFORGE_BUCKET = os.getenv("INSFORGE_STORAGE_BUCKET_CONJURE", "conjure-models")


def upload_to_insforge_storage(file_path: Path, bucket: str = INSFORGE_BUCKET):
    if not INSFORGE_API_KEY:
        log.info("[InsForge] no key — skipping")
        return None
    try:
        with open(file_path, "rb") as f:
            r = requests.post(
                f"{INSFORGE_BASE_URL}/api/storage/buckets/{bucket}/objects",
                headers={"Authorization": f"Bearer {INSFORGE_API_KEY}"},
                files={"file": (file_path.name, f, "application/octet-stream")},
                timeout=30,
            )
        log.info("[InsForge] storage: HTTP %s", r.status_code)
        if r.status_code in (200, 201):
            key = (r.json() or {}).get("key", file_path.name)
            url = f"{INSFORGE_BASE_URL}/api/storage/buckets/{bucket}/objects/{key}"
            log.info("[InsForge] uploaded — %s", url)
            return url
        log.warning("[InsForge] upload failed (%s): %s", r.status_code, r.text[:100])
        return None
    except Exception as e:
        log.warning("[InsForge] error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Printability step shared by both pipelines
# ---------------------------------------------------------------------------
async def _apply_printability(active_path: Path, model_path: Path, generated: bool) -> dict:
    """Rewrites the active STL in place, mirrors it into the model dir, and
    records the report on pipeline_state. Never fatal — a failed check must not
    cost the user a model they just waited two minutes for."""
    try:
        report = await asyncio.to_thread(make_printable, active_path, generated)
        await asyncio.to_thread(shutil.copy2, str(active_path), str(model_path))
    except Exception as e:
        log.warning("[Printability] check failed: %s", e)
        report = {"printable": True, "warnings": [], "notes": [], "error": str(e)}
    pipeline_state["printability"] = report
    return report


def _printability_message(report: dict) -> str:
    if report.get("error"):
        return "Printability check skipped"
    dims = report.get("dimensions_mm") or []
    size = "×".join(str(d) for d in dims) + " mm" if dims else "sized"
    if report.get("warnings"):
        return f"{size} — {report['warnings'][0]}"
    return f"Printable — {size}, walls ≈{report.get('thickness_mm')} mm"


# ---------------------------------------------------------------------------
# Generation background task
# ---------------------------------------------------------------------------
async def run_generation(prompt: str, meshy_prompt_override: str | None = None) -> None:
    _clear_event_buffer()
    try:
        # ── Step 1: Create Meshy task ──────────────────────────────────────
        await push_event("create", "active", "Sending prompt to Meshy AI...", 2)
        threading.Thread(target=speak, args=("Got it. Conjuring your model now.",), daemon=True).start()
        # Written here rather than during the library search so the user isn't
        # held on a spinner for it; the regex cleaner covers LLM failures.
        meshy_prompt = meshy_prompt_override
        if not meshy_prompt:
            meshy_prompt = await asyncio.to_thread(llm_meshy_prompt, prompt) \
                           or build_meshy_prompt(prompt)
        # Last line of defence on the length cap. build_meshy_prompt() already
        # fits itself, but the LLM path and the precomputed override both reach
        # Meshy through here, and a 400 this late costs the user the whole wait.
        meshy_prompt = _fit_meshy_prompt([meshy_prompt])
        log.info("[Meshy] prompt (%d chars) → %s", len(meshy_prompt), meshy_prompt)
        r = await asyncio.to_thread(
            meshy_request,
            "POST",
            f"{MESHY_BASE}/v2/text-to-3d",
            headers=MESHY_HEADERS,
            json={
                "mode": "preview",
                "prompt": meshy_prompt,
                "art_style": "realistic",
                "negative_prompt": "low quality, low resolution, ugly, deformed, scene, environment, multiple objects, people, hands, text, labels",
            },
        )
        if r.status_code not in (200, 201, 202):
            raise Exception(f"Meshy create failed: HTTP {r.status_code} — {r.text[:200]}")

        task_id = r.json()["result"]
        pipeline_state["task_id"] = task_id
        log.info("[Meshy] task created — %s", task_id)

        # Insert DB row (SQLite) and create per-model directory
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
                meshy_request,
                "GET",
                f"{MESHY_BASE}/v2/text-to-3d/{task_id}",
                headers={"Authorization": f"Bearer {MESHY_API_KEY}"},
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
        log.info("[Gen] GLB saved — %d KB", glb_kb)
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
            log.info("[Gen] STL downloaded — %d KB", stl_kb)
            await push_event("download_stl", "complete", f"STL downloaded — {stl_kb} KB", 75)
        else:
            await push_event("download_stl", "active", "Converting GLB → STL via trimesh...", 68)

            def _convert_glb_to_stl() -> None:
                import trimesh
                mesh = trimesh.load(str(glb_model_path), force="mesh")
                # Meshy GLBs are Y-up; the slicer wants Z-up or the part lies down.
                _y_up_to_z_up(mesh)
                mesh.export(str(stl_model_path))

            await asyncio.to_thread(_convert_glb_to_stl)
            shutil.copy2(str(stl_model_path), str(stl_active_path))
            stl_kb = stl_model_path.stat().st_size // 1024
            log.info("[Gen] STL converted — %d KB", stl_kb)
            await push_event("download_stl", "complete", f"STL converted — {stl_kb} KB", 75)

        pipeline_state["stl_path"] = str(stl_active_path)

        # ── Step 4b: Make it printable ────────────────────────────────────
        await push_event("printability", "active", "Checking printability...", 76)
        report = await _apply_printability(stl_active_path, stl_model_path, generated=True)
        await push_event("printability",
                         "complete" if report.get("printable", True) else "warn",
                         _printability_message(report), 78)

        # Persist final paths to SQLite
        db_update_model_paths(row_id, str(glb_model_path), str(stl_model_path))

        # ── Step 5: Upload STL to InsForge storage ────────────────────────
        await push_event("insforge", "active", "Uploading to cloud...", 78)
        stl_size_mb = stl_active_path.stat().st_size / (1024 * 1024)
        if stl_size_mb > 20:
            log.info("[Gen] STL too large for cloud upload (%.1f MB) — skipping", stl_size_mb)
            await push_event("insforge", "complete", f"STL too large for cloud ({stl_size_mb:.0f} MB) — skipped", 88)
        else:
            try:
                cloud_url = await asyncio.to_thread(upload_to_insforge_storage, stl_active_path)
                if cloud_url:
                    await push_event("insforge", "complete", "Saved to cloud", 88)
                else:
                    await push_event("insforge", "complete", "Cloud upload skipped", 88)
            except Exception as e:
                await push_event("insforge", "complete", f"Cloud upload skipped: {e}", 88)

        # ── Supabase backup — runs in background thread, never blocks pipeline ──
        _prompt_for_backup  = prompt
        _task_id_for_backup = task_id
        _glb_path_for_backup = glb_model_path
        _stl_path_for_backup = stl_model_path
        _model_id_for_backup = row_id

        def _supabase_backup():
            try:
                glb_url_supa = upload_to_supabase(_glb_path_for_backup, folder="glb")
                stl_url_supa = upload_to_supabase(_stl_path_for_backup, folder="stl")

                supabase_model_id = save_model_to_supabase(
                    prompt=_prompt_for_backup,
                    task_id=_task_id_for_backup,
                    glb_path=_glb_path_for_backup,
                    stl_path=_stl_path_for_backup,
                    glb_url=glb_url_supa,
                    stl_url=stl_url_supa
                )

                # events.model_id has an FK to the Supabase models table, so it
                # must be the Supabase id (or NULL) — never the local SQLite id.
                log_supabase_event(
                    "model_generated",
                    model_id=supabase_model_id,
                    message=_prompt_for_backup,
                    metadata={
                        "task_id": _task_id_for_backup,
                        "local_model_id": _model_id_for_backup,
                        "glb_url": glb_url_supa,
                        "stl_url": stl_url_supa
                    }
                )
            except Exception as e:
                log.warning("[Supabase] Backup thread error: %s", e)

        threading.Thread(target=_supabase_backup, daemon=True).start()

        # ── Done ──────────────────────────────────────────────────────────
        pipeline_state["status"] = "model_ready"
        log.info("[Gen] complete — model_id=%s prompt=%r", row_id, prompt)
        await push_event("complete", "complete", "Model ready — tap PRINT THIS to slice", 100)
        threading.Thread(target=speak, args=("Your model is ready. Tap Print This to slice it.",), daemon=True).start()

    except Exception as exc:
        log.error("[Gen] pipeline error: %s", exc)
        pipeline_state["status"] = "error"
        pipeline_state["error"] = str(exc)
        await push_event("error", "error", str(exc), 0)
        threading.Thread(target=speak, args=("Something went wrong. Please try again.",), daemon=True).start()


# ---------------------------------------------------------------------------
# Library-model download background task — the "found it, print that" path.
# Emits the same SSE step ids as run_generation so the existing progress UI
# works unchanged.
# ---------------------------------------------------------------------------
async def run_library_download(prompt: str, source: str, model_id: str, name: str) -> None:
    _clear_event_buffer()
    try:
        await push_event("create", "active", f"Fetching design from {source.title()}...", 5)
        threading.Thread(target=speak, args=("Great choice. Fetching that design now.",), daemon=True).start()

        # ── Resolve an STL download URL ───────────────────────────────────
        if source == "printables":
            stls = await asyncio.to_thread(printables_stl_files, model_id)
            if not stls:
                raise Exception("No STL files listed for this design")
            # Multi-part models: grab the largest STL — usually the main body.
            main = max(stls, key=lambda f: f.get("fileSize") or 0)
            stl_url = await asyncio.to_thread(printables_download_url, str(main["id"]), model_id)
            file_name = main.get("name") or "model.stl"
        elif source == "thingiverse":
            stl_url, file_name = await asyncio.to_thread(thingiverse_stl_url, model_id)
        else:
            raise Exception(f"Unknown library source: {source}")

        if not stl_url:
            raise Exception("Could not get a download link for this design")
        await push_event("create", "complete", "Download link ready", 15)

        # ── Download STL ──────────────────────────────────────────────────
        await push_event("download_stl", "active", f"Downloading {file_name}...", 20)
        # Thingiverse's file CDN 403s a bare request even with a valid token —
        # it wants a browser-shaped one with a Referer.
        headers = ({"Authorization": f"Bearer {THINGIVERSE_APP_TOKEN}",
                    "User-Agent": _LIBRARY_UA["User-Agent"],
                    "Referer": "https://www.thingiverse.com/"}
                   if source == "thingiverse" else {"User-Agent": _LIBRARY_UA["User-Agent"]})
        stl_resp = await asyncio.to_thread(requests.get, stl_url, timeout=120, headers=headers)
        stl_resp.raise_for_status()
        if len(stl_resp.content) < 100:
            raise Exception("Downloaded file is empty")

        row_id = db_insert_model(prompt or name, f"{source}:{model_id}")
        pipeline_state["model_id"] = row_id
        model_dir = OUTPUT_DIR / "models" / str(row_id)
        model_dir.mkdir(parents=True, exist_ok=True)

        stl_model_path  = model_dir / "model.stl"
        stl_active_path = OUTPUT_DIR / "model.stl"
        async with aiofiles.open(stl_model_path, "wb") as f:
            await f.write(stl_resp.content)
        async with aiofiles.open(stl_active_path, "wb") as f:
            await f.write(stl_resp.content)
        pipeline_state["stl_path"] = str(stl_active_path)
        stl_kb = len(stl_resp.content) // 1024
        await push_event("download_stl", "complete", f"STL downloaded — {stl_kb} KB", 55)

        # ── Printability pass — community STLs are authored in real mm, so
        #    this repairs and seats the part without touching the scale. ──
        await push_event("printability", "active", "Checking printability...", 57)
        report = await _apply_printability(stl_active_path, stl_model_path, generated=False)
        await push_event("printability",
                         "complete" if report.get("printable", True) else "warn",
                         _printability_message(report), 58)

        # ── Convert STL → GLB so the 3D viewer + gallery previews work ────
        await push_event("download_glb", "active", "Preparing 3D preview...", 60)
        glb_model_path  = model_dir / "model.glb"
        glb_active_path = OUTPUT_DIR / "model.glb"
        glb_ok = False
        try:
            def _convert_stl_to_glb() -> None:
                import trimesh
                mesh = trimesh.load(str(stl_model_path), force="mesh")
                # Preview only — the STL on disk stays Z-up for the slicer.
                _z_up_to_y_up(mesh)
                mesh.export(str(glb_model_path))

            await asyncio.to_thread(_convert_stl_to_glb)
            shutil.copy2(str(glb_model_path), str(glb_active_path))
            pipeline_state["glb_path"] = str(glb_active_path)
            glb_ok = True
            await push_event("download_glb", "complete", "3D preview ready", 70)
        except Exception as e:
            # Viewer falls back to loading the STL directly — non-fatal. The
            # previous run's model.glb must go, or /api/model/glb would serve
            # a stale model to anything that reaches for it.
            log.warning("[Library] STL→GLB conversion failed: %s", e)
            glb_active_path.unlink(missing_ok=True)
            pipeline_state["glb_path"] = None
            await push_event("download_glb", "complete", "Preview uses STL directly", 70)

        db_update_model_paths(row_id, str(glb_model_path) if glb_ok else None, str(stl_model_path))

        # ── Upload to cloud (same as generated models) ────────────────────
        await push_event("insforge", "active", "Uploading to cloud...", 78)
        try:
            cloud_url = await asyncio.to_thread(upload_to_insforge_storage, stl_active_path)
            await push_event("insforge", "complete",
                             "Saved to cloud" if cloud_url else "Cloud upload skipped", 90)
        except Exception as e:
            await push_event("insforge", "complete", f"Cloud upload skipped: {e}", 90)

        # ── Done ──────────────────────────────────────────────────────────
        pipeline_state["status"] = "model_ready"
        log.info("[Library] complete — model_id=%s source=%s:%s (%s)", row_id, source, model_id, name)
        await push_event("complete", "complete", "Design ready — tap PRINT THIS to slice", 100)
        threading.Thread(target=speak, args=("Your design is ready. Tap Print This to slice it.",), daemon=True).start()

    except Exception as exc:
        log.error("[Library] download error: %s", exc)
        pipeline_state["status"] = "error"
        pipeline_state["error"] = str(exc)
        await push_event("error", "error", str(exc), 0)
        threading.Thread(target=speak, args=("Something went wrong. Please try again.",), daemon=True).start()


# ---------------------------------------------------------------------------
# Gcode provenance
# ---------------------------------------------------------------------------
# "Does model.gcode belong to the model on disk right now?" is the question
# worth asking before a part is dispatched, and neither obvious answer survives
# contact with this kiosk. An in-memory flag dies on restart while the gcode
# file does not, so a reboot leaves the previous session's part printable.
# Comparing timestamps fails too: choosing a model from the gallery copies its
# STL with shutil.copy2, which preserves the source mtime, so a freshly chosen
# model can look older than gcode sliced from something else entirely.
#
# Recording the digest of the STL that was actually sliced answers it directly.
# It sits beside the gcode rather than in memory, so it outlives a restart, and
# it needs no per-mode scoping — a Speak part and an Engineer part are checked
# the same way.
def _stl_digest(stl_path: Path) -> str | None:
    """SHA-256 of an STL, or None if it cannot be read."""
    try:
        h = hashlib.sha256()
        with open(stl_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError as e:
        log.warning("Could not fingerprint %s: %s", stl_path, e)
        return None


def _gcode_source_file(gcode_path: Path) -> Path:
    return gcode_path.with_name(gcode_path.name + ".source")


def record_gcode_source(stl_path: Path, gcode_path: Path) -> None:
    """Stamp a freshly sliced gcode with the STL it came from."""
    sidecar = _gcode_source_file(gcode_path)
    digest = _stl_digest(stl_path)
    try:
        if digest is None:
            sidecar.unlink(missing_ok=True)
        else:
            sidecar.write_text(digest)
    except OSError as e:
        log.warning("Could not record gcode source: %s", e)


def forget_gcode_source(gcode_path: Path) -> None:
    """Drop the stamp when its gcode goes, so it can never be read as
    describing whatever gcode appears next."""
    try:
        _gcode_source_file(gcode_path).unlink(missing_ok=True)
    except OSError as e:
        log.warning("Could not clear gcode source: %s", e)


def gcode_matches_model(gcode_path: Path, stl_path: Path) -> tuple[bool, str]:
    """(ok, reason) — is this gcode the one for the STL currently on disk?

    Unprovable counts as no. Gcode with no stamp was sliced before this check
    existed, or by a path that did not record one, and "probably fine" is not a
    standard worth dispatching a print on. The cost of being wrong is a wasted
    spool and a ruined part; the cost of being cautious is one re-slice.
    """
    if not gcode_path.exists() or gcode_path.stat().st_size == 0:
        return False, "No gcode yet — slice the model first"
    if not stl_path.exists():
        return False, "No model on disk to check the gcode against — slice again"

    sidecar = _gcode_source_file(gcode_path)
    if not sidecar.exists():
        return False, ("This gcode is not stamped with the model it came from — "
                       "slice again before printing")
    try:
        recorded = sidecar.read_text().strip()
    except OSError as e:
        return False, f"Could not read the gcode's source stamp: {e}"

    current = _stl_digest(stl_path)
    if current is None:
        return False, "Could not read the model to check it against the gcode"
    if recorded != current:
        return False, ("This gcode was sliced from a different model — "
                       "slice the current one before printing")
    return True, ""


# ---------------------------------------------------------------------------
# Slicing background task
# ---------------------------------------------------------------------------
async def run_slicing() -> None:
    _clear_event_buffer()
    try:
        stl_path       = OUTPUT_DIR / "model.stl"
        gcode_path     = OUTPUT_DIR / "model.gcode"
        orca_path      = ORCASLICER_PATH
        orca_profile   = Path(ORCASLICER_PROFILE)
        cura_path      = CURAENGINE_PATH
        cura_profile   = PROFILES_DIR / f"{PRINTER_PROFILE}_cura.def.json"
        cura_resources = CURA_RESOURCES_PATH

        # ── Step 1: Validate STL ───────────────────────────────────────────
        await push_event("load_stl", "active", "Loading model for slicing...", 5)
        if not stl_path.exists():
            raise Exception("model.stl not found — generate a model first")
        stl_kb = stl_path.stat().st_size // 1024
        log.info("[Slice] STL loaded — %d KB", stl_kb)
        await push_event("load_stl", "complete", f"STL loaded — {stl_kb} KB", 12)

        # Clear any stale gcode so the size checks below are meaningful
        if gcode_path.exists():
            gcode_path.unlink()
        forget_gcode_source(gcode_path)

        sliced = False

        # ── Step 2a: Slice with OrcaSlicer (Neptune 4 Plus system presets) ──
        # OrcaSlicer 2.4.x CLI needs a full flattened preset bundle (machine +
        # process + filament, inheritance pre-resolved), a per-model --scale
        # (a safety net — make_printable already fits the mesh), --arrange +
        # --ensure-on-bed to center/seat the part, and it always writes
        # plate_1.gcode into --outputdir. The old --slice/--export-gcode/--load
        # form is not valid in this version.
        if orca_path and Path(orca_path).exists():
            await push_event("slice", "active", "Slicing with OrcaSlicer...", 18)
            orca_machine  = PROFILES_DIR / "flat_machine_neptune4plus_04.json"
            orca_process  = PROFILES_DIR / "flat_process_0.20mm_standard_n4plus_04.json"
            orca_filament = PROFILES_DIR / "flat_filament_elegoo_pla_en4plus.json"
            orca_out      = OUTPUT_DIR / "orca_out"
            proc = None
            try:
                # Compute a fit-to-bed scale from OrcaSlicer's own --info readout.
                scale = 1.0
                try:
                    info_proc = await asyncio.create_subprocess_exec(
                        orca_path, "--info", str(stl_path),
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    )
                    info_out, _ = await asyncio.wait_for(info_proc.communicate(), timeout=60)
                    dims = {}
                    for line in info_out.decode("utf-8", "ignore").splitlines():
                        m = re.search(r"size_([xyz])\s*[:=]\s*([0-9.]+)", line)
                        if m:
                            dims[m.group(1)] = float(m.group(2))
                    if dims:
                        sx = dims.get("x") or 1.0
                        sy = dims.get("y") or 1.0
                        sz = dims.get("z") or 1.0
                        scale = min(BED_X_MM / sx, BED_Y_MM / sy, BED_Z_MM / sz, 1.0)
                except Exception as se:
                    log.warning("[Slice] OrcaSlicer --info failed (%s) — using scale 1.0", se)

                if orca_out.exists():
                    shutil.rmtree(orca_out, ignore_errors=True)
                orca_out.mkdir(parents=True, exist_ok=True)

                orca_cmd = [
                    orca_path,
                    "--load-settings", f"{orca_machine};{orca_process}",
                    "--load-filaments", str(orca_filament),
                    "--scale", f"{scale:.5f}",
                    "--ensure-on-bed",
                    "--arrange", "1",
                    "--slice", "0",
                    "--outputdir", str(orca_out),
                    str(stl_path),
                ]
                proc = await asyncio.create_subprocess_exec(
                    *orca_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(BASE_DIR),
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
                orca_gcode = orca_out / "plate_1.gcode"   # CLI always emits this fixed name
                if proc.returncode == 0 and orca_gcode.exists() and orca_gcode.stat().st_size > 5000:
                    shutil.move(str(orca_gcode), str(gcode_path))
                    gcode_mb = gcode_path.stat().st_size / (1024 * 1024)
                    log.info("[Slice] OrcaSlicer complete — %.1f MB (scale %.3f)", gcode_mb, scale)
                    await push_event("slice", "complete", f"OrcaSlicer: gcode ready — {gcode_mb:.1f} MB", 60)
                    sliced = True
                else:
                    tail = stderr.decode("utf-8", "ignore")[-300:] if stderr else ""
                    log.warning("[Slice] OrcaSlicer failed (rc=%s) — falling back to CuraEngine. %s", proc.returncode, tail)
                    await push_event("slice", "active", "OrcaSlicer failed — trying CuraEngine...", 22)
            except asyncio.TimeoutError:
                if proc is not None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                log.warning("[Slice] OrcaSlicer timed out — falling back to CuraEngine")
                await push_event("slice", "active", "OrcaSlicer timed out — trying CuraEngine...", 22)
            except Exception as e:
                log.warning("[Slice] OrcaSlicer exception: %s", e)
                await push_event("slice", "active", "OrcaSlicer error — trying CuraEngine...", 22)
        else:
            log.info("[Slice] OrcaSlicer not found at %s — using CuraEngine", orca_path)
            await push_event("slice", "active", "OrcaSlicer not found — using CuraEngine...", 18)

        # ── Step 2b: Fallback to CuraEngine ───────────────────────────────
        if not sliced:
            if gcode_path.exists():
                gcode_path.unlink()
            cura_cmd = [
                cura_path, "slice",
                "-j", str(cura_profile),
                "-e0",
                "-l", str(stl_path),
                "-o", str(gcode_path),
                "-s", "layer_height=0.2",
                "-s", "infill_sparse_density=15",
                "-s", "support_enable=false",
            ]
            proc = None
            try:
                cura_env = {**os.environ, "CURA_ENGINE_SEARCH_PATH": cura_resources}
                proc = await asyncio.create_subprocess_exec(
                    *cura_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(BASE_DIR),
                    env=cura_env,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
                if proc.returncode != 0:
                    err = stderr.decode("utf-8", errors="replace")[:500]
                    raise Exception(f"CuraEngine failed: {err}")
                if not gcode_path.exists() or gcode_path.stat().st_size < 5000:
                    raise Exception("Gcode output empty — slicing failed")
                gcode_mb = gcode_path.stat().st_size / (1024 * 1024)
                log.info("[Slice] CuraEngine complete — %.1f MB", gcode_mb)
                await push_event("slice", "complete", f"CuraEngine: gcode ready — {gcode_mb:.1f} MB", 60)
                sliced = True
            except asyncio.TimeoutError:
                if proc is not None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                raise Exception("Slicing timed out after 5 minutes")
            except FileNotFoundError:
                raise Exception(
                    "No slicer found — install OrcaSlicer (set ORCASLICER_PATH) "
                    "or CuraEngine (set CURAENGINE_PATH) in .env"
                )

        if not sliced:
            raise Exception("All slicers failed")

        gcode_mb = gcode_path.stat().st_size / (1024 * 1024)
        pipeline_state["gcode_path"] = str(gcode_path)
        record_gcode_source(stl_path, gcode_path)

        # slice_ok answers "does model.gcode belong to the model currently on
        # disk", and this is the other place that makes it true. Editing a
        # dimension clears the flag along with the gcode; without this the
        # re-slice that follows would leave it cleared and USB export would
        # refuse an export that is in fact correct.
        # Scoped to Engineer parts on purpose. Speak mode has never set this
        # flag and widening it here would change a path Feature 1 has no
        # business touching.
        with engineer_state["lock"]:
            if engineer_state.get("scad_script"):
                engineer_state["slice_ok"] = True

        threading.Thread(
            target=log_supabase_event,
            args=("model_sliced",),
            kwargs={"message": f"{gcode_mb:.1f}MB gcode generated"},
            daemon=True
        ).start()

        # ── Step 3: Detect USB (optional) ──────────────────────────────────
        # A missing USB stick used to fail the whole build, throwing away a
        # perfectly good slice. The gcode already exists on disk at this point,
        # so USB is just one of two ways to collect it — the browser download is
        # always available.
        await push_event("usb_check", "active", "Checking USB drive...", 65)
        usb_path = _find_usb()
        if usb_path is None:
            log.info("[Slice] no USB mounted — download-only")
            await push_event("usb_check", "complete", "No USB — download instead", 72)
        else:
            log.info("[Slice] USB found at %s", usb_path)
            await push_event("usb_check", "complete", f"USB found at {usb_path}", 72)

        # ── Step 4: Copy gcode to USB, when there is one ──────────────────
        if usb_path is not None:
            await push_event("copy_usb", "active", "Copying gcode to USB...", 78)
            dest = Path(usb_path) / "conjure_print.gcode"
            await asyncio.to_thread(shutil.copy2, str(gcode_path), str(dest))

            sync_proc = await asyncio.create_subprocess_exec(
                "sync",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await sync_proc.wait()
            await asyncio.sleep(1)
            log.info("[Slice] gcode written to USB — %.1f MB", gcode_mb)
            await push_event("copy_usb", "complete", f"conjure_print.gcode written — {gcode_mb:.1f} MB", 92)

            threading.Thread(
                target=log_supabase_event,
                args=("usb_exported",),
                kwargs={"message": "gcode exported to USB"},
                daemon=True
            ).start()
        else:
            await push_event("copy_usb", "complete", f"Gcode ready to download — {gcode_mb:.1f} MB", 92)

        # ── Done ──────────────────────────────────────────────────────────
        pipeline_state["status"] = "usb_ready"
        pipeline_state["usb_used"] = usb_path is not None
        if usb_path is not None:
            await push_event("usb_ready", "complete", "USB ready — safe to remove", 100)
            threading.Thread(target=speak, args=("Done. Remove the USB drive and insert it into your printer.",), daemon=True).start()
        else:
            await push_event("usb_ready", "complete", "Print file ready — tap download", 100)
            threading.Thread(target=speak, args=("Your print file is ready. Tap download to save it.",), daemon=True).start()

    except Exception as exc:
        log.error("[Slice] pipeline error: %s", exc)
        pipeline_state["status"] = "error"
        pipeline_state["error"] = str(exc)
        await push_event("error", "error", str(exc), 0)
        threading.Thread(target=speak, args=("Something went wrong. Please try again.",), daemon=True).start()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Conjure Kiosk", version="2.2.0")

# Self-hosted web fonts (Archivo + IBM Plex Mono) so the kiosk renders correct
# type with no network dependency at boot.
app.mount("/fonts", StaticFiles(directory=str(BASE_DIR / "fonts")), name="fonts")

# Self-hosted three.js r128 + GLTF/STL loaders — the 3D viewer must work offline.
app.mount("/vendor", StaticFiles(directory=str(BASE_DIR / "vendor")), name="vendor")


@app.on_event("startup")
async def startup_event() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "models").mkdir(parents=True, exist_ok=True)
    log.info("Conjure Kiosk started — output dir: %s", OUTPUT_DIR)
    log.info("Supabase: %s", "configured" if SUPABASE_URL and SUPABASE_ANON_KEY else "not configured")
    # Warm the local speech-to-text engine in the background so the model is
    # resident before the first voice prompt — without blocking startup on the
    # multi-second model load.
    threading.Thread(target=ensure_whisper_server, daemon=True).start()


@app.on_event("shutdown")
def shutdown_event() -> None:
    stop_whisper_server()


# Front door: the marketing scroll landing page. Its CTAs hand off to the working
# kiosk at /app?mode=speak|engineer. Also reachable at /landing.
@app.get("/", response_class=HTMLResponse)
@app.get("/landing", response_class=HTMLResponse)
def get_landing() -> HTMLResponse:
    return HTMLResponse(content=(BASE_DIR / "landing.html").read_text())


# The working kiosk app (describe -> generate -> view -> slice -> print).
@app.get("/app", response_class=HTMLResponse)
def get_app() -> HTMLResponse:
    return HTMLResponse(content=(BASE_DIR / "index.html").read_text())


@app.post("/api/transcribe")
def api_transcribe(audio: UploadFile = File(...)) -> JSONResponse:
    """On-device speech-to-text for the voice prompt. The kiosk browser records
    the mic (snap Chromium's cloud Web Speech API is unavailable — no Google API
    key) and POSTs the clip here. We normalise it to 16 kHz mono WAV and run it
    through whisper.cpp, preferring the resident whisper-server and falling back
    to whisper-cli. Returns {"text": ...}."""
    data = audio.file.read()
    if not data:
        raise HTTPException(400, "Empty audio upload")
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "clip")
        wav = os.path.join(td, "clip.wav")
        with open(src, "wb") as f:
            f.write(data)
        try:
            _ffmpeg_to_wav(src, wav)
        except Exception as e:
            log.warning("transcribe: ffmpeg decode failed: %s", e)
            raise HTTPException(422, "Could not decode audio")
        text = None
        if ensure_whisper_server():
            text = _transcribe_via_server(wav)
        if text is None:
            log.info("transcribe: whisper-server unavailable, using whisper-cli")
            try:
                text = _transcribe_via_cli(wav)
            except Exception as e:
                log.warning("transcribe: whisper-cli failed: %s", e)
                raise HTTPException(500, "Transcription failed")
    # whisper emits bracketed non-speech markers ([BLANK_AUDIO], [MUSIC]) and
    # timestamp-driven newlines; collapse them into a single clean line.
    text = re.sub(r"\[[^\]]*\]", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return JSONResponse({"text": text})


class GenerateRequest(BaseModel):
    prompt: str
    meshy_prompt: str | None = None  # precomputed by /api/search-models


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
        "usb_used":       None,
        "sent_to_printer": False,
    })
    background_tasks.add_task(run_generation, req.prompt.strip(), req.meshy_prompt)
    return JSONResponse({"status": "started", "prompt": req.prompt.strip()})


# ---------------------------------------------------------------------------
# Library search-before-generate flow
# ---------------------------------------------------------------------------
class SearchModelsRequest(BaseModel):
    prompt: str


@app.post("/api/search-models")
def api_search_models(req: SearchModelsRequest) -> JSONResponse:
    """Metaprompt the transcript, then search model libraries for an existing
    printable design. The kiosk offers matches before falling back to Meshy."""
    raw = req.prompt.strip()
    if not raw:
        raise HTTPException(400, "Prompt cannot be empty")

    # Only the search keywords are computed here. The full Meshy prompt is
    # deferred to /api/generate, where it hides inside the generation wait
    # instead of holding the user on a spinner.
    meta = llm_search_query(raw) or {}
    search_query = meta.get("search_query") or _fallback_search_query(raw)
    object_name  = meta.get("object_name") or search_query.title()

    try:
        results, sources, matched_query = search_with_fallback(search_query, limit=6)
    except Exception as e:
        log.warning("[Library] search failed: %s", e)
        results, sources, matched_query = [], [], search_query

    return JSONResponse({
        "results":       results,
        "sources":       sources,
        "search_query":  search_query,
        "matched_query": matched_query,
        "object_name":   object_name,
        "llm_used":      bool(meta),
        "fit_warning":   fitted_part_warning(raw),
    })


class LibraryModelRequest(BaseModel):
    source: str
    model_id: str
    name: str = ""
    prompt: str = ""


@app.post("/api/use-library-model")
async def api_use_library_model(req: LibraryModelRequest, background_tasks: BackgroundTasks) -> JSONResponse:
    if req.source not in ("printables", "thingiverse"):
        raise HTTPException(400, f"Unknown source: {req.source}")
    pipeline_state.update({
        "status":         "generating",
        "prompt":         req.prompt.strip() or req.name,
        "task_id":        f"{req.source}:{req.model_id}",
        "model_id":       None,
        "meshy_progress": 0,
        "error":          None,
        "stl_path":       None,
        "glb_path":       None,
        "gcode_path":     None,
        "usb_used":       None,
        "sent_to_printer": False,
    })
    background_tasks.add_task(run_library_download, req.prompt.strip(), req.source, req.model_id, req.name)
    return JSONResponse({"status": "started", "source": req.source, "model_id": req.model_id})


# Meshy task ids are hex-and-hyphen (uuid-shaped). Anything else does not belong
# in a URL we then fetch: task_id is interpolated into MESHY_BASE below, so an
# unconstrained value lets a caller steer the request path — an SSRF hole.
# Starlette already refuses `/` in a path param, so the host cannot be changed;
# this closes the remaining `..` traversal within Meshy.
_MESHY_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@app.get("/api/status/{task_id}")
def api_task_status(task_id: str) -> JSONResponse:
    if not MESHY_API_KEY:
        raise HTTPException(500, "MESHY_API_KEY not configured in .env")
    if not _MESHY_TASK_ID.match(task_id):
        raise HTTPException(400, "Invalid task id")
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


# ---------------------------------------------------------------------------
# Moonraker — sending the sliced gcode straight to the printer, so the kiosk
# doesn't depend on someone carrying a USB stick across the room. Fluidd is the
# web UI the user knows it by; Moonraker is the HTTP API it sits on.
# ---------------------------------------------------------------------------
def moonraker_base() -> str | None:
    """The printer's API root, or None when no printer has been configured."""
    if MOONRAKER_URL:
        return MOONRAKER_URL
    if PRINTER_IP:
        return f"http://{PRINTER_IP}:{MOONRAKER_PORT}"
    return None


def _moonraker_headers() -> dict:
    # Moonraker only demands a key when the kiosk's IP isn't in its
    # trusted_clients list; sending an empty one would be rejected outright.
    return {"X-Api-Key": MOONRAKER_API_KEY} if MOONRAKER_API_KEY else {}


def moonraker_request(method: str, path: str, **kw):
    base = moonraker_base()
    if not base:
        raise Exception("No printer configured — set PRINTER_IP in .env")
    kw.setdefault("timeout", MOONRAKER_TIMEOUT)
    headers = dict(kw.pop("headers", {}))
    headers.update(_moonraker_headers())
    return requests.request(method, f"{base}{path}", headers=headers, **kw)


# Klipper states that mean the printer cannot accept a new job right now.
_BUSY_STATES = {"printing", "paused"}


def moonraker_status() -> dict:
    """Whether the printer is reachable and what it's doing.

    Deliberately total — the UI polls this, and a printer that is merely off
    should render as "offline", never as a stack trace.
    """
    base = moonraker_base()
    if not base:
        return {"configured": False, "online": False,
                "message": "No printer set up yet — add PRINTER_IP to .env"}
    try:
        r = moonraker_request(
            "GET",
            "/printer/objects/query?print_stats&display_status&extruder&heater_bed",
            timeout=6,
        )
        if r.status_code == 401:
            return {"configured": True, "online": False,
                    "message": "Printer rejected the kiosk — set MOONRAKER_API_KEY in .env"}
        r.raise_for_status()
        s = (r.json().get("result") or {}).get("status") or {}
        stats = s.get("print_stats") or {}
        state = (stats.get("state") or "unknown").lower()
        # print_stats.progress only counts sliced-move progress; display_status
        # is what Fluidd shows, so prefer it and fall back.
        progress = (s.get("display_status") or {}).get("progress")
        if progress is None:
            progress = stats.get("progress") or 0.0
        return {
            "configured": True,
            "online":     True,
            "state":      state,
            "busy":       state in _BUSY_STATES,
            "filename":   stats.get("filename") or "",
            "progress":   round(float(progress) * 100, 1),
            "nozzle_c":   round(float((s.get("extruder") or {}).get("temperature") or 0), 1),
            "bed_c":      round(float((s.get("heater_bed") or {}).get("temperature") or 0), 1),
            "message":    "",
        }
    except (requests.Timeout, requests.ConnectionError):
        # Deliberately not surfacing the exception text: urllib3's version is a
        # paragraph of retry internals, and this renders on a kiosk touchscreen.
        # The real detail still goes to the log for whoever is debugging.
        log.warning("[Moonraker] %s unreachable", base, exc_info=True)
        return {"configured": True, "online": False,
                "message": f"No answer from the printer at {base} — "
                           "check it's powered on and on the same network"}
    except Exception as e:
        log.warning("[Moonraker] status query failed: %s", e)
        return {"configured": True, "online": False,
                "message": f"Can't read the printer at {base}: {type(e).__name__}"}


@app.get("/api/printer")
def api_printer_status() -> JSONResponse:
    return JSONResponse(moonraker_status())


@app.post("/api/print-now")
def api_print_now() -> JSONResponse:
    """Uploads the sliced gcode to the printer and starts it.

    That the file exists proves only that some slice once succeeded. A failed
    slice leaves the previous build's model.gcode on disk at full size, and a
    restart clears the in-memory record of what it was for while leaving the
    file itself untouched — so existence alone would happily dispatch the last
    session's part. The stamp written at slice time is what settles it.
    """
    gcode = OUTPUT_DIR / "model.gcode"
    fresh, why = gcode_matches_model(gcode, OUTPUT_DIR / "model.stl")
    if not fresh:
        raise HTTPException(400 if not gcode.exists() else 409, why)

    status = moonraker_status()
    if not status.get("configured"):
        raise HTTPException(400, status.get("message") or "No printer configured")
    if not status.get("online"):
        raise HTTPException(502, status.get("message") or "Printer is offline")
    # Refusing here rather than letting Moonraker queue it: silently interrupting
    # or stacking onto someone else's running print is the one failure mode of
    # this feature that wastes filament and ruins a part.
    if status.get("busy"):
        raise HTTPException(409,
            f"The printer is already {status.get('state')} "
            f"({status.get('progress')}% of {status.get('filename') or 'a job'}). "
            "Wait for it to finish, or stop it in Fluidd first.")

    name = _download_name("gcode")
    try:
        with open(gcode, "rb") as fh:
            r = moonraker_request(
                "POST", "/server/files/upload",
                # print=true makes Moonraker start the job as part of the upload.
                # Uploading and then calling /printer/print/start separately races
                # against Klipper's metadata scan and intermittently 404s.
                files={"file": (name, fh, "application/octet-stream")},
                data={"root": "gcodes", "print": "true"},
                timeout=max(MOONRAKER_TIMEOUT, 120),  # multi-MB over Wi-Fi
            )
        if r.status_code == 401:
            raise HTTPException(502, "Printer rejected the kiosk — set MOONRAKER_API_KEY in .env")
        if not r.ok:
            raise HTTPException(502, f"Printer refused the file (HTTP {r.status_code}): {r.text[:200]}")
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    except HTTPException:
        raise
    except requests.Timeout:
        raise HTTPException(504, "Upload to the printer timed out — check the Wi-Fi signal")
    except Exception as e:
        raise HTTPException(502, f"Couldn't send to the printer: {e}")

    started = str((body.get("result") or {}).get("print_started", "")).lower() == "true" \
        or (body.get("result") or {}).get("print_started") is True
    log.info("[Moonraker] uploaded %s (%.1f MB), print_started=%s",
             name, gcode.stat().st_size / 1e6, started)
    pipeline_state["sent_to_printer"] = True
    return JSONResponse({"status": "ok", "filename": name, "started": started,
                         "message": "Printing now — watch it on the printer or in Fluidd"
                                    if started else
                                    "Uploaded to the printer. Start it from Fluidd."})


def _download_name(ext: str) -> str:
    """A filename the user can recognise in their Downloads folder — 38 files
    all called model.stl is useless once more than one has been made."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(pipeline_state.get("prompt") or "model").lower()).strip("-")
    return f"conjure-{slug[:40] or 'model'}.{ext}"


# HEAD as well as GET. Starlette adds HEAD to a GET route automatically but
# FastAPI's APIRoute does not, so the UI's "is the file there yet?" pre-check was
# getting 405 and reporting "no print file yet" for a gcode sitting on disk.
@app.api_route("/api/download/stl", methods=["GET", "HEAD"])
def api_download_stl() -> FileResponse:
    p = OUTPUT_DIR / "model.stl"
    if not p.exists():
        raise HTTPException(404, "No STL yet — make a model first")
    return FileResponse(str(p), media_type="application/octet-stream",
                        filename=_download_name("stl"))


@app.api_route("/api/download/gcode", methods=["GET", "HEAD"])
def api_download_gcode() -> FileResponse:
    """The sliced, printer-ready file — the thing that used to only reach a USB."""
    p = OUTPUT_DIR / "model.gcode"
    if not p.exists():
        raise HTTPException(404, "No gcode yet — slice the model first")
    return FileResponse(str(p), media_type="text/plain",
                        filename=_download_name("gcode"))


@app.post("/api/slice")
async def api_slice(background_tasks: BackgroundTasks) -> JSONResponse:
    if not (OUTPUT_DIR / "model.stl").exists():
        raise HTTPException(400, "No STL file — generate a model first")

    # Last stop before slicing. Only a part that cannot be printed in ANY
    # orientation blocks; one that merely needs turning is allowed through with
    # the suggestion attached, because turning it is free and the slicer will
    # place it anyway. No printer selected, an unreadable STL or a broken check
    # never block -- they report why the check did not happen instead.
    fit = current_model_fit()
    if fit.get("blocking"):
        engineer_log(f"Slice blocked — {fit.get('message')}", "warning")
        return JSONResponse({"status": "blocked", "reason": "too_big",
                             "fit": fit, "error": fit.get("message")},
                            status_code=409)

    pipeline_state["status"] = "slicing"
    pipeline_state["error"]  = None
    background_tasks.add_task(run_slicing)
    return JSONResponse({"status": "started", "fit": fit})


@app.get("/api/usb/status")
def api_usb_status() -> JSONResponse:
    path = _find_usb()
    return JSONResponse({"mounted": path is not None, "path": path or USB_MOUNT_PATH})


@app.post("/api/copy-stl")
def api_copy_stl() -> JSONResponse:
    stl = OUTPUT_DIR / "model.stl"
    if not stl.exists():
        raise HTTPException(400, "No STL file — generate a model first")
    usb = _find_usb()
    if usb is None:
        raise HTTPException(503, "No USB drive found — insert a USB drive and try again")
    dest = Path(usb) / "conjure_model.stl"
    shutil.copy2(str(stl), str(dest))
    mb = dest.stat().st_size / (1024 * 1024)
    return JSONResponse({"status": "ok", "path": str(dest), "size_mb": round(mb, 2)})


@app.get("/api/usb/list")
def api_usb_list() -> JSONResponse:
    """Every detected removable target, plus whether the current gcode is
    actually exportable. The UI needs both to decide what to show, and one
    round trip avoids the two answers disagreeing."""
    drives = _list_usb_drives()
    gcode = OUTPUT_DIR / "model.gcode"
    with engineer_state["lock"]:
        slice_ok = engineer_state["slice_ok"]
    exists = gcode.exists()
    size = gcode.stat().st_size if exists else 0
    # Same helper the export endpoint refuses on, so the button the UI draws
    # and the answer it gets on click cannot disagree.
    exportable, why = gcode_matches_model(gcode, OUTPUT_DIR / "model.stl")
    return JSONResponse({
        "drives": drives,
        "count": len(drives),
        "gcode_exists": exists,
        "gcode_size_mb": round(size / (1024 * 1024), 2) if exists else 0,
        "slice_ok": slice_ok,
        "exportable": exportable,
        "blocked_reason": why,
    })


@app.post("/api/usb/export-gcode")
def api_usb_export_gcode(body: dict = None) -> JSONResponse:
    """Copy this build's gcode to a USB drive.

    Applies the same refusal the Moonraker path applies, now through the same
    helper rather than a parallel rule that happened to agree. Exporting gcode
    that belongs to a different model hands someone a USB stick holding the
    wrong object — the same defect as printing it, with the failure deferred
    until they walk to the printer.

    The old rule keyed on engineer_state["slice_ok"], which only Engineer
    builds ever set. That refused every Speak-mode export as "not sliced yet"
    however correct it was. Checking the gcode against the model on disk is
    the question that was meant all along, and it does not care which pipeline
    produced the part.
    """
    body = body or {}
    gcode = OUTPUT_DIR / "model.gcode"

    with engineer_state["lock"]:
        status = engineer_state["status"]

    if status == "RUNNING":
        return JSONResponse({"ok": False, "error": "A build is still running — "
                             "wait for it to finish before exporting"},
                            status_code=409)

    fresh, why = gcode_matches_model(gcode, OUTPUT_DIR / "model.stl")
    if not fresh:
        return JSONResponse({"ok": False, "error": why}, status_code=409)

    drives = _list_usb_drives()
    if not drives:
        return JSONResponse({"ok": False, "error": "No USB drive found — insert "
                             "a USB drive and try again"}, status_code=503)

    requested = (body.get("path") or "").strip()
    if requested:
        # Allowlist, not a path check. Accepting a caller-supplied destination
        # verbatim would let this endpoint write a multi-megabyte file anywhere
        # the kiosk user can write, so the target must be one we just detected.
        match = next((d for d in drives if d["path"] == str(Path(requested))), None)
        if match is None:
            return JSONResponse({"ok": False, "error": f"{requested!r} is not a "
                                 "detected USB drive",
                                 "drives": [d["path"] for d in drives]},
                                status_code=400)
    else:
        match = drives[0]

    # Checked up front rather than left to the copy. A read-only volume raises
    # EROFS (errno 30), which is a plain OSError and NOT PermissionError
    # (errno 13) — so catching PermissionError alone let a mounted read-only
    # stick fall through to the generic handler and surface as a 500 with a raw
    # errno string. It is a 403: the request is fine, the destination is not.
    if not match.get("writable"):
        return JSONResponse({"ok": False, "error": f"{match['name']} is "
                             "read-only — unlock the drive or use another one"},
                            status_code=403)

    size = gcode.stat().st_size
    free = match.get("free_bytes")
    if free is not None and free < size:
        return JSONResponse({"ok": False, "error": "Not enough space on "
                             f"{match['name']} — need {size/(1024*1024):.1f} MB, "
                             f"{free/(1024*1024):.1f} MB free",
                             "free_mb": match.get("free_mb")}, status_code=507)

    dest = Path(match["path"]) / "conjure_print.gcode"
    try:
        shutil.copy2(str(gcode), str(dest))
        written = dest.stat().st_size
        if written != size:
            return JSONResponse({"ok": False, "error": "Copy incomplete — wrote "
                                 f"{written} of {size} bytes"}, status_code=500)
        # Without this the bytes can still be in the page cache when the user
        # pulls the stick out, which is the classic way a "successful" export
        # arrives at the printer truncated.
        try:
            subprocess.run(["sync"], timeout=30, check=False)
        except Exception as se:
            log.warning("[USB] sync failed: %s", se)
    except PermissionError:
        return JSONResponse({"ok": False, "error": f"{match['name']} is not "
                             "writable (read-only or locked)"}, status_code=403)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"Copy failed: {e}"},
                            status_code=500)

    after = _usb_drive_info(match["path"])
    log.info("[USB] exported gcode -> %s (%.1f MB)", dest, size / (1024 * 1024))
    return JSONResponse({
        "ok": True, "path": str(dest), "drive": match["name"],
        "size_mb": round(size / (1024 * 1024), 2),
        "free_mb_after": after.get("free_mb"),
        "message": f"Saved conjure_print.gcode to {match['name']} — safe to remove",
    })


@app.get("/api/state")
def api_state() -> JSONResponse:
    return JSONResponse({k: v for k, v in pipeline_state.items()})


def llm_usage_summary() -> dict:
    """Running Anthropic spend, read straight from the ledger."""
    empty = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
    try:
        with _db_lock:
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row

            def agg(where: str = "", args: tuple = ()) -> dict:
                r = conn.execute(
                    "SELECT COUNT(*) c, COALESCE(SUM(tokens_in),0) ti, "
                    "COALESCE(SUM(tokens_out),0) to_, COALESCE(SUM(cost_usd),0) cost "
                    f"FROM llm_usage {where}", args).fetchone()
                return {"calls": r["c"], "tokens_in": r["ti"],
                        "tokens_out": r["to_"], "cost_usd": round(r["cost"], 6)}

            total = agg()
            today = agg("WHERE ts >= ?", (datetime.now().strftime("%Y-%m-%d"),))
            by_label = {
                row["label"]: {"calls": row["c"], "cost_usd": round(row["cost"], 6),
                               "avg_usd": round(row["cost"] / row["c"], 6) if row["c"] else 0.0}
                for row in conn.execute(
                    "SELECT label, COUNT(*) c, SUM(cost_usd) cost FROM llm_usage GROUP BY label")
            }
            conn.close()
    except Exception as e:
        log.warning("[Usage] summary failed: %s", e)
        return {"total": empty, "today": empty, "by_label": {}, "model": ANTHROPIC_MODEL}

    return {
        "model":    ANTHROPIC_MODEL,
        "priced":   ANTHROPIC_MODEL in MODEL_PRICING,
        "total":    total,
        "today":    today,
        "by_label": by_label,
    }


@app.get("/api/usage")
def api_usage(limit: int = 20) -> JSONResponse:
    """Spend summary plus the most recent calls, for eyeballing what costs what."""
    out = llm_usage_summary()
    try:
        with _db_lock:
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            out["recent"] = [dict(r) for r in conn.execute(
                "SELECT ts, label, model, tokens_in, tokens_out, cost_usd "
                "FROM llm_usage ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),))]
            conn.close()
    except Exception:
        out["recent"] = []
    return JSONResponse(out)


@app.get("/api/health")
def health_check() -> JSONResponse:
    disk = shutil.disk_usage(str(BASE_DIR))

    try:
        conn = sqlite3.connect(str(DB_PATH))
        model_count = conn.execute("SELECT COUNT(*) FROM models").fetchone()[0]
        conn.close()
        db_ok = True
    except Exception:
        model_count = 0
        db_ok = False

    try:
        sb_ok = get_supabase_client() is not None if (SUPABASE_URL and SUPABASE_ANON_KEY) else False
    except Exception:
        sb_ok = False

    return JSONResponse({
        "status": "ok",
        "db": db_ok,
        "model_count": model_count,
        "disk_free_gb": round(disk.free / (1024 ** 3), 2),
        "api_keys": {
            "meshy":       bool(MESHY_API_KEY),
            "elevenlabs":  bool(ELEVENLABS_API_KEY),
            "insforge":    bool(INSFORGE_API_KEY),
            "anthropic":   bool(ANTHROPIC_API_KEY),
            "thingiverse": bool(THINGIVERSE_APP_TOKEN),
        },
        "supabase_configured": bool(SUPABASE_URL and SUPABASE_ANON_KEY),
        "supabase_connected":  sb_ok,
        "llm_usage":  llm_usage_summary(),
        "printer":    moonraker_status(),
        "output_dir": str(OUTPUT_DIR),
        "db_path":    str(DB_PATH),
    })


@app.get("/api/supabase/status")
def supabase_status() -> JSONResponse:
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        return JSONResponse({
            "connected": False,
            "reason": "SUPABASE_URL or SUPABASE_ANON_KEY not set in .env"
        })
    try:
        client = get_supabase_client()
        if not client:
            return JSONResponse({"connected": False, "reason": "Client init failed"})

        # anon key cannot call list_buckets() — probe the bucket directly instead
        bucket_ok = False
        bucket_reason = None
        try:
            client.storage.from_(SUPABASE_BUCKET).list("", {"limit": 1})
            bucket_ok = True
        except Exception as be:
            bucket_reason = str(be)[:120]

        model_count = None
        try:
            result = client.table("models").select("id", count="exact").execute()
            model_count = result.count
        except Exception:
            pass

        return JSONResponse({
            "connected": True,
            "bucket": SUPABASE_BUCKET,
            "bucket_exists": bucket_ok,
            "bucket_note": None if bucket_ok else bucket_reason,
            "supabase_url": SUPABASE_URL,
            "model_count_in_supabase": model_count,
        })
    except Exception as e:
        return JSONResponse({"connected": False, "reason": str(e)})


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
        "usb_used":       None,
        "sent_to_printer": False,
        "error":          None,
        "printability":   None,
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

# In-memory voice switch. Seeded from VOICE_ENABLED so the env var still sets
# the boot default, but the kiosk toggle wins after that.
_voice_setting = {"enabled": os.getenv("VOICE_ENABLED", "true").lower() == "true"}

# VOICE_ENABLED=false is a hard kill switch: while set, no API call — not
# even a stale cached page hitting /api/settings/voice — can re-enable sound.
VOICE_HARD_DISABLED = os.getenv("VOICE_ENABLED", "true").lower() != "true"


@app.get("/api/settings/voice")
def get_voice_setting() -> JSONResponse:
    return JSONResponse({"enabled": _voice_setting["enabled"]})


@app.post("/api/settings/voice")
async def set_voice_setting(body: dict) -> JSONResponse:
    enabled = bool(body.get("enabled", True)) and not VOICE_HARD_DISABLED
    _voice_setting["enabled"] = enabled
    return JSONResponse({"ok": True, "enabled": enabled})


class SpeakRequest(BaseModel):
    text: str


@app.post("/api/speak")
def api_speak(req: SpeakRequest) -> JSONResponse:
    # Sync (non-async) on purpose: speak() blocks on the ElevenLabs HTTP call,
    # so FastAPI must run this in its threadpool instead of the event loop —
    # otherwise every TTS call would freeze /events (SSE) for up to 15s.
    if not req.text.strip():
        return JSONResponse({"ok": False, "reason": "empty_text", "message": None})

    if VOICE_HARD_DISABLED or not _voice_setting["enabled"]:
        return JSONResponse({"ok": False, "reason": "disabled", "message": None})

    result = speak(req.text.strip())
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Gallery endpoints
# ---------------------------------------------------------------------------

@app.get("/api/models")
def list_models() -> JSONResponse:
    try:
        models = db_list_models()
        # Report what actually exists on disk so the library never dead-ends on a
        # missing/invalid asset. A real preview needs a per-model .glb that exists;
        # a real STL is a per-model .stl (NOT the shared output/model.stl scratch).
        for m in models:
            gp = (m.get("glb_path") or "")
            sp = (m.get("stl_path") or "")
            m["glb_available"] = bool(gp) and gp.lower().endswith(".glb") and Path(gp).exists()
            sp_norm = sp.replace("\\", "/")
            m["stl_available"] = (
                bool(sp) and sp.lower().endswith(".stl")
                and "/models/" in sp_norm and Path(sp).exists()
            )
        return JSONResponse({"models": models, "count": len(models)})
    except Exception as e:
        return JSONResponse(
            {"models": [], "count": 0, "error": str(e)},
            status_code=500
        )


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
def select_model(model_id: int) -> JSONResponse:
    try:
        with _db_lock:
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM models WHERE id = ?", (model_id,)
            ).fetchone()
            conn.close()

        if not row:
            return JSONResponse(
                {"ok": False, "error": f"Model {model_id} not found"},
                status_code=404
            )

        model = dict(row)

        if model.get("glb_path") and Path(model["glb_path"]).exists():
            shutil.copy2(model["glb_path"], OUTPUT_DIR / "model.glb")
        else:
            return JSONResponse(
                {"ok": False, "error": "GLB file not found for this model"},
                status_code=404
            )

        if model.get("stl_path") and Path(model["stl_path"]).exists():
            shutil.copy2(model["stl_path"], OUTPUT_DIR / "model.stl")

        pipeline_state["status"] = "model_ready"
        pipeline_state["active_model_id"] = model_id

        # model_id here is the local SQLite id — not valid against the Supabase
        # models FK, so it rides in metadata and the FK column stays NULL.
        threading.Thread(
            target=log_supabase_event,
            args=("model_selected",),
            kwargs={"message": model.get("prompt", ""), "metadata": {"local_model_id": model_id}},
            daemon=True
        ).start()

        return JSONResponse({
            "ok": True,
            "model_id": model_id,
            "prompt": model.get("prompt")
        })

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.delete("/api/models/{model_id}")
def delete_model(model_id: int) -> JSONResponse:
    try:
        row, orphaned_paths = db_delete_model(model_id)
        if row is None:
            return JSONResponse(
                {"ok": False, "error": f"Model {model_id} not found"},
                status_code=404
            )

        # Remove the model's files — but never the live working copies in
        # OUTPUT_DIR (model.glb / model.stl) that the viewer and slicer use.
        working_copies = {
            (OUTPUT_DIR / "model.glb").resolve(),
            (OUTPUT_DIR / "model.stl").resolve(),
        }
        deleted_files = []
        for p in orphaned_paths:
            path = Path(p)
            try:
                if path.exists() and path.resolve() not in working_copies:
                    path.unlink()
                    deleted_files.append(str(path))
            except Exception as e:
                log.warning("[Delete] Could not remove %s: %s", p, e)

        if pipeline_state.get("active_model_id") == model_id:
            pipeline_state["active_model_id"] = None

        # Local SQLite id — not valid against the Supabase models FK, so it
        # rides in metadata (same pattern as model_selected).
        threading.Thread(
            target=log_supabase_event,
            args=("model_deleted",),
            kwargs={"message": row.get("prompt", ""), "metadata": {"local_model_id": model_id}},
            daemon=True
        ).start()

        return JSONResponse({
            "ok": True,
            "model_id": model_id,
            "deleted_files": deleted_files
        })

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


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


# ---------------------------------------------------------------------------
# ENGINEER MODE — research-driven parametric CAD pipeline
# ---------------------------------------------------------------------------
engineer_state = {
    "status": "IDLE",
    "current_step": 0,
    "current_step_name": "",
    "logs": [],
    "scad_script": None,
    # The script as generated, kept beside the working one so "Reset to
    # original" restores the model rather than the last edit to it.
    "scad_original": None,
    "specs": None,
    "design_review": None,
    # slice_ok was a local in run_engineer_pipeline, so nothing outside that
    # function could tell a real slice from a stale model.gcode left behind by
    # an earlier build. The USB export endpoint needs exactly that distinction
    # to apply the same refusal the printer path already applies, so the result
    # is recorded here. None = no build has sliced yet this process.
    "slice_ok": None,
    "review": None,
    "lock": threading.Lock(),
}

ENGINEER_STEP_NAMES = [
    "Design Brief",
    "CAD Generation",
    "OpenSCAD Render",
    "OrcaSlicer Slice",
    "Send to Printer",
    "Design Review",
]

def engineer_log(message: str, level: str = "info") -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = {"time": timestamp, "message": message, "level": level}
    with engineer_state["lock"]:
        engineer_state["logs"].append(entry)
    print(f"[ENGINEER][{timestamp}] [{level.upper()}] {message}", flush=True)


def engineer_set_step(n: int) -> None:
    with engineer_state["lock"]:
        engineer_state["current_step"] = n
        engineer_state["current_step_name"] = ENGINEER_STEP_NAMES[n] if n < len(ENGINEER_STEP_NAMES) else ""


def engineer_set_status(s: str) -> None:
    with engineer_state["lock"]:
        engineer_state["status"] = s


# PLA material properties. These were previously "discovered" by a web search
# that shipped these exact numbers as its fallback anyway — so they were always
# constants wearing a research costume. Named here so they can be corrected.
PLA_TENSILE_MPA      = 37.0
DESIGN_SAFETY_FACTOR = 2.5

_BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "summary":              {"type": "string"},
        "primary_dimension_mm": {"type": "number"},
        "wall_mm":              {"type": "number"},
        "clearance_mm":         {"type": "number"},
    },
    "required": ["summary", "primary_dimension_mm", "wall_mm", "clearance_mm"],
    "additionalProperties": False,
}

_BRIEF_SYSTEM = """You size parts for a 0.4mm-nozzle FDM printer before they are modelled.

Given what the user wants, return:
 - summary: one sentence naming the part and how it is meant to work.
 - primary_dimension_mm: the part's main overall dimension in mm. Use a real
   figure when the object has one (an M8 nut is 13mm across flats; a 2-inch pipe
   is 60.3mm OD); otherwise choose a sensible size for a desk object, 20-200mm.
 - wall_mm: structural wall thickness, at least 1.2 (three 0.4mm perimeters).
 - clearance_mm: gap between parts that must move independently when printed in
   place. 0.3-0.5 is the usable band on FDM; use 0 if nothing has to move."""

_SCAD_SYSTEM = """You are a parametric CAD engineer. Output ONLY raw OpenSCAD code:
no markdown, no backticks, no prose, no explanation.

Open with a Customizer parameter block, then modules, then one top-level call.

That block is a contract, not decoration: the kiosk parses it and builds the
dimension-editing UI out of it, so anything it cannot read is a dimension the
user cannot adjust. OpenSCAD only treats a variable as a parameter when it sits
before the first "{" in the file and holds a plain literal — so:
 - Every user-meaningful dimension is a top-level variable assigned a bare
   number. No expressions, no arithmetic, no references to other variables.
 - One descriptive comment directly above each, starting at column 0, naming
   what it controls and its unit: "// Overall length in mm".
 - A range annotation trailing each numeric variable — // [min:max], or
   // [min:step:max] where a coarse step reads better. Bracket the sensible
   design envelope, not the physical extremes.
 - Group with /* [Group Name] */ headers: Dimensions, Holes, Fit, and so on.
 - Everything the user must not touch — $fn, derived values, internal
   constants — goes after a /* [Hidden] */ header or inside a module.
 - Every declared parameter must actually drive geometry. An orphaned variable
   is a slider that moves nothing.

Shape of the block:

/* [Dimensions] */
// Overall length in mm
length = 40; // [20:100]
// Wall thickness in mm
wall_thickness = 3; // [1:0.5:10]

/* [Holes] */
// Mounting hole diameter in mm
hole_diameter = 4; // [2:0.5:12]

/* [Hidden] */
$fn = 64;

Rules:
 - Every dimension in millimetres.
 - $fn on every curved primitive (48-64) so the mesh is smooth but not enormous.
 - The result must be manifold and printable on FDM with no supports: a flat
   base on the bed, no unsupported overhang past ~45 degrees, no wall thinner
   than the given wall_mm.
 - For parts printed pre-assembled, separate every moving surface by exactly the
   given clearance_mm — do not let solids touch or intersect, or it fuses solid.
 - Prefer difference()/union()/hull() over huge polyhedron() vertex lists."""

_REVIEW_SYSTEM = """You review OpenSCAD for a 0.4mm-nozzle FDM printer. In 2-3 sentences
name the single biggest printability or structural risk and the concrete fix.
No preamble, no restating the design. If it looks sound, say so briefly."""


def design_brief(intent: str) -> dict:
    """Sizes the part before any geometry exists.

    Replaces the old You.com/Tavily "research" pair, which regex-scraped one
    number out of a search snippet and otherwise returned its own defaults.
    """
    out = _llm_json(_BRIEF_SYSTEM, intent, _BRIEF_SCHEMA, "DesignBrief")
    if not out:
        engineer_log("Design brief unavailable — using generic defaults", "warning")
        out = {"summary": intent, "primary_dimension_mm": 60.0,
               "wall_mm": 3.0, "clearance_mm": 0.4}
    else:
        engineer_log(f"Brief: {out['summary']}", "success")
    return {
        "od_mm":         float(out["primary_dimension_mm"]),
        "inner_r":       round(float(out["primary_dimension_mm"]) / 2 + 0.4, 4),
        "wall_mm":       max(float(out["wall_mm"]), MIN_WALL_MM),
        "clearance_mm":  float(out["clearance_mm"]),
        "tensile_mpa":   PLA_TENSILE_MPA,
        "safety_factor": DESIGN_SAFETY_FACTOR,
        "summary":       out["summary"],
    }


def generate_scad(intent: str, specs: dict) -> str | None:
    """Claude writes the OpenSCAD. Returns None when it can't — deliberately.

    The old version fell back to a hardcoded pipe clamp, so asking for a
    planetary gearbox with no API key produced a pipe clamp and reported
    success. Returning None lets the pipeline fail honestly instead.
    """
    user = (
        f"Part: {specs['summary']}\n"
        f"Main dimension: {specs['od_mm']}mm\n"
        f"Wall thickness: {specs['wall_mm']}mm\n"
        f"Moving-part clearance: {specs['clearance_mm']}mm\n"
        f"Material: PLA, {specs['tensile_mpa']}MPa tensile, "
        f"{specs['safety_factor']}x safety factor\n\n"
        f"Original request: {intent}\n\n"
        "Write the OpenSCAD script."
    )
    raw = _llm_text(_SCAD_SYSTEM, user, "EngineerCAD", max_tokens=4000)
    if not raw:
        return None
    # Strip a markdown fence if one slips through despite the instruction.
    cleaned = re.sub(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$", "", raw).strip()
    if not any(k in cleaned for k in ("cylinder", "cube", "difference", "union", "sphere", "polygon")):
        engineer_log("CAD output contained no OpenSCAD geometry — rejected", "error")
        return None
    engineer_log(f"CAD generated: {len(cleaned.splitlines())} lines", "success")
    return cleaned


# ── OpenSCAD Customizer parameter block ──────────────────────────────────────
# Parsed to the rules in the OpenSCAD manual rather than to a convenient
# approximation of them, because the LLM writes to those rules and OpenSCAD
# itself reads by them. A variable is a parameter only when it is assigned in
# the main file, sits before the first "{" syntax element, and holds a plain
# literal — number, bool, string, or a list of up to four numbers. Expressions
# are skipped, not evaluated. Anything under /* [Hidden] */ is excluded.
#
# Every failure here degrades to "no parameters". A script without a block is
# an ordinary script, not an error: Speak mode has no SCAD at all, and older
# Engineer builds predate the prompt that emits annotations.

_SCAD_GROUP_RE  = re.compile(r"^\s*/\*\s*\[([^\]]+)\]\s*\*/\s*$")
_SCAD_DESC_RE   = re.compile(r"^\s*//\s?(.*)$")
_SCAD_ASSIGN_RE = re.compile(
    r"^\s*(\$?[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*;\s*(?://\s*(.*?)\s*)?$"
)
_SCAD_NUM_RE    = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")


def _scad_customizer_head(src: str) -> str:
    """Source up to the first "{" that is real code — the customizer cutoff.

    Tracked through strings and comments so a brace inside either does not end
    the block early. That is the actual rule; "before the first module" is the
    folklore version of it and gets scripts wrong.
    """
    i, n = 0, len(src)
    line_comment = block_comment = in_string = False
    while i < n:
        c   = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if line_comment:
            if c == "\n":
                line_comment = False
        elif block_comment:
            if c == "*" and nxt == "/":
                block_comment = False
                i += 1
        elif in_string:
            if c == "\\":
                i += 1
            elif c == '"':
                in_string = False
        elif c == "/" and nxt == "/":
            line_comment = True
            i += 1
        elif c == "/" and nxt == "*":
            block_comment = True
            i += 1
        elif c == '"':
            in_string = True
        elif c == "{":
            return src[:i]
        i += 1
    return src


def _scad_literal(raw: str):
    """(value, type) for a literal, or (None, None) for anything else."""
    t = raw.strip()
    if t in ("true", "false"):
        return t == "true", "bool"
    if _SCAD_NUM_RE.match(t):
        v = float(t)
        keep_int = v.is_integer() and "." not in t and "e" not in t.lower()
        return (int(v) if keep_int else v), "number"
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"' and '"' not in t[1:-1]:
        return t[1:-1], "string"
    if t.startswith("[") and t.endswith("]"):
        items = [p.strip() for p in t[1:-1].split(",") if p.strip()]
        if 1 <= len(items) <= 4 and all(_SCAD_NUM_RE.match(p) for p in items):
            return [float(p) for p in items], "vector"
    return None, None


def _scad_annotation(note: str, ptype: str) -> dict:
    """Constraints from a trailing comment: [max], [min:max], [min:step:max],
    or a [a, b, c] / [val:Label, ...] option list.

    A comment it cannot read yields no constraints, which is still a usable
    control — a bare number box beats refusing to show the parameter.
    """
    out = {"min": None, "max": None, "step": None,
           "options": None, "max_length": None}
    if not note:
        return out
    m = re.search(r"\[([^\]]*)\]", note)
    if not m:
        # `String = "hello"; //8` is the documented text-box length form.
        bare = note.strip()
        if ptype == "string" and _SCAD_NUM_RE.match(bare):
            out["max_length"] = int(float(bare))
        return out

    body = m.group(1).strip()
    if not body:
        return out

    # A comma means an option list even when the items also carry ":" labels,
    # so this test has to come before the range test.
    if "," in body:
        options = []
        for item in body.split(","):
            item = item.strip()
            if not item:
                continue
            val, _, label = item.partition(":")
            val, label = val.strip(), label.strip()
            pv = float(val) if _SCAD_NUM_RE.match(val) else val.strip('"')
            options.append({"value": pv, "label": label or str(pv)})
        out["options"] = options or None
        return out

    parts = [p.strip() for p in body.split(":")]
    if all(_SCAD_NUM_RE.match(p) for p in parts):
        nums = [float(p) for p in parts]
        if len(nums) == 1:
            out["min"], out["max"] = 0.0, nums[0]
        elif len(nums) == 2:
            out["min"], out["max"] = nums
        elif len(nums) == 3:
            out["min"], out["step"], out["max"] = nums
    elif ptype == "string" and ":" not in body:
        # A lone string is a one-item option list. A lone *malformed range*
        # like [oops:bad] is not — that falls through to no constraints, since
        # a plain number box beats a dropdown with one nonsense entry in it.
        out["options"] = [{"value": body.strip('"'), "label": body.strip('"')}]
    return out


def _scad_walk_head(src: str) -> list[dict]:
    """Every customizer-eligible assignment in the head, in source order.

    One walk, two consumers: the parameter list the UI renders from, and the
    rewriter that puts edited values back. Splitting them would let the two
    disagree about what counts as a parameter, and a dimension you can edit but
    cannot save is worse than one you were never shown.

    A name assigned more than once yields only its *last* assignment. OpenSCAD
    binds one value per name for the whole file and the last one wins, so the
    earlier assignments are dead code — they cannot change the model no matter
    what is written to them. Showing them would put two rows on screen for one
    dimension, drifting to different numbers as soon as either is edited, and
    rewriting them would edit lines whose effect the user can never see.
    """
    found: dict[str, dict] = {}
    group, hidden, desc = "Parameters", False, None
    for idx, line in enumerate(_scad_customizer_head(src).splitlines()):
        g = _SCAD_GROUP_RE.match(line)
        if g:
            name   = g.group(1).strip()
            hidden = name.lower() == "hidden"
            group  = name
            desc   = None
            continue

        stripped = line.strip()
        if not stripped:
            desc = None          # a description has to sit directly above
            continue
        if stripped.startswith("//"):
            d = _SCAD_DESC_RE.match(line)
            desc = (d.group(1).strip() or None) if d else None
            continue

        a = _SCAD_ASSIGN_RE.match(line)
        if not a:
            desc = None
            continue

        name, rawval, note = a.group(1), a.group(2), a.group(3)
        value, ptype = _scad_literal(rawval)
        # $fn and friends are OpenSCAD specials, not the user's dimensions.
        if ptype is None or hidden or name.startswith("$"):
            desc = None
            continue

        # Re-inserted rather than overwritten in place: a dict keeps a key at
        # its first position, and the row belongs where the assignment that
        # actually binds is — its group header and its neighbours are there.
        found.pop(name, None)
        found[name] = {"line": idx, "text": line, "name": name, "raw": rawval,
                       "note": note, "value": value, "type": ptype,
                       "group": group, "desc": desc}
        desc = None

    return list(found.values())


def parse_scad_parameters(src: str) -> list[dict]:
    """Customizer parameters of a .scad script, in source order."""
    params: list[dict] = []
    if not src:
        return params

    for a in _scad_walk_head(src):
        note = a["note"]
        ann  = _scad_annotation(note or "", a["type"])
        # A trailing comment carrying no annotation is a description in the
        # other legal position, so use it rather than dropping it.
        trailing = note.strip() if note and "[" not in note else None
        label = a["desc"] or trailing or a["name"].replace("_", " ").strip().capitalize()
        blurb = " ".join(filter(None, (a["desc"], trailing)))

        params.append({
            "name":        a["name"],
            "label":       label,
            "description": a["desc"] or trailing,
            "value":       a["value"],
            "type":        a["type"],
            "group":       a["group"],
            "unit":        "mm" if re.search(r"\bmm\b", blurb, re.I) else None,
            **ann,
        })

    return params


def _current_scad(original: bool = False) -> tuple[str | None, str | None]:
    """The live Engineer script, else the last one written to disk.

    With original=True, the script as the model generator first wrote it —
    what "Reset to original" restores to. Once edits have been applied the
    working script no longer holds those values, so they have to be kept
    separately or the reset button silently resets to the last edit.
    """
    key = "scad_original" if original else "scad_script"
    with engineer_state["lock"]:
        script = engineer_state.get(key)
    if script:
        return script, "engineer_state"
    if original:
        return None, None
    path = OUTPUT_DIR / "engineer_bracket.scad"
    try:
        if path.exists():
            return path.read_text(), path.name
    except OSError as e:
        log.warning("Could not read %s: %s", path, e)
    return None, None


@app.get("/api/model/parameters")
def api_model_parameters() -> JSONResponse:
    """Editable dimensions of the current model, for the parameter form.

    Answers 200 with an empty list for every "nothing to edit" case — Speak
    mode, a pre-annotation build, a script of pure expressions — because the
    edit panel renders nothing on an empty list and an error would make the
    viewer look broken when it is working correctly.
    """
    src, source = _current_scad()
    if not src:
        return JSONResponse({"available": False, "source": None,
                             "parameters": [], "groups": []})
    try:
        params = parse_scad_parameters(src)
    except Exception as e:               # a parser bug must not kill the viewer
        log.exception("SCAD parameter parse failed")
        return JSONResponse({"available": False, "source": source,
                             "parameters": [], "groups": [],
                             "error": f"{type(e).__name__}: {e}"})

    groups: list[str] = []
    for p in params:
        if p["group"] not in groups:
            groups.append(p["group"])

    # Values as generated, for "Reset to original". Falls back to the current
    # values when no original was recorded (a script picked up off disk after a
    # restart), so the button is never wired to nothing.
    osrc, _ = _current_scad(original=True)
    try:
        originals = {p["name"]: p["value"] for p in parse_scad_parameters(osrc)} if osrc else {}
    except Exception:
        log.exception("SCAD original-value parse failed")
        originals = {}
    for p in params:
        originals.setdefault(p["name"], p["value"])

    return JSONResponse({"available": bool(params), "source": source,
                         "parameters": params, "groups": groups,
                         "original": originals})


# ---------------------------------------------------------------------------
# Parameter editing — turning form values back into a .scad
# ---------------------------------------------------------------------------

def _scad_number_literal(value: float, like) -> str:
    """A number formatted the way the line it replaces was written.

    `like` is the value being overwritten: a script that said 60.0 keeps its
    decimal point and one that said 60 keeps its absence, so applying an edit
    to one dimension does not silently restyle the whole block.
    """
    v = float(value)
    if v.is_integer() and abs(v) < 1e15:
        return f"{v:.1f}" if isinstance(like, float) else str(int(v))
    return f"{v:.6f}".rstrip("0").rstrip(".")


def _scad_value_literal(value, ptype: str, like) -> str:
    if ptype == "bool":
        return "true" if value else "false"
    if ptype == "number":
        return _scad_number_literal(value, like)
    if ptype == "vector":
        return "[" + ", ".join(_scad_number_literal(v, like[i] if i < len(like) else v)
                               for i, v in enumerate(value)) + "]"
    # Strings are the one type that reaches OpenSCAD as text rather than as a
    # number, so they are the one type that could carry syntax with them.
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    text = re.sub(r"[\r\n]", " ", text)
    return f'"{text}"'


def _coerce_scad_override(spec: dict, raw):
    """(value, None) for an acceptable edit, (None, reason) otherwise.

    Everything here ends up inside a file that OpenSCAD executes, so the check
    is what the parameter declared it accepts — not what happens to parse.
    """
    ptype = spec["type"]

    if spec.get("options"):
        allowed = [o["value"] for o in spec["options"]]
        for a in allowed:
            if a == raw or str(a) == str(raw):
                return a, None
        return None, f"{raw!r} is not one of the declared options"

    if ptype == "bool":
        if isinstance(raw, bool):
            return raw, None
        if str(raw).lower() in ("true", "false", "0", "1"):
            return str(raw).lower() in ("true", "1"), None
        return None, f"{raw!r} is not a boolean"

    if ptype == "number":
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            return None, f"{raw!r} is not a number"
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None, f"{raw!r} is not a number"
        if not math.isfinite(v):
            return None, "value must be finite"
        lo, hi = spec.get("min"), spec.get("max")
        # Clamped rather than rejected: the slider cannot leave the range, so a
        # value outside it came from the number box, and the honest answer to
        # "120 when the max is 100" is the part at 100, not a refusal.
        if isinstance(lo, (int, float)) and v < lo:
            v = float(lo)
        if isinstance(hi, (int, float)) and v > hi:
            v = float(hi)
        return v, None

    if ptype == "string":
        if not isinstance(raw, str):
            return None, f"{raw!r} is not text"
        limit = spec.get("max_length")
        if isinstance(limit, int) and len(raw) > limit:
            return None, f"longer than the declared {limit} characters"
        return raw, None

    if ptype == "vector":
        if not isinstance(raw, (list, tuple)):
            return None, f"{raw!r} is not a vector"
        if len(raw) != len(spec["value"]):
            return None, f"expected {len(spec['value'])} components, got {len(raw)}"
        out = []
        for c in raw:
            try:
                c = float(c)
            except (TypeError, ValueError):
                return None, f"{c!r} is not a number"
            if not math.isfinite(c):
                return None, "components must be finite"
            out.append(c)
        return out, None

    return None, f"unsupported parameter type {ptype!r}"


def apply_scad_overrides(src: str, overrides: dict) -> tuple[str, dict, dict]:
    """Rewrite the customizer block with the caller's values.

    Returns (script, applied, rejected). Only lines the parser already accepted
    as parameters are touched, and only their literal is replaced — so an edit
    cannot reach the modules, the hidden block, or anything past the first "{".
    """
    if not src:
        return src, {}, {k: "no script loaded" for k in (overrides or {})}

    specs = {p["name"]: p for p in parse_scad_parameters(src)}
    applied: dict = {}
    rejected: dict = {}
    edits: dict[int, str] = {}

    for name, raw in (overrides or {}).items():
        spec = specs.get(name)
        if spec is None:
            rejected[name] = "not an editable parameter of this model"
            continue
        value, why = _coerce_scad_override(spec, raw)
        if why:
            rejected[name] = why
            continue
        applied[name] = value

    for a in _scad_walk_head(src):
        if a["name"] not in applied:
            continue
        value = applied[a["name"]]
        if value == a["value"]:
            del applied[a["name"]]        # unchanged — nothing to write
            continue
        literal = _scad_value_literal(value, a["type"], a["value"])
        indent  = a["text"][:len(a["text"]) - len(a["text"].lstrip())]
        # Rebuilt from the parsed pieces rather than patched by substitution:
        # a regex replacing up to the first ";" would cut a string literal that
        # contains one in half.
        line = f"{indent}{a['name']} = {literal};"
        if a["note"] is not None:
            line += f" // {a['note']}"
        edits[a["line"]] = line

    if not edits:
        return src, applied, rejected

    lines = src.splitlines(keepends=True)
    for idx, text in edits.items():
        ending = "\n" if lines[idx].endswith("\n") else ""
        lines[idx] = text + ending
    return "".join(lines), applied, rejected


@app.post("/api/model/scad")
def api_model_scad(body: dict | None = None) -> JSONResponse:
    """The current script with the caller's edits substituted in — rendered by
    nobody.

    The browser's wasm preview needs the exact text the server would render, and
    doing the substitution here rather than in JS means there is one
    implementation of it. Two would eventually disagree, and the failure mode is
    a preview that does not match the part that gets printed.
    """
    body = body or {}
    src, source = _current_scad()
    if not src:
        return JSONResponse({"ok": False, "error": "no model script loaded"},
                            status_code=409)
    try:
        script, applied, rejected = apply_scad_overrides(src, body.get("overrides") or {})
    except Exception as e:
        log.exception("SCAD override substitution failed")
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"},
                            status_code=500)
    return JSONResponse({"ok": True, "source": source, "script": script,
                         "applied": applied, "rejected": rejected})


# ---------------------------------------------------------------------------
# Printer catalogue and selection
#
# data/printers.json is built at development time by tools/import_orca_printers.py
# from OrcaSlicer's bundled profiles and committed to the repo. Nothing here
# reads OrcaSlicer's profile tree and nothing goes to the network.
#
# IMPORTANT SCOPE LIMIT: the selected printer drives the does-it-fit check and
# nothing else. Slicing still runs against the fixed Neptune 4 Plus machine
# profile hard-coded in run_slicing() / orca_slice_sync(). Making selection
# drive slicing would mean vendoring a machine+process+filament triple per
# printer and rewiring the working slice path. The UI says so out loud rather
# than letting the user assume otherwise.
# ---------------------------------------------------------------------------

PRINTERS_JSON = BASE_DIR / "data" / "printers.json"
PRINTER_SETTING_KEY = "selected_printer"

# Past this, a bed dimension is a typo or a joke, not a printer. The largest
# real bed in the imported catalogue is about 1000mm, so this leaves room for
# machines bigger than anything OrcaSlicer ships without accepting 99999.
MAX_BED_MM = 3000.0

_printer_catalog: dict = {"loaded": False, "printers": [], "by_id": {}, "error": None}


def _load_printer_catalog() -> dict:
    """Read data/printers.json once, tolerating its absence.

    A missing or corrupt catalogue is not fatal anywhere: the picker shows the
    built-in Generic entry, the fit check reports that it has no printer, and
    every other part of the pipeline is untouched.
    """
    if _printer_catalog["loaded"]:
        return _printer_catalog

    # Always available even with no catalogue file at all, so the user can still
    # type a bed size and get a real fit check.
    generic = {
        "id": "generic/generic-fdm-printer", "vendor": "Generic",
        "name": "Generic FDM Printer", "model": "Generic FDM Printer",
        "bed_x": 220.0, "bed_y": 220.0, "bed_z": 250.0,
        "origin": "corner", "bed_shape": "rect", "nozzles": [0.4],
        "extruders": 1, "variants": [], "editable": True, "source": "builtin",
    }
    printers, error = [], None
    try:
        data = json.loads(PRINTERS_JSON.read_text())
        printers = [p for p in data.get("printers", []) if p.get("id")]
        if not printers:
            error = "catalogue file contains no printers"
    except FileNotFoundError:
        error = "data/printers.json not found — run tools/import_orca_printers.py"
    except Exception as e:                                  # noqa: BLE001
        error = f"catalogue unreadable: {type(e).__name__}: {e}"

    if error:
        log.warning("[Printers] %s — falling back to the Generic entry", error)
        printers = [generic]
    if not any(p["id"] == generic["id"] for p in printers):
        printers = [generic] + printers

    _printer_catalog.update({"loaded": True, "printers": printers,
                             "by_id": {p["id"]: p for p in printers},
                             "error": error})
    log.info("[Printers] catalogue: %d entries%s", len(printers),
             f" ({error})" if error else "")
    return _printer_catalog


def _printer_summary(p: dict) -> str:
    """One line of real specs, for logs and for anything that needs a sentence."""
    z = "?" if p.get("bed_z") is None else f"{p['bed_z']:g}"
    noz = ", ".join(f"{n:g}" for n in (p.get("nozzles") or [])) or "?"
    return (f"{p['name']} — bed {p.get('bed_x', 0):g} x {p.get('bed_y', 0):g} "
            f"x {z} mm, {noz} mm nozzle")


def get_selected_printer() -> dict | None:
    """The printer the user picked, or None. Never raises.

    The stored row is a full snapshot, not just an id. Re-importing the
    catalogue after an OrcaSlicer upgrade can renumber or rename an entry, and a
    selection that silently became "no printer" would turn the fit check off
    without saying so. The snapshot keeps working; it just stops being refreshed.
    """
    raw = None
    try:
        raw = db_get_setting(PRINTER_SETTING_KEY)
    except Exception as e:                                  # noqa: BLE001
        log.warning("[Printers] could not read selection: %s", e)
        return None
    if not raw:
        return None
    try:
        sel = json.loads(raw)
    except Exception:                                       # noqa: BLE001
        log.warning("[Printers] stored selection is not valid JSON — ignoring")
        return None
    if not isinstance(sel, dict) or not sel.get("id"):
        return None

    live = _load_printer_catalog()["by_id"].get(sel["id"])
    if live:
        # Catalogue wins on specs, snapshot wins on the dimensions the user
        # typed for the Generic entry.
        merged = dict(live)
        for k in ("bed_x", "bed_y", "bed_z"):
            if sel.get(k) is not None and live.get("editable"):
                merged[k] = sel[k]
        return merged
    sel["stale"] = True          # no longer in the catalogue; still usable
    return sel


@app.get("/api/printers")
def api_printers(q: str = "", limit: int = 50) -> JSONResponse:
    """Search the catalogue. Empty query returns the first page, not all 397."""
    cat = _load_printer_catalog()
    rows = cat["printers"]

    terms = [t for t in str(q or "").lower().split() if t]
    if terms:
        scored = []
        for p in rows:
            hay = f"{p.get('vendor', '')} {p.get('name', '')} {p.get('model', '')}".lower()
            if not all(t in hay for t in terms):
                continue
            # Prefix matches on the visible name first — typing "prusa" should
            # not bury Prusa printers under other vendors' clone profiles.
            name = str(p.get("name", "")).lower()
            rank = 0 if name.startswith(terms[0]) else (
                1 if str(p.get("vendor", "")).lower().startswith(terms[0]) else 2)
            scored.append((rank, name, p))
        scored.sort(key=lambda s: (s[0], s[1]))
        rows = [s[2] for s in scored]

    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 50

    return JSONResponse({
        "ok": True,
        "total": len(cat["printers"]),
        "matched": len(rows),
        "printers": rows[:limit],
        "catalog_error": cat["error"],
        # Said once here so every consumer of this API gets the same caveat.
        "slicing_note": "Printer selection drives the fit check only. Slicing "
                        "always uses the built-in Neptune 4 Plus profile.",
    })


# /api/printer (no suffix) is already the live Moonraker status of the machine
# on the network. This is a different thing — which printer's dimensions to
# check parts against — so it gets its own path rather than shadowing that one.
@app.get("/api/printer/profile")
def api_printer_get() -> JSONResponse:
    cat = _load_printer_catalog()
    sel = get_selected_printer()
    return JSONResponse({"ok": True, "printer": sel,
                         "summary": _printer_summary(sel) if sel else None,
                         "catalog_error": cat["error"],
                         "catalog_size": len(cat["printers"])})


@app.post("/api/printer/profile")
def api_printer_set(body: dict | None = None) -> JSONResponse:
    """Select a printer, or clear the selection with {"id": null}.

    Last write wins, deliberately. Two kiosk tabs each picking a printer is a
    real scenario and there is one physical printer, so the most recent choice
    is the right one; both tabs then re-read on their next poll.
    """
    body = body or {}
    pid = body.get("id")

    if pid in (None, ""):
        db_set_setting(PRINTER_SETTING_KEY, None)
        engineer_log("Printer selection cleared", "info")
        return JSONResponse({"ok": True, "printer": None})

    cat = _load_printer_catalog()
    base = cat["by_id"].get(pid)
    if not base:
        return JSONResponse({"ok": False, "error": f"unknown printer id {pid!r}"},
                            status_code=404)

    stored = {"id": pid}
    if base.get("editable"):
        # Only the Generic entry accepts typed dimensions. A real printer's bed
        # is a fact about the machine, not a preference.
        for key, label in (("bed_x", "width"), ("bed_y", "depth"), ("bed_z", "height")):
            if body.get(key) is None:
                continue
            try:
                val = float(body[key])
            except (TypeError, ValueError):
                return JSONResponse({"ok": False,
                                     "error": f"Bed {label} must be a number"},
                                    status_code=422)
            if not (val == val and abs(val) != float("inf")):   # NaN / inf
                return JSONResponse({"ok": False,
                                     "error": f"Bed {label} must be a real number"},
                                    status_code=422)
            if val <= 0:
                return JSONResponse({"ok": False,
                                     "error": f"Bed {label} must be greater than 0 "
                                              f"— got {val:g}mm"},
                                    status_code=422)
            if val > MAX_BED_MM:
                return JSONResponse({"ok": False,
                                     "error": f"Bed {label} of {val:g}mm is larger "
                                              f"than any real printer (max "
                                              f"{MAX_BED_MM:g}mm)"},
                                    status_code=422)
            stored[key] = round(val, 3)

    db_set_setting(PRINTER_SETTING_KEY, json.dumps(stored))
    sel = get_selected_printer()
    engineer_log(f"Printer set: {_printer_summary(sel)}" if sel
                 else "Printer set", "info")
    return JSONResponse({"ok": True, "printer": sel,
                         "summary": _printer_summary(sel) if sel else None})


def _stl_dimensions(stl_path: Path) -> dict | None:
    """Bounding box of a rendered STL in mm, or None if it cannot be read."""
    try:
        import trimesh
        mesh = trimesh.load(str(stl_path), force="mesh")
        lo, hi = mesh.bounds
        return {"x": round(float(hi[0] - lo[0]), 3),
                "y": round(float(hi[1] - lo[1]), 3),
                "z": round(float(hi[2] - lo[2]), 3)}
    except Exception as e:
        log.info("[Params] STL dimension read skipped: %s", e)
        return None


# ---------------------------------------------------------------------------
# Does it fit on the bed?
# ---------------------------------------------------------------------------

# Parts do not fit a bed to the micron. This is float noise tolerance, not
# clearance: a 235.0mm part on a 235mm bed is a fit, and 235.4 is not.
FIT_EPSILON_MM = 0.05

# The six ways an axis-aligned box can sit on a bed, as (footprint_a,
# footprint_b, height) index triples into (x, y, z). The first is as-modelled;
# the second is the free 90° spin; the rest lay it on another face.
# Each carries the instruction in the imperative, because it gets dropped
# straight into "This fits if you ___." and has to read as something to do.
_ORIENTATIONS = [
    ((0, 1, 2), "as-is",                     "leave it as it is"),
    ((1, 0, 2), "rotated 90° on the bed",    "rotate it 90° on the bed"),
    ((0, 2, 1), "on its side",               "lay it on its side"),
    ((2, 0, 1), "on its side, rotated 90°",  "lay it on its side and rotate it 90°"),
    ((1, 2, 0), "on end",                    "stand it on end"),
    ((2, 1, 0), "on end, rotated 90°",       "stand it on end and rotate it 90°"),
]


def _point_in_polygon(x: float, y: float, poly: list) -> bool:
    """Ray casting. Used only for the 20 non-rectangular beds."""
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xint = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < xint:
                inside = not inside
    return inside


def _footprint_fits(w: float, d: float, printer: dict) -> bool:
    """Can a w x d rectangle sit on this bed?"""
    bed_x = float(printer.get("bed_x") or 0)
    bed_y = float(printer.get("bed_y") or 0)
    if w > bed_x + FIT_EPSILON_MM or d > bed_y + FIT_EPSILON_MM:
        return False

    poly = printer.get("bed_polygon")
    if not poly or len(poly) < 3:
        return True                      # rectangular bed; the extents settle it

    # Non-rectangular bed (delta circles, hexagons). Every one of these in the
    # catalogue is convex, and for a convex bed the best placement of an
    # axis-aligned rectangle is centred, so testing the four corners of a
    # centred rectangle is both cheap and correct here.
    cx = sum(p[0] for p in poly) / len(poly)
    cy = sum(p[1] for p in poly) / len(poly)
    hw, hd = w / 2.0, d / 2.0
    return all(_point_in_polygon(cx + sx * hw, cy + sy * hd, poly)
               for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)))


def check_model_fit(dims: dict | None, printer: dict | None) -> dict:
    """Will this bounding box print on this bed, in any orientation?

    Never raises and never blocks on missing information. "No printer selected"
    and "could not measure the model" are reported as their own verdicts so the
    caller can say why the check did not happen, rather than skipping silently.
    """
    if not printer:
        return {"verdict": "no_printer", "blocking": False,
                "message": "No printer selected, so this part has not been size-"
                           "checked. Pick your printer to have Conjure warn you "
                           "before slicing something that will not fit."}
    if not dims:
        return {"verdict": "unknown", "blocking": False,
                "printer": printer.get("name"),
                "message": f"Could not measure this model, so it has not been "
                           f"checked against your {printer.get('name')}. "
                           f"Slicing is still allowed."}

    size = [float(dims.get("x") or 0), float(dims.get("y") or 0),
            float(dims.get("z") or 0)]
    bed_z = printer.get("bed_z")
    name = printer.get("name", "your printer")

    fits = []
    for (ia, ib, ih), label, instruction in _ORIENTATIONS:
        w, d, h = size[ia], size[ib], size[ih]
        if not _footprint_fits(w, d, printer):
            continue
        # The one machine with no printable_height: check the footprint, flag the
        # height as unverified, and do not invent a limit to block on.
        if bed_z is not None and h > float(bed_z) + FIT_EPSILON_MM:
            continue
        fits.append({"orientation": label, "instruction": instruction,
                     "size": [round(v, 2) for v in (w, d, h)]})

    bed_desc = (f"{printer.get('bed_x', 0):g} x {printer.get('bed_y', 0):g}"
                f" x {'?' if bed_z is None else format(float(bed_z), 'g')} mm")
    common = {"printer": name, "bed": bed_desc,
              "model": [round(v, 2) for v in size],
              "z_unverified": bed_z is None}
    # Never claim a clean fit on a height that was never checked.
    z_caveat = ("" if bed_z is not None else
                f" Note: OrcaSlicer's profile for this printer does not state a "
                f"maximum height, so only the {size[0]:g} x {size[1]:g} mm "
                f"footprint was checked.")

    if fits and fits[0]["orientation"] == "as-is":
        return {**common, "verdict": "fits", "blocking": False,
                "orientation": "as-is",
                "message": f"Fits on your {name} ({bed_desc})." + z_caveat}

    if fits:
        best = fits[0]
        return {**common, "verdict": "fits_rotated", "blocking": False,
                "orientation": best["orientation"],
                "alternatives": fits,
                "message": (f"This fits if you {best['instruction']}. "
                            f"As modelled it is {size[0]:g} x {size[1]:g} x "
                            f"{size[2]:g} mm, and your {name} bed is "
                            f"{bed_desc}." + z_caveat)}

    # Nothing works. Name the axis that actually overruns, using the smallest
    # part dimension against the largest bed dimension -- that is the pairing
    # that has to fail for the part to be genuinely unprintable.
    over = []
    bed_x, bed_y = float(printer.get("bed_x") or 0), float(printer.get("bed_y") or 0)
    biggest_flat = max(bed_x, bed_y)
    for axis, value in zip("XYZ", size):
        if value > biggest_flat + FIT_EPSILON_MM and (
                bed_z is None or value > float(bed_z) + FIT_EPSILON_MM):
            over.append(f"{value:g}mm ({axis})")
    detail = (f"It measures {size[0]:g} x {size[1]:g} x {size[2]:g} mm; "
              f"your {name} has a {bed_desc} build volume.")
    if over:
        detail = (f"It is {', '.join(over)} — larger than any dimension of your "
                  f"{name}, which is {bed_desc}.")
    return {**common, "verdict": "too_big", "blocking": True,
            "split": plan_split(dims, printer),
            "message": f"This model will not fit on your {name} in any "
                       f"orientation. {detail}"}


# ---------------------------------------------------------------------------
# How to cut it up
# ---------------------------------------------------------------------------

# Room for the kerf of a saw, print swell, and a skim of glue. Cutting exactly
# at bed_x means every piece is exactly bed-sized, which in practice does not
# seat. Shrinking the usable bed slightly makes each piece land inside it.
SPLIT_MARGIN_MM = 5.0


def _joint_advice(area_mm2: float, pieces: int) -> dict:
    """What to rejoin the pieces with, and why, from the size of the mating face.

    The area is the bounding cross-section of one piece at the cut -- an upper
    bound on the real glue area, since the part is rarely solid there. It is
    used only to pick between three well-separated recommendations, which is
    what it is good enough for.
    """
    if area_mm2 < 150:
        joint, why = ("Superglue (cyanoacrylate) alone",
                      "the mating face is too small to drill for pins without "
                      "removing most of what is holding it together")
    elif area_mm2 < 1200:
        joint, why = ("Glue plus two 3mm dowel pins",
                      "pins stop the faces sliding while the glue sets, and "
                      "carry the shear that a butt-glued joint would not")
    else:
        joint, why = ("Bolts into captive nuts, or glue plus four 4mm dowels",
                      "a face this large is hard to align by hand and heavy "
                      "enough to peel a glue-only joint apart")

    extra = []
    if pieces > 4:
        extra.append(f"Number the {pieces} pieces as they come off the bed — "
                     f"at this count they stop being obvious.")
    extra.append("Print a dovetail or puzzle joint instead only if you want no "
                 "hardware: it self-aligns, but the clearance needs tuning to "
                 "your printer and usually takes a test fit or two.")
    return {"joint": joint, "why": why, "notes": extra}


def _mesh_solidity(stl_path: Path) -> float | None:
    """Fraction of the bounding box the part actually fills, or None.

    Used to keep the mating-face estimate honest. A hollow enclosure and a solid
    block with the same bounding box have very different amounts of material at
    a cut, and the joint advice should not treat them alike. Measuring the true
    cross-section would need trimesh's section(), which needs scipy, which is
    not installed -- so this scales the bounding cross-section by how solid the
    part is overall, which separates the three cases the advice distinguishes.
    """
    try:
        import trimesh
        mesh = trimesh.load(str(stl_path), force="mesh")
        lo, hi = mesh.bounds
        bbox_vol = float((hi[0] - lo[0]) * (hi[1] - lo[1]) * (hi[2] - lo[2]))
        if bbox_vol <= 0 or not mesh.is_watertight:
            return None            # volume is meaningless on an open mesh
        return max(0.02, min(1.0, abs(float(mesh.volume)) / bbox_vol))
    except Exception as e:                                  # noqa: BLE001
        log.info("[Split] solidity unavailable: %s", e)
        return None


def plan_split(dims: dict | None, printer: dict | None,
               solidity: float | None = None) -> dict | None:
    """Cut a too-big part into bed-sized pieces: how many, which way, where.

    Returns None when the part fits or there is nothing to plan against. Every
    number is computed from the real bounding box and the real bed, not from a
    rule of thumb.

    The cut planes are spaced evenly, and that is all they are. Conjure does not
    look at what a plane passes through, so one can land on a screw hole or down
    a thin wall. The plan says so rather than implying the positions were chosen
    with the part's features in mind.
    """
    if not dims or not printer:
        return None
    size = [float(dims.get("x") or 0), float(dims.get("y") or 0),
            float(dims.get("z") or 0)]
    if min(size) <= 0:
        return None

    bed = [float(printer.get("bed_x") or 0), float(printer.get("bed_y") or 0),
           None if printer.get("bed_z") is None else float(printer["bed_z"])]
    # Height is the one axis where the margin is not needed: nothing has to slide
    # past anything to seat a part vertically.
    usable = [max(bed[0] - SPLIT_MARGIN_MM, 1.0),
              max(bed[1] - SPLIT_MARGIN_MM, 1.0),
              bed[2]]
    if usable[0] <= 1 or usable[1] <= 1:
        return None

    best = None
    for (ia, ib, ih), label, instruction in _ORIENTATIONS:
        counts = [1, 1, 1]
        counts[ia] = math.ceil(size[ia] / usable[0])
        counts[ib] = math.ceil(size[ib] / usable[1])
        counts[ih] = 1 if usable[2] is None else math.ceil(size[ih] / usable[2])
        total = counts[0] * counts[1] * counts[2]
        axes_cut = sum(1 for c in counts if c > 1)
        # Fewest pieces first; then fewest separate cut directions, because a
        # part cut on one axis is far easier to align than one cut on two.
        key = (total, axes_cut, 0 if label == "as-is" else 1)
        if best is None or key < best[0]:
            best = (key, label, instruction, counts)

    if best is None:
        return None
    (total, axes_cut, _), label, instruction, counts = best
    if total <= 1:
        return None

    cuts = []
    for axis, count in enumerate(counts):
        if count < 2:
            continue
        step = size[axis] / count
        cuts.append({
            "axis": "XYZ"[axis],
            "pieces": count,
            "piece_mm": round(step, 1),
            "positions_mm": [round(step * k, 1) for k in range(1, count)],
        })

    # Cross-section of one piece at the first cut: the two axes that are not
    # being cut through, at their per-piece size, scaled by how solid the part
    # actually is. Without the scaling a hollow enclosure would be told to use
    # bolts because its bounding box is large.
    first = cuts[0]
    ai = "XYZ".index(first["axis"])
    others = [i for i in range(3) if i != ai]
    area = (size[others[0]] / counts[others[0]]) * (size[others[1]] / counts[others[1]])
    if solidity is not None:
        area *= solidity

    return {
        "pieces": total,
        "orientation": label,
        "orientation_instruction": instruction,
        "cuts": cuts,
        "cut_area_mm2": round(area, 1),
        "cut_area_estimated": solidity is not None,
        "rejoin": _joint_advice(area, total),
        "margin_mm": SPLIT_MARGIN_MM,
        "caveat": ("Cut positions are spaced evenly — Conjure has not checked "
                   "what each plane passes through. If one lands on a hole, a "
                   "boss or a thin wall, move it a few millimetres; the pieces "
                   "do not have to be equal."),
    }


def current_model_fit() -> dict:
    """Fit verdict for the STL currently on disk. Never raises."""
    try:
        stl = OUTPUT_DIR / "model.stl"
        if not stl.exists():
            return {"verdict": "no_model", "blocking": False,
                    "message": "No model yet."}
        dims, printer = _stl_dimensions(stl), get_selected_printer()
        fit = check_model_fit(dims, printer)
        if fit.get("verdict") == "too_big":
            # Recomputed with the real mesh so the joint advice reflects how
            # much material is actually at the cut, not just the bounding box.
            fit["split"] = plan_split(dims, printer, _mesh_solidity(stl))
        return fit
    except Exception as e:                                  # noqa: BLE001
        # A broken fit check must never be the reason a working pipeline stops.
        log.warning("[Fit] check failed, allowing through: %s", e)
        return {"verdict": "unknown", "blocking": False,
                "message": f"Size check could not run ({type(e).__name__}). "
                           f"Slicing is still allowed."}


@app.get("/api/model/fit")
def api_model_fit() -> JSONResponse:
    return JSONResponse({"ok": True, "fit": current_model_fit()})


# Serialises /api/model/apply. Every apply funnels through three fixed paths —
# engineer_edit.scad, engineer_edit.stl and model.stl — so two overlapping
# requests render each other's script and race to move the same temp file onto
# model.stl. The loser hits a FileNotFoundError, and the survivor leaves a
# model.stl whose geometry disagrees with the engineer_bracket.scad recorded
# beside it. Read-modify-render-commit has to be one atomic unit; a lock of its
# own rather than engineer_state's, because it is held across a render that may
# take minutes and the status endpoint the UI polls must not block behind it.
_apply_lock = threading.Lock()


@app.post("/api/model/apply")
def api_model_apply(body: dict | None = None) -> JSONResponse:
    """Commit edited dimensions: rewrite the .scad, re-render, re-run the gate.

    This is the authoritative path — the browser's wasm render is a preview of
    it, never a substitute. Three things have to happen together or the build
    stops agreeing with itself: the script on disk becomes the edited one, the
    STL everything downstream reads is re-rendered from it, and the previous
    gcode is destroyed. That last one matters most: model.gcode was sliced from
    the pre-edit STL, and leaving it on disk next to an edited model is the
    stale-gcode failure the print and USB paths already refuse to commit.
    """
    body = body or {}
    with engineer_state["lock"]:
        if engineer_state["status"] == "RUNNING":
            return JSONResponse({"ok": False, "error": "A build is still running "
                                 "— wait for it to finish before editing"},
                                status_code=409)
        specs = engineer_state.get("specs") or {}

    with _apply_lock:
        src, _ = _current_scad()
        if not src:
            return JSONResponse({"ok": False, "error": "no model script loaded"},
                                status_code=409)

        try:
            script, applied, rejected = apply_scad_overrides(src, body.get("overrides") or {})
        except Exception as e:
            log.exception("SCAD override substitution failed")
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"},
                                status_code=500)

        if not applied:
            return JSONResponse({"ok": True, "changed": False, "applied": {},
                                 "rejected": rejected,
                                 "message": "nothing to apply"})

        # Rendered to a scratch file first. A failed render must leave the previous
        # model.stl untouched rather than half-overwritten, because the viewer, the
        # slicer and the gate all read that one path.
        stl_path = OUTPUT_DIR / "model.stl"
        tmp_scad = OUTPUT_DIR / "engineer_edit.scad"
        tmp_stl  = OUTPUT_DIR / "engineer_edit.stl"
        try:
            tmp_scad.write_text(script)
            result = subprocess.run(
                [OPENSCAD_PATH, "-o", str(tmp_stl), str(tmp_scad)],
                capture_output=True, timeout=300, cwd=str(BASE_DIR),
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.decode("utf-8", "replace")[-400:] or
                                   f"openscad exited {result.returncode}")
            if not tmp_stl.exists() or tmp_stl.stat().st_size < 1000:
                raise RuntimeError("STL too small — the edited geometry is not solid")
        except subprocess.TimeoutExpired:
            return JSONResponse({"ok": False, "applied": applied, "rejected": rejected,
                                 "error": "OpenSCAD timed out re-rendering the edited "
                                          "model — the original is unchanged"},
                                status_code=504)
        except Exception as e:
            engineer_log(f"Edited render failed: {e}", "warning")
            return JSONResponse({"ok": False, "applied": applied, "rejected": rejected,
                                 "error": f"Could not render the edited model: {e}"},
                                status_code=422)

        shutil.move(str(tmp_stl), str(stl_path))
        (OUTPUT_DIR / "engineer_bracket.scad").write_text(script)

        # The gate runs on the EDITED mesh. Running it once at build time and
        # trusting that verdict afterwards would mean an edit could take a part
        # non-manifold or under the wall minimum and still inherit a pass.
        review = engineer_review_gate(stl_path, specs, "")

        gcode_path = OUTPUT_DIR / "model.gcode"
        stale_removed = False
        try:
            if gcode_path.exists():
                gcode_path.unlink()
                stale_removed = True
        except OSError as e:
            log.warning("Could not remove stale gcode: %s", e)
        forget_gcode_source(gcode_path)

        with engineer_state["lock"]:
            engineer_state["scad_script"] = script
            engineer_state["review"] = review
            engineer_state["slice_ok"] = None      # this model has not been sliced

        dims = _stl_dimensions(stl_path)
        # Re-checked on the edited mesh, for the same reason the gate is: an edit
        # can take a part that fitted and make it too big for the bed, and finding
        # that out at slice time is later than it needs to be.
        fit = check_model_fit(dims, get_selected_printer())
        engineer_log(
            "Dimensions edited: " + ", ".join(f"{k}={v}" for k, v in applied.items()) +
            (f" — {dims['x']}x{dims['y']}x{dims['z']}mm" if dims else "") +
            (" — previous gcode discarded" if stale_removed else ""),
            "success" if review["verdict"] == "pass" else "warning")
        if fit.get("verdict") in ("too_big", "fits_rotated"):
            engineer_log(f"Size check: {fit.get('message')}",
                         "warning" if fit.get("blocking") else "info")

        return JSONResponse({"ok": True, "changed": True, "applied": applied,
                             "rejected": rejected, "review": review,
                             "dimensions": dims, "fit": fit,
                             "stl_bytes": stl_path.stat().st_size,
                             "needs_reslice": True})


def review_design(scad_script: str, specs: dict) -> None:
    text = _llm_text(
        _REVIEW_SYSTEM,
        f"Specs: {specs}\n\nOpenSCAD:\n{scad_script[:6000]}",
        "EngineerReview",
        max_tokens=400,
    )
    if not text:
        return
    with engineer_state["lock"]:
        engineer_state["design_review"] = text
    engineer_log(f"Review: {text}", "info")


def orca_slice_sync(stl_path: Path, gcode_path: Path) -> bool:
    """Blocking OrcaSlicer slice for the engineer pipeline.

    A sync twin of the async path in run_slicing() — same flattened Neptune 4
    Plus preset bundle and same CLI form, which is what the speak-mode pipeline
    already proves works. Engineer mode used CuraEngine at a Linux-only path
    that does not exist on the dev Mac, so this step never ran here.
    """
    orca = ORCASLICER_PATH
    if not orca or not Path(orca).exists():
        engineer_log(f"OrcaSlicer not found at {orca} — skipping slice", "warning")
        return False
    orca_out = OUTPUT_DIR / "orca_out_engineer"
    shutil.rmtree(orca_out, ignore_errors=True)
    orca_out.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [
                orca,
                "--load-settings",
                f"{PROFILES_DIR / 'flat_machine_neptune4plus_04.json'};"
                f"{PROFILES_DIR / 'flat_process_0.20mm_standard_n4plus_04.json'}",
                "--load-filaments", str(PROFILES_DIR / "flat_filament_elegoo_pla_en4plus.json"),
                "--ensure-on-bed",
                "--arrange", "1",
                "--slice", "0",
                "--outputdir", str(orca_out),
                str(stl_path),
            ],
            capture_output=True, timeout=300, cwd=str(BASE_DIR),
        )
        plate = orca_out / "plate_1.gcode"        # CLI always writes this name
        if result.returncode != 0 or not plate.exists() or plate.stat().st_size < 5000:
            tail = result.stderr.decode("utf-8", "ignore")[-300:]
            engineer_log(f"OrcaSlicer failed (rc={result.returncode}) {tail}", "warning")
            return False
        shutil.move(str(plate), str(gcode_path))
        record_gcode_source(stl_path, gcode_path)
        engineer_log(f"Gcode sliced: {gcode_path.stat().st_size / (1024*1024):.1f}MB", "success")
        return True
    except subprocess.TimeoutExpired:
        engineer_log("OrcaSlicer timed out", "warning")
        return False
    except Exception as e:
        engineer_log(f"OrcaSlicer error: {e}", "warning")
        return False


def engineer_review_gate(stl_path: Path, specs: dict, intent: str) -> dict:
    """Review between render and slice. Decides whether this build is allowed
    to be called COMPLETE.

    This runs BEFORE the slice, not after: with a printer configured, a build
    marked COMPLETE has already been dispatched, and a quality signal produced
    after the print starts is a record, not a check.
    """
    issues = []

    watertight = None
    open_edges = None
    try:
        import trimesh
        import numpy as np
        from collections import Counter
        mesh = trimesh.load(str(stl_path), force="mesh")
        mesh.merge_vertices()
        # NOT trimesh's is_watertight. Measured against real OpenSCAD output on
        # 2026-08-03: a plain closed coaster reported is_watertight False while
        # having ZERO edges used by only one face — it is closed. That flag also
        # folds in winding/duplicate-face strictness which OpenSCAD's STL writer
        # trips constantly, so gating prints on it would have marked essentially
        # every legitimate build NEEDS_REVIEW and withheld every print.
        #   real coaster      faces=3068 edges=4538 used_once=0   <- closed
        #   deliberately torn faces=1240 edges=1878 used_once=36  <- open
        # An edge used by exactly one face is what actually makes a mesh
        # unprintable, so that is what gets counted.
        counts = Counter(map(tuple, np.sort(mesh.edges, axis=1)))
        open_edges = sum(1 for v in counts.values() if v == 1)
        watertight = open_edges == 0
    except Exception as me:
        # None, never False. "We could not check" and "the mesh is open" are
        # different claims, and this one gates a print.
        log.info("[Gate] watertight check skipped: %s", me)
    if watertight is False:
        issues.append(f"mesh has {open_edges} open edge(s) — not a closed solid, "
                      f"will slice into unpredictable geometry")

    wall = specs.get("wall_mm")
    if isinstance(wall, (int, float)) and wall < MIN_WALL_MM:
        issues.append(f"wall {wall}mm is below the {MIN_WALL_MM}mm minimum "
                      f"({NOZZLE_MM}mm nozzle needs two perimeters)")

    sf = specs.get("safety_factor")
    if isinstance(sf, (int, float)) and sf < DESIGN_SAFETY_FACTOR:
        issues.append(f"safety factor {sf} is under the {DESIGN_SAFETY_FACTOR} "
                      f"design minimum")

    verdict = "needs_review" if issues else "pass"
    result = {
        "verdict": verdict,
        "watertight": watertight,
        "open_edges": open_edges,
        "wall_mm": wall,
        "safety_factor": sf,
        "issues": issues,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    return result


def run_engineer_pipeline(intent: str) -> None:
    with engineer_state["lock"]:
        engineer_state["status"] = "RUNNING"
        engineer_state["current_step"] = 0
        engineer_state["current_step_name"] = ENGINEER_STEP_NAMES[0]
        engineer_state["logs"] = []
        engineer_state["scad_script"] = None
        engineer_state["scad_original"] = None
        engineer_state["specs"] = None
        engineer_state["design_review"] = None
        # Cleared on entry, not just set on success: a build that crashes before
        # step 3 must not leave the previous build's True sitting here and make
        # its stale gcode look exportable.
        engineer_state["slice_ok"] = None
        engineer_state["review"] = None

    engineer_log("=== ENGINEER PIPELINE STARTED ===", "success")
    engineer_log(f"Intent: {intent}", "info")

    engineer_set_step(0)
    engineer_log("Step 0 — design brief", "info")
    specs = design_brief(intent)
    with engineer_state["lock"]:
        engineer_state["specs"] = specs
    engineer_log(f"Specs: {specs}", "success")

    engineer_set_step(1)
    engineer_log("Step 1 — CAD generation", "info")
    scad_script = generate_scad(intent, specs)
    if not scad_script:
        # Stopping here on purpose. The old pipeline substituted a hardcoded
        # pipe clamp and reported success, so every unanswerable request
        # silently produced the same wrong part.
        engineer_log("No CAD could be generated — engineer mode needs an "
                     "Anthropic API key with available credit", "error")
        engineer_set_status("ERROR")
        return

    scad_path = OUTPUT_DIR / "engineer_bracket.scad"
    scad_path.write_text(scad_script)
    with engineer_state["lock"]:
        engineer_state["scad_script"] = scad_script
        engineer_state["scad_original"] = scad_script
    engineer_log(f"SCAD saved: {len(scad_script.splitlines())} lines", "success")

    engineer_set_step(2)
    engineer_log(f"Step 2 — OpenSCAD render (binary: {OPENSCAD_PATH})", "info")
    stl_path = OUTPUT_DIR / "model.stl"
    try:
        result = subprocess.run(
            [OPENSCAD_PATH, "-o", str(stl_path), str(scad_path)],
            capture_output=True,
            timeout=90,
            cwd=str(BASE_DIR),
        )
        if result.returncode != 0:
            raise Exception(result.stderr.decode("utf-8", errors="replace")[:500])
        if not stl_path.exists() or stl_path.stat().st_size < 1000:
            raise Exception("STL too small — geometry likely invalid")
        stl_kb = stl_path.stat().st_size / 1024
        engineer_log(f"STL rendered: {stl_kb:.1f}KB", "success")
    except Exception as e:
        engineer_log(f"OpenSCAD render failed: {e}", "error")
        engineer_set_status("ERROR")
        return

    # Second-agent review, between render and slice. Its verdict decides
    # whether this build may print and whether it ends COMPLETE.
    review = engineer_review_gate(stl_path, specs, intent)
    with engineer_state["lock"]:
        engineer_state["review"] = review
    if review["verdict"] == "needs_review":
        engineer_log(f"Review gate: NEEDS REVIEW — {len(review['issues'])} "
                     f"issue(s), print will be withheld", "warning")
        for iss in review["issues"]:
            engineer_log(f"  • {iss}", "warning")
    else:
        engineer_log(f"Review gate: pass (watertight={review['watertight']}, "
                     f"wall={review['wall_mm']}mm, sf={review['safety_factor']})",
                     "success")

    engineer_set_step(3)
    engineer_log("Step 3 — OrcaSlicer slice", "info")
    gcode_path = OUTPUT_DIR / "model.gcode"
    # Return value bound rather than discarded — it is the only trustworthy
    # signal that *this* run sliced. A failed slice leaves the previous run's
    # model.gcode in place, so "the file exists and is large" proves nothing.
    slice_ok = orca_slice_sync(stl_path, gcode_path)
    with engineer_state["lock"]:
        engineer_state["slice_ok"] = slice_ok

    try:
        model_id = db_insert_model(intent, "engineer-" + datetime.now().strftime("%Y%m%d%H%M%S"))
        # Engineer parts are parametric CAD -> STL; there is NO Meshy GLB. Persist
        # the STL into a per-model dir (not the shared output/model.stl scratch,
        # which the next job overwrites) and leave glb_path EMPTY. Previously both
        # paths were set to the shared scratch STL, so library entries pointed at a
        # non-existent/overwritten "GLB" and dead-ended on View. See list_models.
        eng_dir = OUTPUT_DIR / "models" / str(model_id)
        eng_dir.mkdir(parents=True, exist_ok=True)
        eng_stl = eng_dir / "model.stl"
        try:
            shutil.copy2(stl_path, eng_stl)
            saved_stl = str(eng_stl)
        except Exception as ce:
            engineer_log(f"STL copy to per-model dir failed: {ce}", "warning")
            saved_stl = str(stl_path)
        # Engineer parts have no Meshy GLB. Convert the OpenSCAD STL -> GLB with
        # trimesh so the library viewer (three.js GLTFLoader / loadModel) can
        # preview them exactly like Speak-mode models. Degrades to STL-only on
        # any failure, so a bad conversion never breaks the pipeline.
        saved_glb = ""
        try:
            import trimesh
            mesh = trimesh.load(saved_stl, force="mesh")
            glb_out = eng_dir / "model.glb"
            mesh.export(str(glb_out), file_type="glb")
            if glb_out.exists() and glb_out.stat().st_size > 0:
                saved_glb = str(glb_out)
                # Mirror to the scratch active file so the immediate post-generation
                # viewer can also show the GLB.
                try:
                    shutil.copy2(glb_out, OUTPUT_DIR / "model.glb")
                except Exception:
                    pass
                engineer_log(f"GLB converted from STL: {glb_out.stat().st_size // 1024}KB", "success")
        except Exception as ge:
            engineer_log(f"STL->GLB conversion failed: {ge} — STL only", "warning")
        db_update_model_paths(model_id, saved_glb, saved_stl)
        engineer_log(f"DB: model record saved (id {model_id}{'' if saved_glb else ', STL only'})", "success")
    except Exception as e:
        engineer_log(f"DB save failed: {e}", "warning")
        model_id = None

    engineer_set_step(4)
    engineer_log(f"Step 4 — send to printer ({moonraker_base() or 'no printer configured'})", "info")
    try:
        if not moonraker_base():
            raise Exception("PRINTER_IP not set")
        # A failed slice leaves the PREVIOUS build's model.gcode on disk —
        # orca_slice_sync returns False without clearing its target. Uploading
        # it here with print=true would start a print of the wrong object on a
        # real machine. Only PRINTER_IP being unset has been hiding this.
        if not slice_ok:
            raise Exception("slice failed — refusing to print a stale gcode "
                            "left over from an earlier build")
        # The review gate is a hard stop for the printer, not advice. A build
        # that failed it still produces its STL and gcode so a human can look
        # at them; what it does not get is an automatic start on a real machine.
        if review["verdict"] == "needs_review":
            raise Exception("review gate: " + "; ".join(review["issues"]))
        with open(gcode_path, "rb") as f:
            up = moonraker_request(
                "POST", "/server/files/upload",
                files={"file": ("model.gcode", f, "application/octet-stream")},
                data={"root": "gcodes", "print": "true"},
                timeout=120,
            )
        up.raise_for_status()
        engineer_log("Moonraker: print started", "success")
    except Exception as e:
        # The reason matters — "printer is off" and "wrong API key" need
        # different fixes, and the old message claimed neither.
        engineer_log(f"Moonraker dispatch failed ({e}) — gcode saved to "
                     "output/model.gcode for USB transfer or browser download", "warning")

    engineer_set_step(5)
    engineer_log("Step 5 — design review", "info")
    review_design(scad_script, specs)

    def _engineer_supabase_backup():
        try:
            stl_url = upload_to_supabase(stl_path, folder="engineer/stl")
            if stl_url and model_id:
                # model_id is the local SQLite id; engineer models have no row in
                # the Supabase models table, so it goes in metadata (FK stays NULL).
                log_supabase_event(
                    "engineer_model_generated",
                    message=intent,
                    metadata={"local_model_id": model_id, "stl_url": stl_url},
                )
        except Exception as e:
            print(f"[Supabase] Engineer backup error: {e}")

    threading.Thread(target=_engineer_supabase_backup, daemon=True).start()

    if review["verdict"] == "needs_review":
        engineer_set_status("NEEDS_REVIEW")
        engineer_log("=== ENGINEER PIPELINE FINISHED — NEEDS REVIEW ===",
                     "warning")
    else:
        engineer_set_status("COMPLETE")
        engineer_log("=== ENGINEER PIPELINE COMPLETE ===", "success")


@app.post("/api/engineer/trigger")
async def engineer_trigger(body: dict) -> JSONResponse:
    intent = body.get("intent", "").strip()
    if not intent:
        return JSONResponse({"ok": False, "error": "intent is required"}, status_code=400)
    with engineer_state["lock"]:
        if engineer_state["status"] == "RUNNING":
            return JSONResponse({"error": "Engineer pipeline already running"}, status_code=409)
    threading.Thread(target=run_engineer_pipeline, args=(intent,), daemon=True).start()
    return JSONResponse({"ok": True, "status": "started", "intent": intent})


@app.get("/api/engineer/status")
def engineer_status() -> JSONResponse:
    with engineer_state["lock"]:
        return JSONResponse({
            "status":            engineer_state["status"],
            "current_step":      engineer_state["current_step"],
            "current_step_name": engineer_state["current_step_name"],
            "logs":              list(engineer_state["logs"]),
            "scad_script":       engineer_state["scad_script"],
            "specs":             engineer_state["specs"],
            "design_review":     engineer_state["design_review"],
            "slice_ok":          engineer_state["slice_ok"],
            "review":            engineer_state["review"],
        })


@app.post("/api/engineer/reset")
def engineer_reset() -> JSONResponse:
    with engineer_state["lock"]:
        engineer_state["status"] = "IDLE"
        engineer_state["current_step"] = 0
        engineer_state["current_step_name"] = ""
        engineer_state["logs"] = []
        engineer_state["scad_script"] = None
        engineer_state["scad_original"] = None
        engineer_state["specs"] = None
        engineer_state["design_review"] = None
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001, reload=False)
