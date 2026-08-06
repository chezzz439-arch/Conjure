# Known issues

Findings from the adversarial stress-test pass of 2026-08-05/06. Every entry
below was reproduced by running it against a live server — the captured output
is quoted verbatim. Nothing here is inferred from reading the code.

Ordered by how likely a real user is to hit it multiplied by how bad the
outcome is when they do.

---

## 1. `/api/print-now` ignores `slice_ok` and will upload stale gcode

**Severity: high — wrong part sent to a printer. Highly reachable.**

`api_print_now` checks only that `output/model.gcode` exists. `api_usb_export_gcode`
checks `slice_ok` as well. Two paths lead to the same artefact and only one of
them is gated.

`slice_ok` lives in memory and is set solely by the slicer's return value, so it
resets to `None` on every restart — crash, power cycle, or service restart. The
gcode file on disk survives. From that moment the previous session's part is
printable.

Reproduction:

```
$ kill -9 <server pid>          # mid-slice
$ <restart server>
slice_ok: None | gcode_exists: True | exportable: False

$ curl -X POST /api/usb/export-gcode
{"ok":false,"error":"No build has been sliced yet — run a build first"}   http=409

$ curl -X POST /api/print-now
{"status":"ok","filename":"conjure-model.gcode","started":false,
 "message":"Uploaded to the printer. Start it from Fluidd."}              http=200

printer log: UPLOAD RECEIVED path=/server/files/upload bytes=906246
```

Partly mitigated by `started: false` — a human still presses go in Fluidd, so
the wrong part is queued rather than printed unattended.

---

## 2. `POST /api/printer/profile` silently wipes the printer selection

**Severity: high — silent data loss, reports success.**

Any body the handler does not understand deletes the stored `selected_printer`
row and returns `200 {"ok":true}`. This was found by accident: it destroyed the
real selection during an unrelated fuzz run.

Reproduction — each of these four bodies empties the `settings` table:

```
body=null                  http=200  {"ok":true,"printer":null}  settings_rows_after=0
body={}                    http=200  {"ok":true,"printer":null}  settings_rows_after=0
body={"printer_id":null}   http=200  {"ok":true,"printer":null}  settings_rows_after=0
body={"garbage":1}         http=200  {"ok":true,"printer":null}  settings_rows_after=0
```

The selected printer is what the bed-fit check measures against, so losing it
silently disables oversized-part detection while telling the caller everything
worked.

---

## 3. `select_model` returns `ok:true` when the model's STL is missing

**Severity: medium-high — wrong geometry, presented as correct.**

A missing GLB returns 404, but a missing STL is skipped without comment. The
handler then sets `status="model_ready"` and reports success, leaving whatever
STL was already at `output/model.stl` in place.

Reproduction — model 115 with its STL moved aside:

```
$ curl -X POST /api/models/115/select
{"ok":true,"model_id":115,"prompt":"a cable clip that screws to a desk edge..."}  http=200

output/model.stl md5 before: 6539ce2c6443c8ac6a839779f471afd8
output/model.stl md5 after:  6539ce2c6443c8ac6a839779f471afd8   (unchanged)
pipeline status: model_ready | active_model_id: 115
```

The user picks one part from the gallery and slices a different one. Needs a
missing per-model STL to trigger, so less reachable than 1 or 2 — but the
outcome is the same wrong part.

---

## 4. `/api/print-now` will dispatch to a printer in an error state

**Severity: medium — dispatch to a faulted machine, no warning.**

`_BUSY_STATES` is `{"printing", "paused"}`. Klipper's `error` state is not in it,
so a printer reporting an MCU shutdown reads as `busy: false`.

Reproduction — fake Moonraker reporting `state: "error"`:

```
$ curl -X POST /api/print-now
http=200, message "Uploaded to the printer."
printer log: UPLOAD RECEIVED path=/server/files/upload bytes=906520
```

Nothing in the response mentions that the printer is faulted. Mitigated by
`started: false`.

---

## 5. Parameters inside `/* */` block comments are editable and silently do nothing

**Severity: low — confusing, not damaging.**

`//`-commented assignments are correctly excluded from the customizer. Block
comments are not: a parameter inside `/* ... */` is exposed as an editable field,
and the rewriter substitutes the new value *inside the comment*, where OpenSCAD
never reads it.

Reproduction — `depth_fake = 99;` placed inside a block comment:

```
GET /api/model/parameters   ->  depth_fake listed as editable
POST /api/model/apply       ->  applied: {'depth_fake': 100.0}
diff of the .scad:
  - depth_fake = 99;
  + depth_fake = 100;        (still inside the /* */ block)
```

The user changes a dimension, is told it applied, and the geometry does not move.

---

## Unverified — found by code reading only, not reproduced

These came out of a static audit and were **not** executed. They may or may not
be real; treat them as leads, not findings.

- No concurrency guard on `/api/generate` — proving it would spend Meshy credits.
- `db_insert_model` failure may be fatal to the Speak pipeline rather than
  degrading.
- Possible `model_id` path traversal within the Thingiverse API surface.

---

## Not covered by this pass

Engineer-pipeline tests could not run: `ANTHROPIC_API_KEY` returns
`401 API key is invalid`, so no Engineer build completes. Also untested —
symlink and disk-full handling in the file pipeline, unplugging a USB drive
mid-export (no writable USB was attached), SSE/load exhaustion, and all
browser-side behaviour.
