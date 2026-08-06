#!/usr/bin/env python3
"""Build data/printers.json from an OrcaSlicer profile tree.

Run at development time, never at runtime. The kiosk reads only the JSON this
writes; it never touches OrcaSlicer's profile directory and never goes to the
network. Re-run it when OrcaSlicer is upgraded and you want the new printers:

    .venv/bin/python tools/import_orca_printers.py
    .venv/bin/python tools/import_orca_printers.py --profiles-dir /path/to/profiles

Everything here is derived from the installed OrcaSlicer profile tree by
reading it, not from documentation. The notes below record what the data
actually looks like, because several of these details are the kind that
produce a wrong bed size rather than an error.

Structure
    <Vendor>.json is an index with machine_list / machine_model_list, each
    entry {name, sub_path} resolving to profiles/<Vendor>/<sub_path>.
    A "machine_model" is the product a person recognises and carries no bed
    dimensions. A "machine" is one nozzle variant and carries printable_area
    and printable_height. They are linked by machine.printer_model == model.name.

    We walk the indexes rather than globbing, because 387 files sitting in
    machine/ directories are actually type=machine_model, and a glob picks up
    profiles no vendor index offers the user.

inherits
    Resolved by NAME, scoped to the vendor -- never a path or a filename.
    Chains run up to 6 deep (331 profiles are 4+ levels). A child key replaces
    the parent's wholesale; there is no array merging anywhere in the tree.

instantiation
    "true" means a user can pick it. The rest are abstract templates
    (fdm_common, fdm_machine_common, ...) that must never reach the picker.

printable_area -- the part that fails quietly
    Normally a list of "XxY" strings. But:
      * 17 profiles (all Creality) give ONE comma-separated string instead.
      * Point counts are not always 4: hexagons, 72-point delta circles, and
        239-point rounded rectangles all occur.
      * Bambu X2D has a trailing space, "0x256 ".
      * Anycubic Predator uses scientific notation, "1.1328e-14x185".
      * There is no origin field anywhere in the tree. Some beds start at
        0,0, some are centred on the origin, some are neither.
    So bed size is max - min over the polygon, never max. Taking max is wrong
    for 121 of 1001 printers, worst case by 250mm -- half the bed, silently.
    We keep the polygon for non-rectangular beds so the fit check can test the
    real outline instead of guessing at a shape.

printable_height
    A string on every profile that has one. Exactly one machine lacks it.

Temperatures
    Max nozzle temp and max bed temp DO NOT EXIST in machine profiles -- zero
    hits for any candidate key across all 1,545 machine and machine_model
    files. Those limits live in filament profiles and are per-material, not
    per-printer. We import nozzle_type / nozzle_hrc / default_bed_type, which
    are real, and report no temperatures rather than inventing them.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# Where OrcaSlicer keeps its profiles, per platform. First hit wins; override
# with --profiles-dir. The kiosk itself never uses these -- only this script.
CANDIDATE_DIRS = [
    Path("/Applications/OrcaSlicer.app/Contents/Resources/profiles"),
    Path("/usr/share/OrcaSlicer/profiles"),
    Path("/usr/local/share/OrcaSlicer/profiles"),
    Path.home() / ".local/share/OrcaSlicer/profiles",
    Path("/opt/OrcaSlicer/resources/profiles"),
]

REPO = Path(__file__).resolve().parent.parent
OUT_JSON = REPO / "data" / "printers.json"
OUT_NOTICE = REPO / "data" / "NOTICE"

SCHEMA_VERSION = 1

# "0.4 nozzle", "0.4 HF nozzle", "(0.25 nozzle)" -- the variant marker inside a
# machine name. Stripped when naming a group, since the group already lists its
# nozzles separately.
NOZZLE_TOKEN = re.compile(r"\s*\(?\b\d+(?:\.\d+)?\s*(?:HF|hf)?\s*nozzle\b\)?", re.I)


# --------------------------------------------------------------------------
# reading the tree
# --------------------------------------------------------------------------

def load_indexes(root: Path) -> dict:
    vendors = {}
    for idx in sorted(root.glob("*.json")):
        if idx.name == "blacklist.json":
            continue
        try:
            vendors[idx.stem] = json.loads(idx.read_text())
        except Exception as exc:                      # noqa: BLE001 - reported
            print(f"  !! vendor index unreadable: {idx.name}: {exc}", file=sys.stderr)
    return vendors


def load_machines(root: Path, vendors: dict) -> tuple[dict, list]:
    """(vendor, name) -> raw profile dict, plus a list of what could not load."""
    raw, failures = {}, []
    for vendor, idx in vendors.items():
        for ent in idx.get("machine_list", []):
            name, sub = ent.get("name"), ent.get("sub_path")
            if not name or not sub:
                failures.append((vendor, str(name), "index entry missing name/sub_path"))
                continue
            path = root / vendor / sub
            try:
                raw[(vendor, name)] = json.loads(path.read_text())
            except FileNotFoundError:
                failures.append((vendor, name, f"file missing: {sub}"))
            except Exception as exc:                  # noqa: BLE001 - reported
                failures.append((vendor, name, f"unparseable: {exc}"))
    return raw, failures


def flatten(raw: dict, vendor: str, name: str, seen: tuple = ()) -> tuple[dict | None, str]:
    """Resolve inherits upward. Returns (merged, error). Cycle-safe."""
    if (vendor, name) in seen:
        return None, f"inherits cycle at {name!r}"
    node = raw.get((vendor, name))
    if node is None:
        return None, f"inherits target {name!r} not found in vendor {vendor!r}"
    parent = node.get("inherits")
    if not parent:
        return dict(node), ""
    merged, err = flatten(raw, vendor, parent, seen + ((vendor, name),))
    if merged is None:
        return None, err
    # Child wins outright -- OrcaSlicer replaces keys, it does not merge lists.
    merged.update({k: v for k, v in node.items() if k != "inherits"})
    return merged, ""


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------

def parse_points(area) -> tuple[list, str]:
    """printable_area -> [(x, y), ...]. Returns ([], reason) if unusable.

    Accepts both real-world forms: a list of "XxY" strings, and the single
    comma-separated string 17 Creality profiles use. Iterating that string
    naively yields a polygon of individual characters, so the two cases are
    flattened together deliberately rather than assumed apart.
    """
    if area is None:
        return [], "no printable_area"
    chunks: list[str] = []
    if isinstance(area, str):
        chunks = area.split(",")
    elif isinstance(area, (list, tuple)):
        for item in area:
            chunks.extend(str(item).split(","))
    else:
        return [], f"printable_area is {type(area).__name__}"

    points = []
    for chunk in chunks:
        chunk = chunk.strip()                # trailing spaces occur, e.g. "0x256 "
        if not chunk:
            continue
        xs, sep, ys = chunk.partition("x")
        if not sep:
            return [], f"point without separator: {chunk!r}"
        try:
            # float() handles the scientific notation Anycubic Predator uses.
            points.append((float(xs), float(ys)))
        except ValueError:
            return [], f"non-numeric point: {chunk!r}"
    if len(points) < 3:
        return [], f"only {len(points)} point(s)"
    return points, ""


def is_axis_rect(points: list) -> bool:
    """True when the outline is exactly the axis-aligned box it spans."""
    if len(points) != 4:
        return False
    xs = {round(p[0], 6) for p in points}
    ys = {round(p[1], 6) for p in points}
    return len(xs) == 2 and len(ys) == 2


def origin_label(min_x: float, min_y: float, max_x: float, max_y: float) -> str:
    """Descriptive only. Nothing computes a fit from this -- the fit check uses
    the extents and the polygon, so a misread label cannot cause a wrong answer."""
    if abs(min_x) < 0.51 and abs(min_y) < 0.51:
        return "corner"
    if abs(min_x + max_x) < 1.0 and abs(min_y + max_y) < 1.0:
        return "center"
    return "offset"


def as_float(value):
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def nozzle_of(machine_name: str, prof: dict):
    """Nozzle size for one machine profile.

    printer_variant is the nozzle size, but 124 profiles omit it, so fall back
    to nozzle_diameter[0]. A nozzle_diameter list longer than one means several
    EXTRUDERS (Bambu H2D is ["0.4","0.4"], Prusa XL 5T has five) -- it does not
    mean the printer offers several nozzle sizes, so only the first is a size.
    """
    variant = as_float(prof.get("printer_variant"))
    if variant is not None:
        return variant, len(prof.get("nozzle_diameter") or []) or 1
    diam = prof.get("nozzle_diameter")
    return as_float(diam), (len(diam) if isinstance(diam, list) else 1) or 1


def slug(text: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return out or "unnamed"


def fmt_num(value) -> str:
    if value is None:
        return "?"
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profiles-dir", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=OUT_JSON)
    args = ap.parse_args()

    root = args.profiles_dir
    if root is None:
        root = next((c for c in CANDIDATE_DIRS if c.is_dir()), None)
    if root is None or not root.is_dir():
        print("Could not find an OrcaSlicer profiles directory. Looked in:", file=sys.stderr)
        for c in CANDIDATE_DIRS:
            print(f"    {c}", file=sys.stderr)
        print("Pass --profiles-dir to point at one.", file=sys.stderr)
        return 2

    print(f"reading {root}")
    vendors = load_indexes(root)
    raw, load_failures = load_machines(root, vendors)
    print(f"  {len(vendors)} vendor indexes, {len(raw)} machine profiles listed")
    for v, n, why in load_failures:
        print(f"  !! could not load {v}/{n}: {why}")

    # ---- flatten -------------------------------------------------------
    flat, flat_failures = {}, []
    for key in raw:
        merged, err = flatten(raw, *key)
        if merged is None:
            flat_failures.append((key[0], key[1], err))
        else:
            flat[key] = merged
    print(f"  flattened {len(flat)}, failed {len(flat_failures)}")
    for v, n, why in flat_failures:
        print(f"  !! {v}/{n}: {why}")

    # ---- keep only what a user can actually select ---------------------
    selectable = {k: d for k, d in flat.items()
                  if str(raw[k].get("instantiation", "")).lower() == "true"}
    print(f"  {len(selectable)} selectable, "
          f"{len(flat) - len(selectable)} abstract templates excluded")

    # ---- per-machine geometry ------------------------------------------
    groups = defaultdict(list)
    skipped = []
    no_height = []
    for (vendor, mname), prof in sorted(selectable.items()):
        points, why = parse_points(prof.get("printable_area"))
        if not points:
            skipped.append((vendor, mname, why))
            continue
        min_x = min(p[0] for p in points)
        max_x = max(p[0] for p in points)
        min_y = min(p[1] for p in points)
        max_y = max(p[1] for p in points)
        bed_x = round(max_x - min_x, 3)
        bed_y = round(max_y - min_y, 3)

        bed_z = as_float(prof.get("printable_height"))
        if bed_z is None:
            # One machine in the whole tree. Recorded as unknown rather than
            # guessed, so the fit check can say so instead of inventing a limit.
            no_height.append((vendor, mname))
        else:
            bed_z = round(bed_z, 3)

        rect = is_axis_rect(points)
        model = prof.get("printer_model") or mname
        nozzle, extruders = nozzle_of(mname, prof)

        # Grouping key: same product, same buildable space, same outline. Nozzle
        # variants collapse (375 of 383 models agree exactly); the 8 that really
        # do differ -- IDEX copy/mirror modes, RatRig's 500 vs 300 height --
        # stay apart, because merging them would advertise a volume that half
        # the variants do not have.
        shape_key = "rect" if rect else json.dumps([[round(x, 3), round(y, 3)]
                                                    for x, y in points])
        groups[(vendor, model, bed_x, bed_y, bed_z, shape_key)].append({
            "machine": mname,
            "nozzle": nozzle,
            "extruders": extruders,
            "points": points,
            "rect": rect,
            "origin": origin_label(min_x, min_y, max_x, max_y),
            "min_x": round(min_x, 3), "min_y": round(min_y, 3),
            "nozzle_type": prof.get("nozzle_type"),
            "nozzle_hrc": prof.get("nozzle_hrc"),
            "default_bed_type": prof.get("default_bed_type"),
        })

    # ---- name the groups ------------------------------------------------
    by_model = defaultdict(list)
    for key in groups:
        by_model[(key[0], key[1])].append(key)

    printers, renamed = [], []
    used_ids = {}
    for (vendor, model), keys in sorted(by_model.items()):
        # Strip the nozzle marker out of each machine name; when every member of
        # a group reduces to the same thing, that is the group's natural label.
        labels = {}
        for key in keys:
            stripped = {NOZZLE_TOKEN.sub("", m["machine"]).strip(" -")
                        for m in groups[key]}
            labels[key] = stripped.pop() if len(stripped) == 1 else model

        for key in sorted(keys):
            _, _, bed_x, bed_y, bed_z, _ = key
            label = labels[key]
            members = groups[key]
            first = members[0]
            nozzles = sorted({m["nozzle"] for m in members if m["nozzle"] is not None})

            # Two groups of one product sharing a label have to be told apart by
            # whatever actually differs between them. Nozzle first: it is what
            # the machine names themselves distinguish and what a user
            # recognises. Volume second, for the products where the nozzle sets
            # overlap. Both can fail -- Folgertech's two i3 profiles have the
            # same nozzle-stripped name AND the same extents, differing only in
            # a bed outline that looks like an upstream typo -- so there is a
            # last-resort counter and every rename gets reported.
            if list(labels.values()).count(label) > 1:
                siblings = [k for k in keys if labels[k] == label]
                noz = {k: tuple(sorted({m["nozzle"] for m in groups[k]
                                        if m["nozzle"] is not None}))
                       for k in siblings}
                if len(set(noz.values())) == len(siblings):
                    label = f"{label} ({', '.join(fmt_num(n) for n in nozzles)} nozzle)"
                else:
                    label = (f"{label} ({fmt_num(bed_x)}x{fmt_num(bed_y)}"
                             f"x{fmt_num(bed_z)} mm)")
                renamed.append(label)

            pid = f"{slug(vendor)}/{slug(label)}"
            if pid in used_ids:                       # never silently collide
                used_ids[pid] += 1
                pid = f"{pid}-{used_ids[pid]}"
                renamed.append(f"{pid} (id collision)")
            else:
                used_ids[pid] = 1

            entry = {
                "id": pid,
                "vendor": vendor,
                "name": label,
                "model": model,
                "bed_x": bed_x,
                "bed_y": bed_y,
                "bed_z": bed_z,
                "origin": first["origin"],
                "bed_shape": "rect" if first["rect"] else "polygon",
                "nozzles": nozzles,
                "extruders": max(m["extruders"] for m in members),
                "variants": sorted(m["machine"] for m in members),
                "source": "orcaslicer",
            }
            if not first["rect"]:
                # Only non-rectangular beds carry an outline. A rectangle is
                # fully described by its extents plus the origin offset, and
                # 59 polygons (some 239 points) are worth keeping small.
                entry["bed_polygon"] = [[round(x, 3), round(y, 3)]
                                        for x, y in first["points"]]
            if first["min_x"] or first["min_y"]:
                entry["bed_min"] = [first["min_x"], first["min_y"]]
            for field in ("nozzle_type", "nozzle_hrc", "default_bed_type"):
                value = first.get(field)
                if value not in (None, ""):
                    entry[field] = value
            printers.append(entry)

    printers.sort(key=lambda p: (p["vendor"].lower(), p["name"].lower()))

    # ---- the fallback ---------------------------------------------------
    # Always present, always first, never dependent on the import having found
    # anything. If the OrcaSlicer data were empty the picker would still work.
    generic = {
        "id": "generic/generic-fdm-printer",
        "vendor": "Generic",
        "name": "Generic FDM Printer",
        "model": "Generic FDM Printer",
        "bed_x": 220.0,
        "bed_y": 220.0,
        "bed_z": 250.0,
        "origin": "corner",
        "bed_shape": "rect",
        "nozzles": [0.4],
        "extruders": 1,
        "variants": [],
        "editable": True,          # the one entry whose bed the user may retype
        "source": "builtin",
    }

    payload = {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "slicer": "OrcaSlicer",
            "profiles_dir": str(root),
            "license": "AGPL-3.0",
            "note": "Derived from OrcaSlicer's bundled profiles. See data/NOTICE.",
        },
        "counts": {
            "machine_profiles_listed": len(raw),
            "flattened": len(flat),
            "selectable": len(selectable),
            "abstract_templates_excluded": len(flat) - len(selectable),
            "printers": len(printers) + 1,
            "skipped_no_usable_bed": len(skipped),
            "missing_printable_height": len(no_height),
        },
        "printers": [generic] + printers,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=1, sort_keys=False) + "\n")

    # ---- report ---------------------------------------------------------
    print()
    print(f"wrote {args.out.relative_to(REPO)}  "
          f"({args.out.stat().st_size / 1024:.0f} KB)")
    print(f"  printers in picker : {len(printers) + 1} "
          f"(incl. Generic FDM Printer)")
    print(f"  from machines      : {len(selectable) - len(skipped)}")
    print(f"  vendors            : {len({p['vendor'] for p in printers})}")
    print(f"  non-rectangular    : {sum(1 for p in printers if p['bed_shape'] != 'rect')}")
    print(f"  multi-extruder     : {sum(1 for p in printers if p['extruders'] > 1)}")
    print(f"  bed_z unknown      : {sum(1 for p in printers if p['bed_z'] is None)}")

    if skipped:
        print(f"\n  DROPPED {len(skipped)} selectable machines with no usable bed:")
        for v, n, why in skipped:
            print(f"    {v}/{n}: {why}")
    if no_height:
        print(f"\n  {len(no_height)} machines have no printable_height "
              f"(kept, height recorded as unknown):")
        for v, n in no_height:
            print(f"    {v}/{n}")
    if flat_failures or load_failures:
        print(f"\n  {len(flat_failures) + len(load_failures)} profiles never "
              f"loaded or flattened -- listed above.")
    if renamed:
        print(f"\n  {len(renamed)} entries disambiguated (one product, several "
              f"genuinely different build volumes or outlines):")
        for r in renamed:
            print(f"    {r}")

    # A bed given as a polygon of only 4 points that is not a rectangle is far
    # more likely an upstream typo than a real machine. Reported, not corrected:
    # the extents stay whatever the profile says, so the fit check is at worst
    # as conservative as OrcaSlicer itself.
    odd = [p for p in printers
           if p["bed_shape"] == "polygon" and len(p.get("bed_polygon", [])) < 6]
    if odd:
        print(f"\n  {len(odd)} bed outline(s) look like upstream typos "
              f"(4-point non-rectangles, kept as-is):")
        for p in odd:
            print(f"    {p['vendor']}/{p['name']}: {p['bed_polygon']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
