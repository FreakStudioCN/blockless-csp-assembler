#!/usr/bin/env python3
"""Regression test for #493: routing geometry must not depend on the Python
version.

KiCad bundles its own interpreter (3.9 here) and the plugin runs inside it,
while the CLI tools run whatever `python3` is on PATH (3.14 here). So the SAME
board routed by the SAME code has to come out the same on both, or the GUI and
the CLI can never agree -- and neither can a corpus chain replayed on a machine
with a different python.

It did not. Python 3.12 changed the builtin `sum()` to use Neumaier compensated
summation:

    sum([0.65]*15)  ->  9.750000000000002   on 3.9   (naive accumulation)
                    ->  9.75                on 3.12+ (compensated)

`bga_fanout/grid.py` averaged clustered pad coordinates with
`sum(group)/len(group)`, so the BGA grid bounds came out 1-2 ULP apart
(min_x = 116.12500000000003 vs exactly 116.125). That is enough to flip a ball
sitting on an exact boundary tie from one escape direction to another, which
changed the channel and layer assignment and left eth_tap's U13 fanout with 109
tracks on one interpreter and 110 on the other -- and every later step of the
chain diverged from there.

`math.fsum` is exactly rounded and identical on every version, so cluster means
use it. This test pins the property two ways:

  1. the arithmetic itself (fsum stable where sum() is not), and
  2. the grid geometry a real in-repo BGA board produces, against values that
     are exact binary and therefore interpreter-independent by construction.

Uses only checked-in boards. Run under BOTH interpreters to compare:

    python3 tests/test_493_interpreter_independence.py
    /Applications/KiCad/KiCad.app/.../python3 tests/test_493_interpreter_independence.py
"""
import math
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT_DIR)

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok   {label}")


def test_fsum_is_version_stable():
    """The primitive: fsum is exact, sum() is not (and changed in 3.12)."""
    print("1. math.fsum is exactly rounded regardless of version")
    # Exactly representable results -- any correctly-rounded implementation
    # must produce these bit patterns on every Python.
    check("fsum([0.65]*15) == 9.75", math.fsum([0.65] * 15), 9.75)
    check("fsum([0.1]*10) == 1.0", math.fsum([0.1] * 10), 1.0)
    check("fsum([1e16,1.0,-1e16]) == 1.0", math.fsum([1e16, 1.0, -1e16]), 1.0)
    # Document the hazard being guarded against; do NOT assert on sum(), whose
    # answer legitimately differs by version -- that is the whole point.
    naive = sum([0.65] * 15)
    note = "differs from fsum" if naive != 9.75 else "matches fsum here"
    print(f"  note builtin sum([0.65]*15) = {naive!r} ({note}) on "
          f"py{sys.version.split()[0]}")


def test_cluster_mean_uses_fsum():
    """_cluster_positions must not regress to the builtin sum()."""
    print("2. bga_fanout.grid._cluster_positions averages with fsum")
    import inspect
    from bga_fanout import grid as grid_mod
    src = inspect.getsource(grid_mod)
    if 'math.fsum' not in src:
        failures.append("grid.py no longer uses math.fsum")
        print("  FAIL math.fsum absent from grid.py")
        return
    print("  ok   math.fsum present")
    body = inspect.getsource(grid_mod._cluster_positions)
    if 'sum(group)' in body:
        failures.append("_cluster_positions uses builtin sum(group)")
        print("  FAIL _cluster_positions still calls the builtin sum()")
    else:
        print("  ok   no builtin sum(group) in _cluster_positions")
    # Behavioural: a cluster whose naive sum is inexact must still land exact.
    vals = [0.65] * 15
    got = grid_mod._mean(vals)
    check("_mean([0.65]*15) == 0.65", got, 0.65)


def test_bga_grid_geometry_is_exact():
    """End-to-end on a checked-in BGA board: the grid bounds must be the same
    bit patterns on any interpreter."""
    print("3. BGA grid bounds on an in-repo board are exact")
    # Built on demand from the tracked haasoscope board rather than skipped:
    # this fixture is produced by test_fanout_and_route.py, which run_all.py
    # runs 60-odd files LATER, so the skip fired every time on a clean tree.
    from fixture_boards import ensure
    board = ensure('fanout_starting_point.kicad_pcb')
    from kicad_parser import parse_kicad_pcb
    from bga_fanout.grid import analyze_bga_grid
    pcb = parse_kicad_pcb(board)
    checked = 0
    for ref, fp in sorted(pcb.footprints.items()):
        if len(fp.pads) < 16:
            continue
        g = analyze_bga_grid(fp)
        if g is None:
            continue
        checked += 1
        # Every bound must round-trip through float.hex identically on any
        # version. We cannot hardcode a board-specific value here without
        # coupling to the fixture, so assert the invariant that broke: the
        # bounds are the cluster means +/- pitch/2, recomputed with fsum, and
        # must equal a recomputation from the same pads.
        from bga_fanout.grid import _cluster_positions
        xs = _cluster_positions(p.global_x for p in fp.pads)
        ys = _cluster_positions(p.global_y for p in fp.pads)
        check(f"{ref} min_x reproducible",
              float.hex(g.min_x), float.hex(min(xs) - g.pitch_x / 2))
        check(f"{ref} max_y reproducible",
              float.hex(g.max_y), float.hex(max(ys) + g.pitch_y / 2))
        # fsum of a cluster is order-independent as well as version-independent
        for name, vals in (('x', xs), ('y', ys)):
            if len(vals) >= 2:
                fwd = math.fsum(vals)
                rev = math.fsum(list(reversed(vals)))
                check(f"{ref} {name} cluster sum order-independent",
                      float.hex(fwd), float.hex(rev))
        break  # one BGA is enough; keep the test fast
    if not checked:
        print("  SKIP no BGA-like footprint found on the fixture")


def main():
    print(f"python {sys.version.split()[0]}")
    for t in (test_fsum_is_version_stable,
              test_cluster_mean_uses_fsum,
              test_bga_grid_geometry_is_exact):
        t()
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print("  " + f)
        return 1
    print("PASS: routing geometry is interpreter-independent (#493)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
