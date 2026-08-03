import os
import re
import math
import json
import time
import logging
import sqlite3
import asyncio
import uuid
import queue
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

import requests
import aiofiles
import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks, HTTPException
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

# ── FalkorDB graph memory (optional; the kiosk runs fine without it) ───────
# Every Engineer build is recorded as a graph so later builds can look up what
# earlier ones already worked out. FALKORDB_URL overrides host+port outright,
# the same way MOONRAKER_URL does, for a connection string or TLS endpoint.
FALKORDB_HOST        = os.getenv("FALKORDB_HOST", "")
FALKORDB_PORT        = int(os.getenv("FALKORDB_PORT", "6379"))
FALKORDB_USERNAME    = os.getenv("FALKORDB_USERNAME", "")
FALKORDB_PASSWORD    = os.getenv("FALKORDB_PASSWORD", "")
FALKORDB_URL         = os.getenv("FALKORDB_URL", "")
FALKORDB_GRAPH       = os.getenv("FALKORDB_GRAPH", "conjure")
FALKORDB_SSL         = os.getenv("FALKORDB_SSL", "").strip().lower() in ("1", "true", "yes")
# The FalkorDB constructor opens the socket eagerly — it does not defer until
# the first query. Without an explicit timeout an unreachable host blocks on
# the OS default (~75 s), which would stall the generation pipeline that this
# whole layer is supposed to stay out of the way of. Keep it short.
FALKORDB_TIMEOUT     = float(os.getenv("FALKORDB_TIMEOUT", "5"))
# Prompt-similarity score above which a prior build's design brief is reused
# instead of asking the LLM again. 1.0 disables reuse while still logging hits.
FALKORDB_REUSE_AT    = float(os.getenv("FALKORDB_REUSE_AT", "0.6"))

# ── Linkup live research (optional) ───────────────────────────────────────
# Consulted only when graph memory has nothing close enough to reuse, so it
# costs nothing on a repeat build. "standard" is ~$0.006 a call; "deep" is
# nearly ten times that and rarely changes the answer for a physical part.
LINKUP_API_KEY       = os.getenv("LINKUP_API_KEY", "")
LINKUP_DEPTH         = os.getenv("LINKUP_DEPTH", "standard")
LINKUP_TIMEOUT       = float(os.getenv("LINKUP_TIMEOUT", "25"))
LINKUP_MAX_SOURCES   = int(os.getenv("LINKUP_MAX_SOURCES", "5"))

LASER_CONNECTION_STRING = os.getenv("LASER_CONNECTION_STRING", "")
LASER_STREAM         = os.getenv("LASER_STREAM", "conjure")
LASER_TOPIC          = os.getenv("LASER_TOPIC", "pipeline-events")
LASER_TIMEOUT        = float(os.getenv("LASER_TIMEOUT", "10"))
LASER_QUEUE_MAX      = int(os.getenv("LASER_QUEUE_MAX", "500"))

ROCKETRIDE_ENABLED   = os.getenv("ROCKETRIDE_ENABLED", "").strip().lower() in ("1", "true", "yes")
ROCKETRIDE_URI       = os.getenv("ROCKETRIDE_URI", "")
ROCKETRIDE_APIKEY    = os.getenv("ROCKETRIDE_APIKEY", "")
ROCKETRIDE_TIMEOUT   = float(os.getenv("ROCKETRIDE_TIMEOUT", "45"))

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
# FalkorDB graph memory
# ---------------------------------------------------------------------------
# Records every Engineer build as a graph so a later build can reuse what an
# earlier one already worked out, instead of paying for the same design brief
# twice.
#
# Strictly additive. Nothing in generation, slicing or printing depends on any
# of this: every entry point below returns None/[] when FalkorDB is missing,
# unreachable, unauthenticated or simply broken, and each caller is written to
# carry on without it. The kiosk's job is to print things, not to have a graph.

_FALKOR_STOPWORDS = {
    "a", "an", "the", "and", "or", "for", "to", "of", "with", "that", "this",
    "it", "is", "my", "me", "our", "your", "i", "in", "on", "at", "as", "be",
    "can", "make", "made", "print", "printed", "printable", "3d", "model",
    "part", "please", "one", "some", "need", "want", "would", "like",
}


def _falkor_tokens(text: str) -> set:
    """The content words of a prompt, for overlap scoring."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) > 1 and w not in _FALKOR_STOPWORDS}


def _falkor_similarity(a: str, b: str) -> float:
    """How much two prompts have in common, 0.0-1.0.

    Deliberately not an embedding. The graph is the substance of this feature,
    and a set intersection needs no model, no API key and no credit — so prompt
    matching keeps working on days when the LLM budget does not.

    Scored on containment rather than plain Jaccard. Jaccard divides by the
    union, so "a gearbox with a crank" against a stored prompt that spells out
    the sun gear, the carrier and the ring gear scores badly purely because the
    stored one says more — real queries are short and stored prompts are long,
    so nothing ever cleared the reuse threshold. Containment asks the question
    that actually matters: is one request essentially a subset of the other?

    The guard stops it degenerating: a single shared word between a two-word
    query and anything at all would otherwise score 1.0, so below two shared
    words this falls back to Jaccard, which stays near zero.
    """
    ta, tb = _falkor_tokens(a), _falkor_tokens(b)
    if not ta or not tb:
        return 0.0
    shared = len(ta & tb)
    if shared == 0:
        return 0.0
    jaccard = shared / len(ta | tb)
    if shared < 2:
        return jaccard
    return max(jaccard, shared / min(len(ta), len(tb)))


def get_falkordb_client():
    """A FalkorDB handle, or None. Never raises.

    Mirrors get_supabase_client(): being unconfigured is the default, not an
    error. Two deliberate differences, both forced by how this client behaves:

     - the import is deferred rather than top-level, so a missing package
       degrades to "no graph memory" instead of refusing to boot the kiosk;
     - the timeouts are mandatory. FalkorDB() opens its socket eagerly, so an
       unreachable host blocks in the constructor on the OS default (~75 s)
       rather than failing at query time.
    """
    if not FALKORDB_HOST and not FALKORDB_URL:
        return None
    try:
        from falkordb import FalkorDB
    except ImportError:
        log.warning("[FalkorDB] configured but the client is not installed "
                    "— pip install FalkorDB")
        return None
    try:
        kw = {"socket_connect_timeout": FALKORDB_TIMEOUT,
              "socket_timeout": FALKORDB_TIMEOUT}
        if FALKORDB_URL:
            return FalkorDB.from_url(FALKORDB_URL, **kw)
        if FALKORDB_USERNAME:
            kw["username"] = FALKORDB_USERNAME
        if FALKORDB_PASSWORD:
            kw["password"] = FALKORDB_PASSWORD
        if FALKORDB_SSL:
            kw["ssl"] = True
        return FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT, **kw)
    except Exception as e:
        log.warning("[FalkorDB] Client init failed: %s", e)
        return None


def _falkor_graph():
    """The kiosk's graph, or None."""
    db = get_falkordb_client()
    if not db:
        return None
    try:
        return db.select_graph(FALKORDB_GRAPH)
    except Exception as e:
        log.warning("[FalkorDB] select_graph(%r) failed: %s", FALKORDB_GRAPH, e)
        return None


def save_build_to_graph(
    prompt: str,
    specs: dict | None,
    material: str = "PLA",
    dimensions: dict | None = None,
) -> str | None:
    """Record one finished build as (Build)-[:USES]->(Material) + specs.

    Returns the new build id, or None if nothing was written. Called alongside
    the existing SQLite and Supabase saves, never instead of them.
    """
    g = _falkor_graph()
    if not g:
        return None

    specs = specs or {}
    # Every numeric the design brief settled on becomes a Spec node, so two
    # builds that agreed on a wall thickness are visibly connected through it.
    dims = dimensions or {k: specs[k] for k in
                          ("od_mm", "wall_mm", "clearance_mm") if k in specs}
    build_id = uuid.uuid4().hex[:12]

    def _num(key):
        try:
            return float(specs[key])
        except (KeyError, TypeError, ValueError):
            return None

    try:
        g.query(
            """
            MERGE (m:Material {name: $material})
              ON CREATE SET m.tensile_strength = $tensile, m.source = $msource
            CREATE (b:Build {
                id: $id, prompt: $prompt, timestamp: $ts, material: $material,
                dimensions: $dims_json, summary: $summary,
                od_mm: $od, wall_mm: $wall, clearance_mm: $clearance
            })
            CREATE (b)-[:USES]->(m)
            """,
            params={
                "id": build_id,
                "prompt": prompt or "",
                "ts": datetime.now().isoformat(timespec="seconds"),
                "material": material,
                "tensile": specs.get("tensile_mpa", PLA_TENSILE_MPA),
                # Named honestly. These are compiled-in constants, not a figure
                # scraped from a datasheet, and the graph should not imply
                # provenance the number does not have.
                "msource": "kiosk-constant",
                "dims_json": json.dumps(dims, sort_keys=True),
                "summary": specs.get("summary", ""),
                "od": _num("od_mm"),
                "wall": _num("wall_mm"),
                "clearance": _num("clearance_mm"),
            },
        )

        for dim_type, value in dims.items():
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            g.query(
                """
                MATCH (b:Build {id: $id})
                MERGE (s:Spec {dimension_type: $dt, value: $v, unit: $unit})
                  ON CREATE SET s.source_url = $src
                MERGE (b)-[:HAS_SPEC]->(s)
                """,
                params={"id": build_id, "dt": dim_type, "v": value,
                        "unit": "mm", "src": "llm:design_brief"},
            )

        # Link to prior builds sharing a material, or landing within 20% on the
        # main dimension. This is what makes the store a graph rather than a
        # table: the demo query walks these edges.
        rel = g.query(
            """
            MATCH (b:Build {id: $id}), (o:Build)
            WHERE o.id <> $id
              AND (o.material = $material
                   OR ($od IS NOT NULL AND o.od_mm IS NOT NULL
                       AND abs(o.od_mm - $od) <= $tol))
            MERGE (b)-[:SIMILAR_TO]->(o)
            """,
            params={"id": build_id, "material": material, "od": _num("od_mm"),
                    "tol": (_num("od_mm") or 0.0) * 0.2},
        )
        log.info("[FalkorDB] Build %s saved (%d SIMILAR_TO edges)",
                 build_id, rel.relationships_created)
        return build_id
    except Exception as e:
        log.warning("[FalkorDB] save_build_to_graph failed: %s", e)
        return None


def query_similar_builds(prompt: str, specs: dict | None = None,
                         limit: int = 5) -> list:
    """Prior builds resembling this request, best match first.

    Scoring happens in Python rather than Cypher on purpose: the graph read is
    a cheap bounded scan, and keeping the ranking here means the threshold can
    be tuned without a query rewrite. Returns [] on any failure.
    """
    g = _falkor_graph()
    if not g:
        return []
    try:
        rows = g.ro_query(
            """
            MATCH (b:Build)
            OPTIONAL MATCH (b)-[:USES]->(m:Material)
            RETURN b.id, b.prompt, b.summary, b.timestamp, b.dimensions,
                   b.od_mm, b.wall_mm, b.clearance_mm, m.name
            ORDER BY b.timestamp DESC
            LIMIT 200
            """
        ).result_set
    except Exception as e:
        log.warning("[FalkorDB] query_similar_builds failed: %s", e)
        return []

    out = []
    for r in rows:
        score = _falkor_similarity(prompt, r[1])
        # A shared main dimension is corroborating evidence, not proof on its
        # own — it nudges the score rather than setting it.
        if specs and r[5] is not None:
            try:
                want = float(specs.get("od_mm"))
                if want and abs(float(r[5]) - want) <= want * 0.2:
                    score = min(1.0, score + 0.15)
            except (TypeError, ValueError):
                pass
        if score <= 0:
            continue
        out.append({
            "id": r[0], "prompt": r[1], "summary": r[2] or "",
            "timestamp": r[3], "dimensions": r[4], "material": r[8] or "",
            "score": round(score, 3),
            "specs": {
                "od_mm": r[5], "wall_mm": r[6], "clearance_mm": r[7],
                "summary": r[2] or "", "tensile_mpa": PLA_TENSILE_MPA,
                "safety_factor": DESIGN_SAFETY_FACTOR,
                "inner_r": round(float(r[5]) / 2 + 0.4, 4) if r[5] else None,
            },
        })
    out.sort(key=lambda d: d["score"], reverse=True)
    return out[:limit]


def falkordb_status() -> dict:
    """Reachability for /api/health. Total — never raises."""
    if not FALKORDB_HOST and not FALKORDB_URL:
        return {"configured": False, "online": False,
                "reason": "FALKORDB_HOST or FALKORDB_URL not set in .env"}
    db = get_falkordb_client()
    if not db:
        return {"configured": True, "online": False,
                "reason": "client init failed — see server log"}
    try:
        graphs = db.list_graphs()
        builds = 0
        try:
            rs = db.select_graph(FALKORDB_GRAPH).ro_query(
                "MATCH (b:Build) RETURN count(b)").result_set
            builds = rs[0][0] if rs else 0
        except Exception:
            # A graph that has never been written to does not exist yet, and
            # asking for its node count is an error, not an outage.
            pass
        return {"configured": True, "online": True, "graph": FALKORDB_GRAPH,
                "graphs": graphs, "builds": builds}
    except Exception as e:
        return {"configured": True, "online": False, "reason": str(e)[:200]}


# ---------------------------------------------------------------------------
# Snyk — last recorded security scan
#
# This reads the artifacts ./snyk_scan.sh left on disk. It never invokes the
# Snyk CLI and never contacts api.snyk.io: a health endpoint that shelled out
# to a network scanner would take tens of seconds and would fail whenever the
# network did, which is the opposite of what a health check is for. So this
# reports the last real scan, and says plainly how old it is rather than
# implying the state is current.
#
# The whole module is written so that "we do not know" never renders as "clean".
# ---------------------------------------------------------------------------
SNYK_DIR = OUTPUT_DIR / "snyk"

# The CLI's documented exit codes, which are the actual source of truth here.
_SNYK_EXIT = {
    0: ("clean", "no issues found"),
    1: ("issues_found", "vulnerabilities found"),
    2: ("failed", "scan did not complete"),
    3: ("no_projects", "no supported projects detected"),
}


def _snyk_read_json(name: str):
    """Parse one artifact, or None. Never raises."""
    path = SNYK_DIR / name
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as e:
        log.warning("[Snyk] could not read %s: %s", name, e)
        return None


def _snyk_severity_counts(items, key) -> dict:
    """Tally severities, tolerating unexpected shapes."""
    out: dict = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        sev = str(item.get(key) or "unknown").lower()
        out[sev] = out.get(sev, 0) + 1
    return out


def _snyk_deps_detail() -> dict:
    """Headline numbers from `snyk test --json-file-output`.

    Documented shape is a single object with ok/vulnerabilities/dependencyCount,
    but --all-projects emits a list of those, so both are handled.
    """
    doc = _snyk_read_json("deps.json")
    if doc is None:
        return {}
    docs = doc if isinstance(doc, list) else [doc]
    vulns, deps = [], 0
    for d in docs:
        if not isinstance(d, dict):
            continue
        v = d.get("vulnerabilities")
        if isinstance(v, list):
            vulns.extend(v)
        if isinstance(d.get("dependencyCount"), int):
            deps += d["dependencyCount"]
    detail = {"severities": _snyk_severity_counts(vulns, "severity")}
    if deps:
        detail["dependencies_scanned"] = deps
    # uniqueCount counts distinct issues; len(vulnerabilities) counts paths to
    # them, so one CVE reached two ways shows up twice. Prefer the former.
    uniq = next((d.get("uniqueCount") for d in docs
                 if isinstance(d, dict) and isinstance(d.get("uniqueCount"), int)), None)
    detail["issues"] = uniq if uniq is not None else len(vulns)
    return detail


def _snyk_code_detail() -> dict:
    """Headline numbers from `snyk code test --json-file-output`.

    Snyk's own docs distinguish --json from --sarif but do not publish the JSON
    schema, and this has not yet been checked against a real authenticated run.
    So try SARIF, then the open-source shape, and if neither matches say so
    instead of reporting zero — an unrecognised file is not an empty one.
    """
    doc = _snyk_read_json("code.json")
    if doc is None:
        return {}
    if isinstance(doc, dict) and isinstance(doc.get("runs"), list):
        results = []
        for run in doc["runs"]:
            if isinstance(run, dict) and isinstance(run.get("results"), list):
                results.extend(run["results"])
        return {"issues": len(results),
                "severities": _snyk_severity_counts(results, "level"),
                "format": "sarif"}
    if isinstance(doc, dict) and isinstance(doc.get("vulnerabilities"), list):
        return {"issues": len(doc["vulnerabilities"]),
                "severities": _snyk_severity_counts(doc["vulnerabilities"], "severity"),
                "format": "snyk"}
    return {"parsed": False,
            "reason": "unrecognised code.json shape — counts unavailable"}


def snyk_status() -> dict:
    """The last recorded scan. Total — never raises, never calls Snyk."""
    summary = _snyk_read_json("summary.json")
    if not isinstance(summary, dict) or "dependencies_exit" not in summary:
        return {"scanned": False,
                "reason": "no scan recorded — run ./snyk_scan.sh"}

    scanned_at = str(summary.get("scanned_at") or "")
    age_hours = None
    try:
        # Written as ...Z by date(1); fromisoformat only learned Z in 3.11.
        stamp = datetime.fromisoformat(scanned_at.replace("Z", "+00:00"))
        age_hours = round(
            (datetime.now(timezone.utc) - stamp).total_seconds() / 3600, 1)
    except Exception:
        pass

    def leg(exit_key: str, detail: dict) -> dict:
        rc = summary.get(exit_key)
        state, note = _SNYK_EXIT.get(rc, ("unknown", f"unexpected exit {rc}"))
        out = {"state": state, "note": note}
        # Only decorate a scan that actually ran. Attaching counts to a failed
        # scan would dress up "we do not know" as "we found nothing".
        if rc in (0, 1):
            out.update(detail)
        return out

    return {
        "scanned": True,
        "scanned_at": scanned_at,
        "age_hours": age_hours,
        "snyk_version": summary.get("snyk_version") or None,
        "dependencies": leg("dependencies_exit", _snyk_deps_detail()),
        "code": leg("code_exit", _snyk_code_detail()),
    }


# ---------------------------------------------------------------------------
# LaserData — pipeline telemetry over Apache Iggy
#
# Every stage transition and log line from the Engineer pipeline is published
# to a Laser topic, so the physical progress of a print is a stream you can
# consume rather than something buried in this process's memory.
#
# ⚠ THE PUBLISH PATH IS UNVERIFIED. Everything below the queue was written
# against the real laser-sdk API — signatures were read off the installed
# package, not guessed — but no message has ever been sent, because LaserData
# Cloud is in private preview and the documented local target (laser-stack)
# needs Docker, which is not installed here. Apache Iggy also ships no macOS
# binaries, so there is no falkordblite-style local engine to test against.
# What IS verified is the part that protects the kiosk: with no connection
# string configured, none of this runs at all. Treat a successful publish as
# untested until someone watches a record land.
#
# Two things drove the design:
#
#   1. Laser.connect() takes no timeout argument — checked against the real
#      signature. Pointed at a black-holed host it blocked past 30s with no
#      sign of giving up. Since the API is async, asyncio.wait_for() supplies
#      the bound the SDK does not, and that wrapper is load-bearing: without
#      it a dead broker parks a thread forever.
#
#   2. Emission must never apply backpressure to a print. So callers only ever
#      touch a bounded queue, and a full queue drops the event rather than
#      waiting. Telemetry that can stall the machine it is measuring is worse
#      than no telemetry.
# ---------------------------------------------------------------------------
_laser_queue: "queue.Queue | None" = None
_laser_started = False
_laser_lock = threading.Lock()
_laser_stats = {"published": 0, "dropped": 0, "failed": 0,
                "connected": False, "reason": "not started"}


def _laser_set(**kw) -> None:
    with _laser_lock:
        _laser_stats.update(kw)


async def _laser_run(q) -> None:
    """Connect once, then drain the queue until the process ends."""
    import laser_sdk as ls

    # The one call that can hang. Everything else is cheap once connected.
    laser = await asyncio.wait_for(
        ls.Laser.connect(LASER_CONNECTION_STRING, stream=LASER_STREAM),
        timeout=LASER_TIMEOUT,
    )
    topic = laser.topic(LASER_TOPIC)
    try:
        # Idempotent; a topic that already exists is not an error.
        await asyncio.wait_for(topic.ensure(1), timeout=LASER_TIMEOUT)
    except Exception as e:
        log.info("[LaserData] topic.ensure skipped: %s", e)
    _laser_set(connected=True, reason="")
    log.info("[LaserData] connected — publishing to %s/%s",
             LASER_STREAM, LASER_TOPIC)

    while True:
        # Blocking get, moved off the event loop so the loop is free to run
        # the publish that follows.
        event = await asyncio.to_thread(q.get)
        if event is None:
            return
        try:
            await asyncio.wait_for(topic.publish(event).send(),
                                   timeout=LASER_TIMEOUT)
            with _laser_lock:
                _laser_stats["published"] += 1
        except Exception as e:
            # One bad record must not kill the drain loop, or the first
            # hiccup silently ends telemetry for the rest of the session.
            with _laser_lock:
                _laser_stats["failed"] += 1
                _laser_stats["reason"] = f"{type(e).__name__}: {str(e)[:120]}"


def _laser_worker(q) -> None:
    t_start = time.monotonic()
    try:
        asyncio.run(_laser_run(q))
    except asyncio.TimeoutError:
        # asyncio.TimeoutError IS builtins.TimeoutError on 3.11+, which the
        # socket layer also raises on a refused connection. Blaming the budget
        # for a failure that took a fraction of it points at the wrong cause,
        # so report the elapsed time and only call it a timeout if it was one.
        el = time.monotonic() - t_start
        if el >= LASER_TIMEOUT * 0.95:
            reason = f"connect timed out after {el:.1f}s (budget {LASER_TIMEOUT}s)"
        else:
            reason = (f"connection refused or dropped after {el:.2f}s "
                      f"— not the {LASER_TIMEOUT}s budget")
        _laser_set(connected=False, reason=reason)
        log.warning("[LaserData] %s — telemetry off, pipeline unaffected", reason)
    except ImportError:
        _laser_set(connected=False, reason="laser-sdk not installed")
        log.warning("[LaserData] configured but the SDK is missing "
                    "— pip install laser-sdk")
    except Exception as e:
        _laser_set(connected=False,
                   reason=f"{type(e).__name__}: {str(e)[:150]}")
        log.warning("[LaserData] worker stopped (%s): %s", type(e).__name__, e)


def emit_pipeline_event(stage: str, status: str,
                        metadata: dict | None = None) -> None:
    """Queue one pipeline event. Never blocks, never raises, never prints.

    Safe to call from anywhere in the pipeline, including inside the engineer
    state lock: the only work done on the caller's thread is building a dict
    and a non-blocking put.
    """
    if not LASER_CONNECTION_STRING:
        return
    global _laser_queue, _laser_started
    try:
        with _laser_lock:
            if not _laser_started:
                _laser_queue = queue.Queue(maxsize=LASER_QUEUE_MAX)
                threading.Thread(target=_laser_worker, args=(_laser_queue,),
                                 daemon=True, name="laserdata").start()
                _laser_started = True
                _laser_stats["reason"] = "connecting"
            q = _laser_queue
        event = {
            "stage": stage,
            "status": status,
            "ts": datetime.now(timezone.utc).isoformat(),
            "source": "conjure-kiosk",
        }
        if metadata:
            event["metadata"] = metadata
        try:
            q.put_nowait(event)
        except queue.Full:
            # Dropping is the correct failure here — see note 2 above.
            with _laser_lock:
                _laser_stats["dropped"] += 1
    except Exception:
        # This function is called from the hot path of a physical machine.
        # It has no business raising, whatever went wrong.
        pass


def laserdata_status() -> dict:
    """For /api/health. Total — never raises."""
    if not LASER_CONNECTION_STRING:
        return {"configured": False, "connected": False,
                "reason": "LASER_CONNECTION_STRING not set in .env"}
    with _laser_lock:
        s = dict(_laser_stats)
    s["configured"] = True
    s["stream"] = LASER_STREAM
    s["topic"] = LASER_TOPIC
    s["publish_verified"] = False  # see the module note above
    return s


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
    log.info("Graph memory: %s", "configured" if FALKORDB_HOST or FALKORDB_URL else "not configured")
    log.info("Linkup research: %s", "configured" if LINKUP_API_KEY else "not configured")
    log.info("LaserData telemetry: %s", "configured (publish path unverified)"
             if LASER_CONNECTION_STRING else "not configured")
    log.info("RocketRide parallel path: %s", "ENABLED (unverified, standalone "
             "endpoint only)" if ROCKETRIDE_ENABLED else "off — original pipeline")


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
# unconstrained value lets a caller steer the request path (Snyk SSRF finding,
# main.py:2746). Starlette already refuses `/` in a path param, so the host
# cannot be changed — this closes the remaining `..` traversal within Meshy.
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


@app.get("/api/memory/similar")
def api_memory_similar(prompt: str = "", limit: int = 5) -> JSONResponse:
    """Prior builds resembling `prompt`, for debugging and live demo.

    Read-only and non-destructive: it reports what the graph already knows and
    writes nothing. Returns 200 with an empty list when FalkorDB is unavailable
    — the caller wants to know there are no matches, and "the memory layer is
    switched off" is a legitimate reason for that rather than a server error.
    """
    if not prompt.strip():
        raise HTTPException(400, "Pass ?prompt=... to search prior builds")
    status = falkordb_status()
    matches = query_similar_builds(prompt, limit=max(1, min(limit, 50)))
    return JSONResponse({
        "prompt": prompt,
        "configured": status["configured"],
        "online": status["online"],
        "reuse_threshold": FALKORDB_REUSE_AT,
        "would_reuse": bool(matches and matches[0]["score"] >= FALKORDB_REUSE_AT),
        "count": len(matches),
        "matches": matches,
    })


@app.post("/api/print-now")
def api_print_now() -> JSONResponse:
    """Uploads the sliced gcode to the printer and starts it."""
    gcode = OUTPUT_DIR / "model.gcode"
    if not gcode.exists():
        raise HTTPException(400, "No gcode yet — slice the model first")

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
    pipeline_state["status"] = "slicing"
    pipeline_state["error"]  = None
    background_tasks.add_task(run_slicing)
    return JSONResponse({"status": "started"})


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
            "linkup":      bool(LINKUP_API_KEY),
        },
        "supabase_configured": bool(SUPABASE_URL and SUPABASE_ANON_KEY),
        "supabase_connected":  sb_ok,
        "llm_usage":  llm_usage_summary(),
        "printer":    moonraker_status(),
        "graph_memory": falkordb_status(),
        "security_scan": snyk_status(),
        "telemetry": laserdata_status(),
        "rocketride": rocketride_status(),
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
    "specs": None,
    "design_review": None,
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
    # Mirrored to LaserData. Emitting from inside here rather than at the ~50
    # call sites means no existing line changes and none can be missed; it is
    # a no-op when LASER_CONNECTION_STRING is unset.
    emit_pipeline_event("log", level, {"message": message})


def engineer_set_step(n: int) -> None:
    with engineer_state["lock"]:
        engineer_state["current_step"] = n
        engineer_state["current_step_name"] = ENGINEER_STEP_NAMES[n] if n < len(ENGINEER_STEP_NAMES) else ""
    name = ENGINEER_STEP_NAMES[n] if n < len(ENGINEER_STEP_NAMES) else ""
    emit_pipeline_event("step", "started", {"step": n, "name": name})


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

Rules:
 - Open with named parameter variables, then modules, then one top-level call.
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


# ---------------------------------------------------------------------------
# Linkup — live web research for parts we have no memory of
# ---------------------------------------------------------------------------
# Sits between the two things that already exist: graph memory answers "have we
# built this before", and Claude answers "how big should it be". Linkup covers
# the gap where the answer is a real-world measurement neither of them knows —
# the width of a specific phone, the pitch of a standard thread.
#
# Only consulted on a memory miss, so a repeat build costs nothing. Same
# contract as every other optional service here: returns None rather than
# raising, and the pipeline runs unchanged without it.


def get_linkup_client():
    """A Linkup client, or None. Never raises.

    Unlike FalkorDB, the constructor does no network I/O, so there is no
    eager-connect trap here — but it *does* raise ValueError when handed no
    key, which is why the guard runs before construction rather than relying
    on the try block.
    """
    if not LINKUP_API_KEY:
        return None
    try:
        from linkup import LinkupClient
    except ImportError:
        log.warning("[Linkup] configured but the client is not installed "
                    "— pip install linkup-sdk")
        return None
    try:
        return LinkupClient(api_key=LINKUP_API_KEY)
    except Exception as e:
        log.warning("[Linkup] Client init failed: %s", e)
        return None


def research_with_linkup(query: str) -> dict | None:
    """Real measurements for `query`, or None if unavailable.

    Asks for a sourced answer rather than raw results: the design brief needs
    a number it can act on, and handing Claude ten page snippets to re-read is
    slower, dearer and less accurate than letting Linkup compose the answer.
    """
    client = get_linkup_client()
    if not client:
        return None

    # Steer it at dimensions. Left as the bare user prompt it returns shopping
    # listings for the finished object, which is the wrong half of the web.
    question = (
        f"What are the real-world dimensions, standard sizes and material "
        f"specifications needed to design and 3D print this: {query}. "
        f"Answer with specific measurements in millimetres where they exist."
    )
    try:
        result = client.search(
            query=question,
            depth=LINKUP_DEPTH,
            output_type="sourcedAnswer",
            timeout=LINKUP_TIMEOUT,
        )
    except Exception as e:
        # Deliberately one handler. The SDK raises a dozen distinct types
        # (auth, credit, timeout, rate limit) and every one of them means the
        # same thing here: carry on without research.
        log.warning("[Linkup] research failed (%s): %s", type(e).__name__, e)
        return None

    answer = (getattr(result, "answer", "") or "").strip()
    if not answer:
        log.info("[Linkup] returned no answer for %r", query[:60])
        return None
    sources = []
    for s in (getattr(result, "sources", None) or [])[:LINKUP_MAX_SOURCES]:
        sources.append({
            "name": getattr(s, "name", "") or "",
            "url": getattr(s, "url", "") or "",
            "snippet": (getattr(s, "snippet", "") or "")[:400],
        })
    log.info("[Linkup] %d chars, %d sources for %r",
             len(answer), len(sources), query[:60])
    return {"query": question, "answer": answer, "sources": sources}


def design_brief(intent: str, research: dict | None = None) -> dict:
    """Sizes the part before any geometry exists.

    `research` is optional live-web context from Linkup. When absent this
    behaves exactly as it always has — the parameter defaults to None so the
    no-Linkup path is not merely supported, it is the same code.
    """
    user = intent
    if research and research.get("answer"):
        user = (f"{intent}\n\n"
                f"Web research on real-world dimensions for this part:\n"
                f"{research['answer']}\n\n"
                f"Prefer these measured figures over your own estimate where "
                f"they apply, but keep every value printable on FDM.")
    out = _llm_json(_BRIEF_SYSTEM, user, _BRIEF_SCHEMA, "DesignBrief")
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


# ---------------------------------------------------------------------------
# RocketRide — parallel design-brief path (OFF unless ROCKETRIDE_ENABLED)
#
# This is deliberately a *sibling* of design_brief(), not a modification of it.
# design_brief() above is untouched and remains the only thing the Engineer
# pipeline calls. Nothing here is reachable from run_engineer_pipeline; the one
# entry point is POST /api/rocketride/design-brief, which exists so the path
# can be exercised standalone and compared against the real brief before anyone
# considers wiring it in. If it never works, the flag stays off and the kiosk
# is byte-for-byte the kiosk it is today.
#
# ⚠ UNVERIFIED AGAINST A REAL SERVER. The API shape was read off the installed
# rocketride 1.3.0 package rather than guessed — use()/send()/terminate() are
# real coroutines with the signatures used below — but no pipeline has ever
# been executed, because that needs an API key for cloud.rocketride.ai or a
# self-hosted engine (Docker, not installed here). Verified: it stays dormant
# when disabled, and it fails closed rather than hanging when it cannot reach
# a server. Treat a successful run as untested.
#
# Two findings drove this code:
#
#   1. request_timeout and max_retry_time do NOT bound connection setup. With
#      both set to 3s against a black-holed host the client was still blocked
#      at 25s. That is worse than having no timeout parameter, because the
#      parameters read as if the problem is handled. asyncio.wait_for is what
#      actually bounds it.
#
#   2. RocketRide's own docs warn that a bare host:port URI silently downgrades
#      to unencrypted ws://. Since the API key travels over that socket, a typo
#      in .env would leak it in cleartext. _rocketride_uri() refuses to do that
#      for anything that is not localhost.
# ---------------------------------------------------------------------------
_ROCKETRIDE_BRIEF_PIPELINE = {
    "description": "Conjure design brief — sizes a printable part from intent",
    "version": 1,
    "source": "in",
    "components": [
        {"id": "in", "provider": "webhook", "config": {},
         "name": "Intent"},
        {"id": "brief", "provider": "ai_chat",
         "name": "Design brief",
         "config": {"system": _BRIEF_SYSTEM, "response_format": "json"},
         "input": [{"from": "in"}]},
        {"id": "out", "provider": "response", "config": {},
         "input": [{"from": "brief"}]},
    ],
}


def rocketride_configured() -> bool:
    return bool(ROCKETRIDE_ENABLED and ROCKETRIDE_URI and ROCKETRIDE_APIKEY)


def _rocketride_uri() -> str:
    """Normalise the URI, refusing a silent downgrade to cleartext."""
    uri = ROCKETRIDE_URI.strip()
    if not uri:
        return ""
    local = uri.startswith(("ws://localhost", "ws://127.0.0.1",
                            "http://localhost", "http://127.0.0.1"))
    if local:
        return uri
    if uri.startswith(("wss://", "https://")):
        return uri
    if uri.startswith(("ws://", "http://")):
        raise ValueError(
            f"ROCKETRIDE_URI={uri!r} would send the API key over an "
            f"unencrypted socket — use wss:// or https:// for a remote engine")
    # A bare host:port is the case their docs call out as silently downgrading.
    return "wss://" + uri


async def _rocketride_brief(intent: str) -> dict:
    from rocketride import RocketRideClient

    uri = _rocketride_uri()
    async with RocketRideClient(uri=uri, auth=ROCKETRIDE_APIKEY,
                                request_timeout=ROCKETRIDE_TIMEOUT) as client:
        started = await client.use(pipeline=_ROCKETRIDE_BRIEF_PIPELINE)
        token = started.get("token")
        if not token:
            raise RuntimeError(f"no task token in use() response: {started!r}")
        try:
            return await client.send(token, intent, mimetype="text/plain")
        finally:
            # Server-side tasks outlive the socket, so a leaked token is a
            # leaked resource on someone else's machine.
            try:
                await client.terminate(token)
            except Exception:
                pass


def rocketride_design_brief(intent: str) -> dict | None:
    """Run the brief through RocketRide. None on any failure. Never raises.

    Synchronous by design so it is a drop-in shape-match for design_brief();
    the async client is confined to its own event loop inside this call.
    """
    if not rocketride_configured():
        return None
    t_start = time.monotonic()
    try:
        return asyncio.run(
            asyncio.wait_for(_rocketride_brief(intent),
                             timeout=ROCKETRIDE_TIMEOUT))
    except asyncio.TimeoutError:
        # asyncio.TimeoutError IS builtins.TimeoutError on 3.11+, and the SDK
        # raises that for a refused connection too. Reporting the configured
        # budget unconditionally therefore claimed a 45s timeout for a failure
        # that took 0.46s, which sent me looking in entirely the wrong place.
        # Report what actually elapsed and let the number tell the story.
        el = time.monotonic() - t_start
        if el >= ROCKETRIDE_TIMEOUT * 0.95:
            log.warning("[RocketRide] timed out after %.1fs (budget %ss)",
                        el, ROCKETRIDE_TIMEOUT)
        else:
            log.warning("[RocketRide] connection refused or dropped after "
                        "%.2fs — not the %ss budget", el, ROCKETRIDE_TIMEOUT)
    except ImportError:
        log.warning("[RocketRide] enabled but the SDK is missing "
                    "— pip install rocketride")
    except Exception as e:
        log.warning("[RocketRide] failed (%s): %s", type(e).__name__, str(e)[:200])
    return None


def rocketride_status() -> dict:
    if not ROCKETRIDE_ENABLED:
        return {"enabled": False,
                "reason": "ROCKETRIDE_ENABLED not set — original pipeline in use"}
    if not (ROCKETRIDE_URI and ROCKETRIDE_APIKEY):
        return {"enabled": True, "usable": False,
                "reason": "ROCKETRIDE_URI or ROCKETRIDE_APIKEY missing"}
    try:
        uri = _rocketride_uri()
    except ValueError as e:
        return {"enabled": True, "usable": False, "reason": str(e)}
    return {"enabled": True, "usable": True, "uri": uri,
            "wired_into_pipeline": False,   # standalone endpoint only
            "run_verified": False}


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
        engineer_log(f"Gcode sliced: {gcode_path.stat().st_size / (1024*1024):.1f}MB", "success")
        return True
    except subprocess.TimeoutExpired:
        engineer_log("OrcaSlicer timed out", "warning")
        return False
    except Exception as e:
        engineer_log(f"OrcaSlicer error: {e}", "warning")
        return False


def run_engineer_pipeline(intent: str) -> None:
    with engineer_state["lock"]:
        engineer_state["status"] = "RUNNING"
        engineer_state["current_step"] = 0
        engineer_state["current_step_name"] = ENGINEER_STEP_NAMES[0]
        engineer_state["logs"] = []
        engineer_state["scad_script"] = None
        engineer_state["specs"] = None
        engineer_state["design_review"] = None

    engineer_log("=== ENGINEER PIPELINE STARTED ===", "success")
    engineer_log(f"Intent: {intent}", "info")

    engineer_set_step(0)
    engineer_log("Step 0 — design brief", "info")

    # Graph memory first. A prior build that already sized this exact kind of
    # part has the answer the design brief is about to pay an LLM call for.
    # Wrapped because a memory layer is never allowed to stop a build: any
    # failure here has to land us on the ordinary design_brief() path.
    specs = None
    try:
        prior = query_similar_builds(intent)
        if prior:
            top = prior[0]
            engineer_log(
                f"Graph memory: found similar prior build — \"{top['prompt']}\" "
                f"({top['score']:.0%} match)", "success")
            if top["score"] >= FALKORDB_REUSE_AT and top["specs"].get("od_mm"):
                specs = dict(top["specs"])
                engineer_log("Reusing that build's design brief — skipping the "
                             "LLM sizing call", "success")
    except Exception as e:
        log.warning("[FalkorDB] similar-build lookup failed: %s", e)

    if specs is None:
        # Memory miss. Before guessing, look the part up — this is the only
        # branch that spends money on research, which is the point: a part we
        # have built before never reaches here.
        research = None
        if LINKUP_API_KEY:
            engineer_log("No usable prior build — researching with Linkup", "info")
            try:
                research = research_with_linkup(intent)
            except Exception as e:
                # research_with_linkup already swallows its own errors; this is
                # belt and braces so a surprise can never reach the pipeline.
                log.warning("[Linkup] unexpected failure: %s", e)
            if research:
                engineer_log(
                    f"Linkup: {len(research['sources'])} sources — "
                    f"{research['answer'][:110]}", "success")
                for s in research["sources"][:3]:
                    engineer_log(f"  source: {s['name'][:70]}", "info")
            else:
                engineer_log("Linkup returned nothing usable — sizing from the "
                             "model's own knowledge", "warning")
        else:
            engineer_log("Linkup not configured — sizing from the model's own "
                         "knowledge", "info")
        specs = design_brief(intent, research)
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

    engineer_set_step(3)
    engineer_log("Step 3 — OrcaSlicer slice", "info")
    gcode_path = OUTPUT_DIR / "model.gcode"
    orca_slice_sync(stl_path, gcode_path)

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

    # Graph memory, in addition to the SQLite row above and the Supabase backup
    # below — not instead of either. On its own thread for the same reason the
    # Supabase backup is: a slow or dead remote must not hold up the print.
    def _graph_backup():
        bid = save_build_to_graph(intent, specs, material="PLA")
        if bid:
            engineer_log(f"Graph memory: build {bid} recorded", "success")
    threading.Thread(target=_graph_backup, daemon=True).start()

    engineer_set_step(4)
    engineer_log(f"Step 4 — send to printer ({moonraker_base() or 'no printer configured'})", "info")
    try:
        if not moonraker_base():
            raise Exception("PRINTER_IP not set")
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


@app.post("/api/rocketride/design-brief")
async def rocketride_design_brief_endpoint(body: dict) -> JSONResponse:
    """Run one design brief through RocketRide, standalone.

    Intentionally NOT part of /api/engineer/trigger. This exists so the path
    can be proven equivalent to design_brief() on its own before anything
    depends on it — and so that while it is unproven, no print can reach it.
    It never mutates engineer_state, so it cannot disturb a running job.
    """
    intent = body.get("intent", "").strip()
    if not intent:
        return JSONResponse({"ok": False, "error": "intent is required"},
                            status_code=400)
    if not ROCKETRIDE_ENABLED:
        return JSONResponse({"ok": False, "enabled": False,
                             "error": "ROCKETRIDE_ENABLED is not set"},
                            status_code=503)
    if not (ROCKETRIDE_URI and ROCKETRIDE_APIKEY):
        return JSONResponse({"ok": False, "enabled": True,
                             "error": "ROCKETRIDE_URI or ROCKETRIDE_APIKEY missing"},
                            status_code=503)
    # Off the event loop: rocketride_design_brief() spins its own loop, and
    # calling asyncio.run() from inside a running loop raises.
    result = await asyncio.to_thread(rocketride_design_brief, intent)
    if result is None:
        return JSONResponse({"ok": False, "error": "RocketRide run failed "
                             "— see server log"}, status_code=502)
    return JSONResponse({"ok": True, "intent": intent, "result": result,
                         "note": "unverified path — compare against "
                                 "design_brief() before relying on it"})


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
        })


@app.post("/api/engineer/reset")
def engineer_reset() -> JSONResponse:
    with engineer_state["lock"]:
        engineer_state["status"] = "IDLE"
        engineer_state["current_step"] = 0
        engineer_state["current_step_name"] = ""
        engineer_state["logs"] = []
        engineer_state["scad_script"] = None
        engineer_state["specs"] = None
        engineer_state["design_review"] = None
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001, reload=False)
