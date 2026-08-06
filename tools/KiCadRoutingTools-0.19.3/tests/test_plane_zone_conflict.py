#!/usr/bin/env python3
"""route_planes: a foreign-net pour on the target layer must not abort the run.

Two defects, both reproduced on megadesk and hackrf_one:

1. `check_existing_zones` treated another net's pour on the target layer as a
   fatal error ("Cannot create <net> zone on same layer. Aborting."), so
   route_planes could not add a plane to any board that already had a pour on
   that layer -- while the GUI path deliberately allowed it ("in KiCad, zones
   with different nets may coexist on a layer"). KiCad fills such zones by
   priority and holds clearance between nets; the run now continues, warns, and
   gives the new plane the higher fill priority so the overlap is decided
   deterministically rather than by UUID order.

2. After that abort, `main()` still ran its post-passes on an output file that was
   never written, so `clean_plane_copper` raised FileNotFoundError and buried the
   real error message under a traceback. The post-passes are now gated on this
   run having actually written the board.

Run with:  python3 tests/test_plane_zone_conflict.py
"""
import os
import subprocess
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, "rust_router"))

from plane_io import (ZoneInfo, check_existing_zones,  # noqa: E402
                      shared_layer_zone_priority, filter_zones_from_content)

BOARD = os.path.join(ROOT_DIR, "kicad_files", "cap_chain.kicad_pcb")


def run():
    fails = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    # ---------------------------------------- foreign pour: continue, don't abort
    foreign = ZoneInfo(net_id=7, net_name='VCC', layer='In1.Cu')
    create, cont, replace, others = check_existing_zones(
        [foreign], 'In1.Cu', 'GND', 1)
    check(cont is True,
          "a foreign-net pour must NOT abort the run -- that is the filed bug")
    check(create is True, "the plane must still be created alongside it")
    check(replace is None, "a foreign pour must never be 'replaced' (deleted)")
    check([z.net_name for z in others] == ['VCC'],
          f"the foreign pour must be reported back to the caller, got {others}")

    # ------------------------------------------------- same net: replace as before
    same = ZoneInfo(net_id=1, net_name='GND', layer='In1.Cu')
    create, cont, replace, others = check_existing_zones([same], 'In1.Cu', 'GND', 1)
    check(cont is True and create is True, "same-net zone: still create")
    check(replace is same, "the SAME-net zone must be returned for replacement")
    check(others == [], "a same-net zone is not a foreign pour")

    # Same net matched by NAME only (net id unknown/0) still replaces.
    byname = ZoneInfo(net_id=0, net_name='GND', layer='In1.Cu')
    _c, _k, replace, _o = check_existing_zones([byname], 'In1.Cu', 'GND', 99)
    check(replace is byname, "same-net match by NAME must still replace")

    # -------------------------------------------- both same-net and foreign present
    create, cont, replace, others = check_existing_zones(
        [foreign, same], 'In1.Cu', 'GND', 1)
    check(cont is True and replace is same and [z.net_name for z in others] == ['VCC'],
          f"mixed layer: replace ours, coexist with theirs; got "
          f"replace={replace}, others={others}")

    # ------------------------------------- other layers and footprint pours ignored
    other_layer = ZoneInfo(net_id=7, net_name='VCC', layer='In2.Cu')
    _c, _k, replace, others = check_existing_zones([other_layer], 'In1.Cu', 'GND', 1)
    check(replace is None and others == [],
          "a pour on a DIFFERENT layer must be irrelevant")
    fp_zone = ZoneInfo(net_id=7, net_name='VCC', layer='In1.Cu', in_footprint=True)
    _c, _k, replace, others = check_existing_zones([fp_zone], 'In1.Cu', 'GND', 1)
    check(replace is None and others == [],
          "#478: a footprint-owned pour must neither veto the layer nor count as "
          "a contending board pour")

    # ---------------------------------------------------------- priority arithmetic
    check(shared_layer_zone_priority([]) == 0,
          "no contention -> priority 0, so untouched boards stay byte-identical")
    check(shared_layer_zone_priority([ZoneInfo(1, 'A', 'In1.Cu', priority=0)]) == 1,
          "one above a priority-0 incumbent")
    check(shared_layer_zone_priority([
              ZoneInfo(1, 'A', 'In1.Cu', priority=0),
              ZoneInfo(2, 'B', 'In1.Cu', priority=3)]) == 4,
          "one above the HIGHEST incumbent, so it wins against all of them")
    check(shared_layer_zone_priority(
              [ZoneInfo(1, 'A', 'In1.Cu', priority=0)]) > 0,
          "priority must stay NON-NEGATIVE: KiCad's parsers match \\d+, so a "
          "negative value is written and then read back as 0 (measured)")

    # ------------------------- same-net replacement: names, and multi-layer pours
    # On a KiCad 10 board a zone carries (net "NAME"), so passing only numeric
    # (net_id, layer) pairs could never match and "Replacing existing zone" was a
    # silent no-op -- every re-run appended another same-net pour (megadesk).
    V10_ONE_LAYER = ('(kicad_pcb\n'
                     '\t(zone\n\t\t(net "GND")\n\t\t(layer "B.Cu")\n'
                     '\t\t(uuid "z-single")\n\t\t(polygon\n\t\t\t(pts\n'
                     '\t\t\t\t(xy 0 0) (xy 5 0) (xy 5 5)\n\t\t\t)\n\t\t)\n\t)\n)\n')
    out = filter_zones_from_content(V10_ONE_LAYER, [(1, 'B.Cu')],
                                    zone_names_to_remove=[('GND', 'B.Cu')])
    check('z-single' not in out,
          "a single-layer KiCad 10 zone must be removed when its NAME pair is given")
    out = filter_zones_from_content(V10_ONE_LAYER, [(1, 'B.Cu')])
    check('z-single' in out,
          "without the name pairs the numeric matcher cannot fire on a v10 board "
          "-- this is the bug, kept as a regression witness for the caller")

    # A `(layers ...)` pour is ONE zone: removing it to replace ONE of its layers
    # would destroy copper on the others.
    V10_MULTI = ('(kicad_pcb\n'
                 '\t(zone\n\t\t(net "GND")\n\t\t(layers "F.Cu" "B.Cu")\n'
                 '\t\t(uuid "z-multi")\n\t\t(polygon\n\t\t\t(pts\n'
                 '\t\t\t\t(xy 0 0) (xy 5 0) (xy 5 5)\n\t\t\t)\n\t\t)\n\t)\n)\n')
    out = filter_zones_from_content(V10_MULTI, [(1, 'B.Cu')],
                                    zone_names_to_remove=[('GND', 'B.Cu')])
    check('z-multi' in out,
          "a multi-layer pour must NOT be deleted to replace one of its layers "
          "-- that silently removes the other layer's copper")
    out = filter_zones_from_content(
        V10_MULTI, [(1, 'B.Cu'), (1, 'F.Cu')],
        zone_names_to_remove=[('GND', 'B.Cu'), ('GND', 'F.Cu')])
    check('z-multi' not in out,
          "a multi-layer pour IS removable when every layer it covers is replaced")

    # check_existing_zones must reach the same conclusion from the span.
    multi = ZoneInfo(net_id=1, net_name='GND', layer='B.Cu', uuid='z-multi',
                     span=('F.Cu', 'B.Cu'))
    create, cont, replace, coexist = check_existing_zones([multi], 'B.Cu', 'GND', 1)
    check(replace is None,
          "a same-net MULTI-layer pour must not be offered as a replace target")
    check(coexist == [multi],
          "it must instead be reported as a pour to coexist with (and out-rank)")
    single = ZoneInfo(net_id=1, net_name='GND', layer='B.Cu', uuid='z-single',
                      span=('B.Cu',))
    _c, _k, replace, coexist = check_existing_zones([single], 'B.Cu', 'GND', 1)
    check(replace is single and coexist == [],
          "a same-net SINGLE-layer pour is still replaced outright")

    # -------------------------- main() must not run post-passes with no output file
    if not os.path.isfile(BOARD):
        print(f"  (skipped no-output guard: {BOARD} missing)")
    else:
        out = os.path.join(TESTS_DIR, "_zone_conflict_no_output.kicad_pcb")
        if os.path.exists(out):
            os.unlink(out)
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT_DIR, "route_planes.py"), BOARD, out,
             "--nets", "NOSUCHNET", "--plane-layers", "B.Cu"],
            capture_output=True, text=True, cwd=ROOT_DIR, timeout=600)
        combined = proc.stdout + proc.stderr
        check("Traceback" not in combined,
              "an engine exit that writes no board must not raise from the "
              f"post-passes; got:\n{combined[-1500:]}")
        check("FileNotFoundError" not in combined,
              "clean_plane_copper must not be handed a nonexistent output file")
        check("skipping the post-route passes" in combined,
              f"the skip should be stated plainly; got:\n{combined[-800:]}")
        check(not os.path.exists(out),
              "no output board should have been produced for an unknown net")
        if os.path.exists(out):
            os.unlink(out)

    if fails:
        for f in fails:
            print(f"  FAIL  {f}")
        print(f"\n{len(fails)} check(s) FAILED")
        return False
    print("PASS  plane zone conflict: coexist + warn, no abort, no post-pass crash")
    return True


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
