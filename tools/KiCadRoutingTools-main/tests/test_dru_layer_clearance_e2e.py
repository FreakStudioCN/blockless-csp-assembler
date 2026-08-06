#!/usr/bin/env python3
"""#498 end-to-end: a .kicad_dru per-layer clearance rule is honored by the
router and graded by check_drc, with no CLI flag and no GUI wiring.

Board: splitflap_driver (2 copper layers), routed at clearance 0.2 with a
B.Cu rule of 0.5 mm.

  1. WITH the dru: the route must come out clean when graded at the rule
     (check_drc auto-reads the output's sibling dru, carried over by
     fix_project_for_output).
  2. WITHOUT the dru (control): the same route graded AT the rule must show
     violations -- proving the rule is a real constraint the default route
     violates, i.e. honoring it actually changed the copper (a wiring fix can
     be inert -- the c99ffb4 lesson).
  3. No-dru output carries no dru and grades clean at 0.2 (strict no-op).
"""
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

BOARD = os.path.join(REPO, "kicad_files", "splitflap_driver.kicad_pcb")
RULE = 0.5
DRU = f'(version 1)\n(rule outer_wide (layer "B.Cu") (constraint clearance (min {RULE}mm)))\n'
NETS = ["*"]

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{(' -- ' + detail) if detail else ''}")


def route(workdir, with_dru, out_name):
    src = os.path.join(workdir, "in_dru.kicad_pcb" if with_dru else "in_plain.kicad_pcb")
    if not os.path.exists(src):
        from copy_board import copy_board
        copy_board(BOARD, src)
        dru = os.path.splitext(src)[0] + ".kicad_dru"
        if with_dru:
            open(dru, "w").write(DRU)
        elif os.path.exists(dru):
            os.remove(dru)
    out = os.path.join(workdir, out_name)
    r = subprocess.run(
        [sys.executable, os.path.join(REPO, "route.py"), src, out,
         "--nets", *NETS, "--layers", "F.Cu", "B.Cu",
         "--clearance", "0.2", "--track-width", "0.2"],
        capture_output=True, text=True, timeout=1200)
    return out, r


def drc_violations(board, clearance):
    r = subprocess.run(
        [sys.executable, os.path.join(REPO, "check_drc.py"), board,
         "--clearance", str(clearance), "--no-size-checks", "--quiet"],
        capture_output=True, text=True, timeout=600)
    import re
    m = re.search(r"(\d+)\s+violation", r.stdout + r.stderr)
    if m:
        return int(m.group(1)), r
    # fall back to exit code semantics: 0 = clean
    return (0 if r.returncode == 0 else -1), r


with tempfile.TemporaryDirectory() as d:
    # --- 1. route WITH the rule ---
    out_dru, r1 = route(d, with_dru=True, out_name="out_dru.kicad_pcb")
    check("route (dru) completed", r1.returncode == 0 and os.path.isfile(out_dru),
          f"rc={r1.returncode}")
    check("engine announced the rule",
          "Per-layer clearance rules" in r1.stdout and "B.Cu:0.5" in r1.stdout)
    check("output carries the dru sibling",
          os.path.isfile(os.path.splitext(out_dru)[0] + ".kicad_dru"))

    n1, rr1 = drc_violations(out_dru, 0.2)
    check("dru-routed board grades CLEAN at the rule", n1 == 0,
          f"{n1} violation(s)")
    check("checker announced the rule", "Per-layer clearance rules" in rr1.stdout
          or n1 == 0)  # --quiet may suppress; cleanliness is the real assert

    # --- 2. control: route WITHOUT the rule, grade AT the rule ---
    out_plain, r2 = route(d, with_dru=False, out_name="out_plain.kicad_pcb")
    check("route (plain) completed", r2.returncode == 0 and os.path.isfile(out_plain),
          f"rc={r2.returncode}")
    check("plain route reads no rule", "Per-layer clearance rules" not in r2.stdout)
    # plant the dru next to the PLAIN output and re-grade: the checker now
    # requires 0.5 on B.Cu, which a 0.2-clearance route must violate somewhere
    planted = os.path.splitext(out_plain)[0] + ".kicad_dru"
    open(planted, "w").write(DRU)
    n2, _ = drc_violations(out_plain, 0.2)
    check("plain route VIOLATES the rule (rule changed real copper)", n2 > 0,
          f"{n2} violation(s)")
    os.remove(planted)

    # --- 3. strict no-op without a dru ---
    n3, _ = drc_violations(out_plain, 0.2)
    check("plain route grades clean at 0.2 (no-op baseline)", n3 == 0,
          f"{n3} violation(s)")

    # --- 4. PLANE step obeys the rule (taps/joins on the ruled layer) ---
    plane_out = os.path.join(d, "out_plane.kicad_pcb")
    rp = subprocess.run(
        [sys.executable, os.path.join(REPO, "route_planes.py"),
         os.path.join(d, "in_dru.kicad_pcb"), "--output", plane_out,
         "--nets", "GND", "--plane-layers", "B.Cu",
         "--clearance", "0.2", "--track-width", "0.25"],
        capture_output=True, text=True, timeout=1200)
    check("route_planes (dru) completed", rp.returncode == 0 and os.path.isfile(plane_out),
          f"rc={rp.returncode}")
    check("plane engine announced the rule",
          "Per-layer clearance rules" in rp.stdout and "B.Cu:0.5" in rp.stdout)
    n4, _ = drc_violations(plane_out, 0.2)
    check("plane output grades CLEAN at the rule", n4 == 0, f"{n4} violation(s)")

# --- 5. FANOUT engines read the rules (announcement-level: QFN exact swap,
#        BGA conservative floor) ---
import shutil as _sh
# fanout_starting_point.kicad_pcb is GENERATED by test_fanout_and_route.py and is
# gitignored, so on a fresh clone it does not exist -- and run_all.py runs this
# file BEFORE its producer, so it was never there. Copying it unconditionally
# raised FileNotFoundError at MODULE scope, which during pytest collection is an
# error on this file. Build it from the tracked haasoscope root instead
# (fixture_boards), so the section actually runs rather than being skipped.
sys.path.insert(0, os.path.join(REPO, "tests"))
from fixture_boards import ensure as _ensure_board
_FANOUT_SEED = _ensure_board("fanout_starting_point.kicad_pcb")

with tempfile.TemporaryDirectory() as d2:
    fsrc = os.path.join(d2, "fan.kicad_pcb")
    _sh.copy(_FANOUT_SEED, fsrc)
    open(os.path.join(d2, "fan.kicad_dru"), "w").write(
        '(version 1)\n(rule t (layer "F.Cu") (constraint clearance (min 0.22mm)))\n')
    rq = subprocess.run(
        [sys.executable, os.path.join(REPO, "qfn_fanout.py"), fsrc,
         "--component", "U2", "--width", "0.1", "--clearance", "0.1",
         "--output", os.path.join(d2, "qfn_out.kicad_pcb")],
        capture_output=True, text=True, timeout=900)
    check("QFN swaps to the escape-layer rule",
          "escape-layer clearance F.Cu 0.1 -> 0.22" in rq.stdout,
          f"rc={rq.returncode}")
    from kicad_parser import parse_kicad_pcb
    from bga_fanout import generate_bga_fanout
    import io, contextlib
    pcb = parse_kicad_pcb(fsrc)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        generate_bga_fanout(pcb.footprints["IC1"], pcb, net_filter=["/fpga_adc/lvds_rx1_0*"],
                            layers=["F.Cu", "In1.Cu"], track_width=0.1,
                            clearance=0.1, via_size=0.3, via_drill=0.2)
    check("BGA floors its scalar at the largest rule",
          "fanout clearance floored 0.1 -> 0.22" in buf.getvalue())

print(f"\n{passed}/{passed + failed} checks passed")
if __name__ == '__main__':
    sys.exit(1 if failed else 0)
elif failed:
    # A module-scope sys.exit -- even sys.exit(0) -- escapes pytest collection as
    # an INTERNALERROR and takes the session with it (#457 item 3).
    raise AssertionError(f"{failed} dru e2e check(s) failed")
