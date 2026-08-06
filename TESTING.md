# TESTING.md — on-hardware test results (Orange Pi 5 Pro + MKS PI printer)

Created 2026-08-06 on the Orange Pi, running the hardware tests that could not
run on the Mac. Standard: **HELD / DEGRADED SAFELY / BROKEN**, real output only.
"Did not run" is stated explicitly where a test needs physical action.

> NOTE: no prior TESTING.md existed in the repo (not on any branch) — this file
> is created fresh as the results log. Test numbers/definitions are taken from
> the tasking email.

## Environment fixes applied to unblock testing (authorized)
- **OpenSCAD installed** — `openscad 2021.01` (apt). Was absent; Engineer render
  step failed without it. Renders headlessly (no xvfb).
- **USB detection fixed** — `_find_usb()`/`_list_usb_drives()` now also scan
  `/run/media/<user>/<label>` (udisks2 auto-mount on this Armbian/GNOME build).
  Committed `d66ec1c`. `.env` is untouched-in-git (local only).
- **Printer configured** — `.env`: `PRINTER_IP=192.168.86.69`, and
  `MOONRAKER_URL=http://192.168.86.69` because this printer (MKS PI / `mkspi`,
  Klipper v0.10 + Fluidd) proxies Moonraker behind **nginx on port 80**, not 7125.

## Tool versions
OpenSCAD **2021.01** · CuraEngine **Cura_SteamEngine 5.0.0** · OrcaSlicer **absent**
(CuraEngine is the active slicer) · Klipper **v0.10.0-530** on the printer.

---

## Sanity checks — ALL HELD (after the fixes above)
1. **Anthropic key + full Engineer build → HELD.** brief → CAD (121 lines) →
   OpenSCAD render → slice → review, `PIPELINE COMPLETE`. Produced
   model.stl/model.glb/.scad. (Before OpenSCAD install: BROKEN at render with
   `[Errno 2] No such file or directory: '/usr/bin/openscad'`.)
2. **Printer / Moonraker → HELD.** `klippy_state: "ready"`, live telemetry
   (nozzle 24.6 °C, bed 24.7 °C, standby) through the app.
3. **USB → HELD.** drive detected at `/run/media/watcher/4C6D-154F`, writable,
   5.7 GB free.

---

## PRIORITY 1 — USB export (51–54)

- **51 — unplug mid-export → DEGRADED (partial).** Could not faithfully time a
  pull mid-byte-write (a 5.5 MB copy finishes in <0.1 s). Reproduced the
  *drive-gone-before-copy* case: the app refused honestly — HTTP **400**
  `'/media/usb' is not a detected USB drive` (listed remaining drives), **no
  false success**. Code path: `shutil.copy2` → verifies `written==size`
  (→ 500 "Copy incomplete" on truncation) → `sync`. The residual risk is that
  `sync` is best-effort (logs a warning, still returns `ok:true`), so a stick
  pulled in the copy-to-cache → flush window could yield a truncated file with
  a success message. Physical mid-write pull: **not run** (needs hands).
- **52 — full drive → HELD.** free 4.0 MB < 5.5 MB gcode → HTTP **507**
  "Not enough space on usb — need 5.5 MB, 4.0 MB free". **No partial file left.**
- **53 — read-only drive → HELD.** RO mount → HTTP **403** "usb is read-only —
  unlock the drive or use another one". Nothing written. Accurate.
- **54 — two drives → DEGRADED.** With no path given, the app picks by a **fixed
  scan order** (`/media/usb` > `/mnt/usb` > `/run/media/...`), **not** the drive
  the user just inserted. It *does* name the target (`"drive":"usb"`), so not
  silent — but the name is the mount-folder basename, ambiguous when two
  fixed-path mounts are both "usb". No way for the user to choose which stick.

## PRIORITY 2 — Moonraker against the real printer (47–50)

- **47 — black-hole host (accepts TCP, never answers) → HELD.** print-now did
  **not** hang: it timed out cleanly at the 6 s status pre-check → HTTP **502**
  "No answer from the printer… check it's powered on and on the same network".
  Wall time 6.0 s. Never reached the 120 s upload.
- **48 — HTTP 200 with non-Moonraker garbage → was BROKEN, now FIXED (`d59a171`).**
  Before: the app believed it — `/api/printer` → `online:true, state:"unknown"`
  from `{"totally":"not moonraker"}`, and print-now "uploaded successfully" to
  the garbage host. After: `moonraker_status()` requires the real Moonraker
  `result.status` envelope; a bare 200 now returns `online:false` "isn't a
  Moonraker printer" and print-now refuses (502). Re-verified against the stub.
- **49 — dispatch to a printer in error/shutdown state → was BROKEN (bug B4),
  now FIXED (`d59a171`).** Before: drove the printer to `shutdown`; the app
  still reported `online:true, state:"standby"` (it read only
  `print_stats.state`) and **print-now dispatched anyway** (HTTP 200 "Uploaded
  to the printer") while nothing printed. After: `moonraker_status()` reads
  `webhooks.state` and exposes `ready`/`klippy_state`; a shutdown printer now
  reports `ready:false` with an honest message, print-now refuses (409), and the
  nav shows "Printer not ready" and hides Print. Re-verified on real hardware
  (emergency_stop → 409 refusal → firmware_restart → ready).
- **50 — stale gcode guard (59b4710) → HELD on real hardware.** Broke the stamp
  (edited model.stl without re-slicing) → print-now **409**
  "This gcode was sliced from a different model — slice the current one before
  printing". **No dispatch.** USB export refused identically (409). Guard holds.

## PRIORITY 3 — Touchscreen and browser (63–69)

- **63/64 — Three.js context leak over repeated model loads → DEGRADED.** JS heap
  did **not** grow (26 → 4.8 MB across 14 load/navigate cycles; GC reclaims).
  But **WebGL contexts accumulated ~1 per view** (3 → 16) until the browser hit
  its ~16-context cap and began force-losing the oldest (`lost:1`).
  `disposeViewer()` calls `renderer.dispose()` but not `renderer.forceContextLoss()`,
  so contexts linger. Bounded by the browser, but on a long-running kiosk doing
  many views without a full reload, rendering will eventually churn/degrade.
- **65 — multiple tabs / shared server state → DEGRADED (by design).**
  `pipeline_state` and `engineer_state` are single module-level globals and
  `/events` broadcasts to all clients — a second tab sees and can clobber the
  first tab's build. Acceptable for a single-screen kiosk; unsafe for concurrent
  sessions.
- **66 — parameter panel open, server killed → HELD** (covered by 67): actions
  fail honestly, no silent success.
- **67 — server down, every action → HELD (honest, generic).** With the backend
  killed, every API call **rejected** ("Failed to fetch") and the app showed an
  error screen "The build stopped — An unexpected error occurred". **Nothing
  claimed false success.** The message is generic rather than "server offline",
  but it is honest. Server + printer restored cleanly afterward.
- **68 — JavaScript disabled → DEGRADED.** Not a blank white screen — the nav bar
  renders ("CONJURE / Home / Library / Set printer") — but **every content
  screen is `display:none`** (revealed only by JS) and there is **no
  `<noscript>`** anywhere. A JS-off user gets a header with dead buttons and an
  empty body, with no explanation.
- **69 — heavy network throttle → NOT RUN** (time). Approach: CDP
  `Network.emulateNetworkConditions` then drive load/slice/export.
- **Touch interaction (sliders, printer-picker modal, param panel, finger-size
  targets) → NOT RUN.** Requires the physical touchscreen; cannot be exercised
  headlessly. Needs a human on the device.

## PRIORITY 4 — Engineer / render / CGAL

- **Engineer build end-to-end → HELD** (after OpenSCAD install). OpenSCAD 2021.01
  renders headlessly with no display/xvfb.
- **Observation (needs follow-up):** during the Engineer build, Step 4 logged
  "Moonraker dispatch failed (slice failed — refusing to print a stale gcode…)",
  yet `/api/slice` on the *same* `model.stl` succeeds and produces valid gcode.
  Suggests the Engineer pipeline's internal slice step differs from the Speak
  `/api/slice` path — worth investigating.
- **CGAL auto-split reproduction / render-time benchmarks → NOT RUN** (time).

---

## BROKEN → both now FIXED (`d59a171`)
1. **B4 (test 49) — dispatched to a faulted/shutdown printer and reported
   success.** FIXED: print-now now refuses unless `klippy_state == ready`.
2. **Test 48 — believed any HTTP-200 server was the printer.** FIXED: status
   query now requires the Moonraker `result.status` envelope.

## DEGRADED (worth fixing, not dangerous)
- 63/64 WebGL context leak (add `forceContextLoss()` in `disposeViewer`).
- 68 no `<noscript>` fallback (kiosk looks half-dead with JS off).
- 54 two-drive selection is scan-order, not user-chosen, and names are ambiguous.
- 51 `sync` is best-effort → possible false success if unplugged in the flush window.
- 65 shared global state across tabs.

## Not run (need a human / more time)
- 51 physical mid-write unplug · 69 network throttle · all touch interaction ·
  P4 CGAL auto-split + render-time benchmarks.
