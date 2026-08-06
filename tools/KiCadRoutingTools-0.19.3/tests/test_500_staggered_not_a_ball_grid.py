#!/usr/bin/env python3
"""bga_fanout must refuse a STAGGERED multi-row package, fast (#500).

bga_fanout models a ball grid. `analyze_bga_grid` derives the pitch from gaps
between distinct pad coordinates, so on a staggered multi-row no-lead package
(AQFN) the two offset rows project onto each axis at HALF the real pad spacing.
Everything downstream is then budgeted against half the truth: osprey_kb's
Nordic_AQFN-73 reports `pitch 0.25` although no two of its 78 pads are closer
than 0.5mm, its under-pad escape budget evaluates to `via <= -0.20mm` --
impossible for ANY via -- and the run spent 2967s dropping balls and retrying
before finishing 39/39.

qfn_fanout does the same chip in 2.4s, 39/39, DRC-clean, with
`--escape-method underpad --allow-via-in-pad`. So bga_fanout should fail in a
second pointing at that, not grind for 49 minutes.

The guard requires BOTH signals -- nearest neighbour well above the lattice step
AND a mostly-empty lattice -- so a depopulated-centre BGA (still ON its lattice)
is not misread. Swept over the corpus it flags 15 of 151 array components, all
QFN/QFP family, and zero true BGA/PGA/LGA/CSP.

Pure geometry, no KiCad needed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bga_fanout.grid import analyze_bga_grid, staggered_lattice_diagnosis

failures = []


def check(name, got, want):
    if bool(got) == bool(want):
        print(f"  ok   {name}: {'flagged' if got else 'allowed'}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {'flagged' if want else 'allowed'}")
        failures.append(name)


class _Pad:
    def __init__(self, x, y):
        self.global_x, self.global_y = float(x), float(y)
        self.local_x, self.local_y = float(x), float(y)
        # analyze_bga_grid filters mechanical NPTH pads out of the pitch vote
        # (#513 item 3); these synthetic balls are copper pads.
        self.pad_type = 'smd'


class _FP:
    def __init__(self, pads, ref='U1', name='test'):
        self.pads = [_Pad(x, y) for x, y in pads]
        self.reference, self.footprint_name = ref, name


def _full_grid(n, pitch):
    return [(c * pitch, r * pitch) for r in range(n) for c in range(n)]


def _staggered_ring(n, pitch):
    """Two offset peripheral rows: neighbours `pitch` apart, lattice pitch/2."""
    pts = []
    half = pitch / 2.0
    for i in range(n):
        pts.append((i * pitch, 0.0))                 # outer row
        pts.append((i * pitch + half, half))         # inner row, offset
        pts.append((i * pitch, (n - 1) * pitch))
        pts.append((i * pitch + half, (n - 1) * pitch - half))
    for j in range(1, n - 1):
        pts.append((0.0, j * pitch))
        pts.append((half, j * pitch + half))
        pts.append(((n - 1) * pitch, j * pitch))
        pts.append(((n - 1) * pitch - half, j * pitch + half))
    return pts


def main():
    print("1. a true full ball grid is ALLOWED")
    fp = _FP(_full_grid(10, 0.5))
    check("10x10 @0.5", staggered_lattice_diagnosis(fp, analyze_bga_grid(fp)), False)

    print("2. a depopulated-centre BGA is ALLOWED (still on its lattice)")
    pts = [(c * 0.8, r * 0.8) for r in range(12) for c in range(12)
           if r in (0, 1, 10, 11) or c in (0, 1, 10, 11)]
    fp = _FP(pts)
    check("perimeter-only 12x12", staggered_lattice_diagnosis(fp, analyze_bga_grid(fp)), False)

    print("3. a STAGGERED multi-row ring is FLAGGED")
    fp = _FP(_staggered_ring(14, 0.5), name='Nordic_AQFN-73-1EP_7x7mm_P0.5mm')
    g = analyze_bga_grid(fp)
    why = staggered_lattice_diagnosis(fp, g)
    check("staggered AQFN-like", why, True)
    if why:
        print(f"       reason: {why}")

    print("4. the guard is opt-outable for deliberate runs")
    check("env override honoured (documented)",
          'KICAD_ALLOW_STAGGERED_BGA' in open(
              os.path.join(os.path.dirname(os.path.dirname(
                  os.path.abspath(__file__))), 'bga_fanout', '__init__.py')).read(),
          True)

    print()
    if failures:
        print(f"FAILED ({len(failures)}): {failures}")
        return 1
    print("PASS: staggered packages are refused, real grids are not")
    return 0


if __name__ == '__main__':
    sys.exit(main())
