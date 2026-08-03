#!/usr/bin/env python
"""Seed the FalkorDB graph with plausible prior builds, for a cold demo.

A fresh FalkorDB instance has no history, so the first thing anyone tries in a
demo returns nothing — which is the correct answer and a terrible showing. This
writes a set of builds that look like they came from real kiosk sessions, so
the memory layer has something to remember.

Every row goes through main.save_build_to_graph(), the same function the
Engineer pipeline calls. That is deliberate: a seeder with its own INSERT
statements drifts from the app, and you end up demoing a graph shape the kiosk
does not actually produce.

Usage
  .venv/bin/python seed_memory.py                # add the seed builds
  .venv/bin/python seed_memory.py --dry-run      # show what would be written
  .venv/bin/python seed_memory.py --reset        # delete the graph first
  .venv/bin/python seed_memory.py --verify       # just report what is there

Unlike the kiosk, this exits non-zero when FalkorDB is unreachable. The app
degrades quietly because a print must not fail over a missing graph; a seeder
that quietly does nothing is just a lie with a zero exit code.
"""

import argparse
import sys

import main


# Grouped into families that share a material and a wall/clearance figure, so
# the SIMILAR_TO edges and the shared Spec nodes are visible rather than
# theoretical. A demo query on any one of these should surface its siblings.
SEED_BUILDS = [
    # ── printed-in-place mechanisms: share clearance 0.4, wall 2.4 ─────────
    ("a compact planetary gearbox with a sun gear, three planet gears on a "
     "carrier and an internal ring gear, printed pre-assembled",
     dict(od_mm=60.0, wall_mm=2.4, clearance_mm=0.4,
          summary="planetary gearbox, printed in place, hand-spun"), "PLA"),
    ("a planetary gearbox with a hand crank on the input shaft, printed "
     "pre-assembled so it spins straight off the bed",
     dict(od_mm=64.0, wall_mm=2.4, clearance_mm=0.4,
          summary="geared crank assembly, 4:1 reduction"), "PLA"),
    ("a print-in-place herringbone gear pair on a base plate",
     dict(od_mm=48.0, wall_mm=2.4, clearance_mm=0.4,
          summary="meshed herringbone gear demo"), "PLA"),
    ("a captive ball joint printed in place for a camera arm",
     dict(od_mm=32.0, wall_mm=2.4, clearance_mm=0.4,
          summary="socket and ball, printed assembled"), "PLA"),

    # ── brackets and mounts: rigid, no moving clearance ────────────────────
    ("a wall bracket for a floating shelf, rated to hold a few kilograms",
     dict(od_mm=120.0, wall_mm=4.0, clearance_mm=0.0,
          summary="L bracket with gusset"), "PLA"),
    ("an under-desk bracket to mount a power strip out of sight",
     dict(od_mm=110.0, wall_mm=4.0, clearance_mm=0.0,
          summary="screw-mounted cradle"), "PLA"),
    ("a monitor arm VESA adapter plate, 100mm hole spacing",
     dict(od_mm=100.0, wall_mm=4.0, clearance_mm=0.0,
          summary="VESA 100 adapter"), "PETG"),

    # ── enclosures: thin wall, small tolerance for lids ────────────────────
    ("a vented enclosure for a Raspberry Pi 4 with a snap-fit lid",
     dict(od_mm=95.0, wall_mm=2.0, clearance_mm=0.3,
          summary="Pi 4 case, snap lid, side vents"), "PETG"),
    ("a small parts box with a hinged lid, printed in one piece",
     dict(od_mm=80.0, wall_mm=2.0, clearance_mm=0.3,
          summary="hinged parts box"), "PLA"),

    # ── desk objects: the "no prior art" end of the range ──────────────────
    ("a desk hook for hanging headphones off the edge of a table",
     dict(od_mm=80.0, wall_mm=2.4, clearance_mm=0.0,
          summary="clamp-on headphone hook"), "PLA"),
    ("a weighted pen cup with a honeycomb wall pattern",
     dict(od_mm=75.0, wall_mm=2.0, clearance_mm=0.0,
          summary="honeycomb pen cup"), "PLA"),
]

# Tensile figures per material, so the Material nodes differ by more than name.
# Both are the usual published ballpark for FDM filament, not measured here —
# same caveat as PLA_TENSILE_MPA in main.py.
TENSILE = {"PLA": main.PLA_TENSILE_MPA, "PETG": 50.0}


def require_graph():
    """The graph, or exit with something actionable."""
    if not main.FALKORDB_HOST and not main.FALKORDB_URL:
        sys.exit("FalkorDB is not configured.\n"
                 "  Set FALKORDB_HOST (and PORT/USERNAME/PASSWORD/SSL) in .env.\n"
                 "  Free instance: https://app.falkordb.cloud/signup")
    status = main.falkordb_status()
    if not status["online"]:
        sys.exit(f"FalkorDB is configured but unreachable: "
                 f"{status.get('reason', 'unknown')}\n"
                 "  A cloud instance is stopped after 1 day idle — check the\n"
                 "  dashboard, and confirm FALKORDB_SSL=true for cloud.")
    g = main._falkor_graph()
    if g is None:
        sys.exit("Could not select the graph — see the log above.")
    return g, status


def report(g) -> None:
    """What the graph currently holds."""
    def scalar(q):
        try:
            rs = g.ro_query(q).result_set
            return rs[0][0] if rs else 0
        except Exception:
            return 0

    print(f"\n  graph          {main.FALKORDB_GRAPH}")
    for label in ("Build", "Material", "Spec"):
        print(f"  {label + ' nodes':14} {scalar(f'MATCH (n:{label}) RETURN count(n)')}")
    for rel in ("USES", "HAS_SPEC", "SIMILAR_TO"):
        print(f"  {rel + ' edges':14} {scalar(f'MATCH ()-[r:{rel}]->() RETURN count(r)')}")

    try:
        shared = g.ro_query(
            "MATCH (b:Build)-[:HAS_SPEC]->(s:Spec) WITH s, count(b) AS n "
            "WHERE n > 1 RETURN s.dimension_type, s.value, s.unit, n "
            "ORDER BY n DESC LIMIT 5"
        ).result_set
        if shared:
            print("\n  spec nodes shared across builds (the graph, doing its job):")
            for dt, val, unit, n in shared:
                print(f"    {dt}={val}{unit} — {n} builds")
    except Exception:
        pass


def main_cli() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reset", action="store_true",
                    help="delete the whole graph before seeding")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the builds without writing anything")
    ap.add_argument("--verify", action="store_true",
                    help="report the current graph contents and exit")
    args = ap.parse_args()

    if args.dry_run:
        print(f"Would write {len(SEED_BUILDS)} builds to graph "
              f"{main.FALKORDB_GRAPH!r}:\n")
        for prompt, specs, material in SEED_BUILDS:
            print(f"  [{material:4}] od={specs['od_mm']:<6} wall={specs['wall_mm']:<4} "
                  f"clr={specs['clearance_mm']:<4} {prompt[:52]}")
        print("\nNothing written (--dry-run).")
        return

    g, status = require_graph()

    if args.verify:
        report(g)
        return

    if args.reset:
        existing = status.get("builds", 0)
        # Destructive, and the operator may not realise the graph already has
        # real pipeline history in it. Make them look at the number first.
        answer = input(f"Delete graph {main.FALKORDB_GRAPH!r} "
                       f"({existing} builds) and reseed? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            sys.exit("Aborted — nothing deleted.")
        try:
            g.delete()
            print(f"Deleted graph {main.FALKORDB_GRAPH!r}.")
        except Exception as e:
            sys.exit(f"Could not delete the graph: {e}")
        g, status = require_graph()

    print(f"Seeding {len(SEED_BUILDS)} builds into {main.FALKORDB_GRAPH!r} "
          f"(had {status.get('builds', 0)})...\n")
    written, failed = 0, 0
    for prompt, specs, material in SEED_BUILDS:
        specs = dict(specs, tensile_mpa=TENSILE.get(material, main.PLA_TENSILE_MPA),
                     safety_factor=main.DESIGN_SAFETY_FACTOR)
        build_id = main.save_build_to_graph(prompt, specs, material=material)
        if build_id:
            written += 1
            print(f"  ✓ {build_id}  [{material:4}] {prompt[:56]}")
        else:
            failed += 1
            print(f"  ✗ FAILED           {prompt[:56]}")

    report(g)
    print(f"\n{written} written, {failed} failed.")
    if failed:
        sys.exit(1)
    print("\nTry it:")
    print("  curl -s 'http://localhost:8000/api/memory/similar"
          "?prompt=planetary+gearbox+with+a+crank' | python -m json.tool")


if __name__ == "__main__":
    main_cli()
