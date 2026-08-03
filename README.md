# Conjure

**Speak it. Build it. Hold it.**

*Built at Wizard Hackathon 2026 — San Francisco*

---

## What Is Conjure

Conjure is two connected systems that take a person from a spoken description to a physical 3D printed object with no human in the loop after the first button press.

**System 1 — Conjure Kiosk**
A standalone touchscreen device running on an Orange Pi 5 Pro. Anyone can walk up, describe what they want out loud, watch a 3D model generate and rotate on screen, then export a print-ready file to a USB drive.

**System 2 — Morph-Forge**
An autonomous engineering agent running on a laptop. It researches real-world engineering specs from the live web, writes parametric CAD code, renders and slices a 3D model, and dispatches it directly to a 3D printer.

---

## The Demo Flow

1. User walks up to the Orange Pi touchscreen
2. Holds the mic button and describes an object out loud
3. ElevenLabs speaks back confirmation in real time
4. Meshy AI generates a full 3D model from the voice description
5. Model appears rotating on screen in green — drag to spin, pinch to zoom, toggle solid / grid / wireframe view
6. User taps PRINT THIS
7. CuraEngine or OrcaSlicer slices it for the Neptune 4 Pro
8. Gcode copies automatically to USB drive
9. ElevenLabs announces the print is ready
10. User removes USB, inserts into printer — object starts printing

---

## Before You Demo: close other kiosk tabs

**Have exactly one kiosk tab open.** A second tab pointed at `/app` calls
`POST /api/reset` when it loads, and that endpoint deletes the shared working
files — including `output/model.gcode`. If an Engineer build has just finished
and another tab loads, that build's gcode is gone and the USB export has
nothing to copy.

It fails safe rather than badly. `/api/usb/export-gcode` re-checks the file on
disk instead of trusting the in-memory `slice_ok` flag, so the result is a
clean `409 "No gcode file on disk"` — never a stick written with the wrong
object. The cost is that you have to re-run the build, roughly a minute.

This is not a bug in reset. Speak mode and Engineer mode both write the same
scratch path, `output/model.gcode`, so there is no "Speak-only" state for reset
to clear — it is doing exactly what it says. The real fix is to give Engineer
its own per-build gcode path alongside the STL it already writes to
`output/models/{id}/`, which would put a finished build structurally out of
reset's reach. That change lives inside the Engineer pipeline and is
deliberately deferred.

---

## Sponsor Stack

| Sponsor | How We Use It |
|---------|--------------|
| **Meshy AI** | Text-to-3D generation. Takes the voice transcription and returns a full GLB and STL model ready to print |
| **InsForge** | Full backend infrastructure — PostgreSQL database for build records, S3-compatible object storage for STL files, model gateway for LLM routing, OAuth agent authentication. Project URL: https://9wy9m8kx.us-west.insforge.app |
| **Nebius** | Runs Llama-3.3-70B for two tasks — writing parametric OpenSCAD CAD scripts from engineering specs, and structural integrity review of generated designs |
| **You.com** | Live web research layer. Queries real engineering specifications for pipe dimensions so the LLM works with real data not hallucinated numbers |
| **Tavily** | Deep research layer. Second pass for PLA tensile strength and safety factors with structured citations from real sources |
| **ElevenLabs** | Text to speech. The kiosk speaks to the user at every stage — confirmation, generation status, print ready announcement |
| **Kite AI** | Agent identity layer. Authenticates the Conjure agent before the pipeline runs |
| **Topify** | Community sharing. Every completed fabrication job is automatically posted to the Topify maker community feed |

---

## Architecture

```
CONJURE KIOSK (Orange Pi 5 Pro)          MORPH-FORGE (Laptop)
─────────────────────────────            ─────────────────────────────────────
[Touchscreen] → Voice input              [Dashboard] → Button click
      ↓                                        ↓
[Web Speech API] → transcript            [Kite AI] → agent auth
      ↓                                        ↓
[Meshy AI] → GLB + STL model             [You.com] → pipe specs
      ↓                                        ↓
[InsForge Storage] → save STL            [Tavily] → material specs
      ↓                                        ↓
[Three.js] → rotating 3D viewer          [Nebius LLM] → OpenSCAD script
      ↓                                        ↓
[CuraEngine/OrcaSlicer] → gcode          [OpenSCAD CLI] → STL mesh
      ↓                                        ↓
[USB Drive] → Neptune 4 Pro              [CuraEngine] → gcode
      ↓                                        ↓
[ElevenLabs] → voice feedback            [InsForge Storage] → save STL
                                               ↓
                                         [InsForge DB] → build record
                                               ↓
                                         [Moonraker API] → printer
                                               ↓
                                         [Nebius] → structural review
                                               ↓
                                         [Topify] → community post
```
