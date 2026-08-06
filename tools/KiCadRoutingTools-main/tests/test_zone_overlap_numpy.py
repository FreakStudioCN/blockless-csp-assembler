#!/usr/bin/env python3
"""Zone-overlap geometry must accept numpy scalars (regression for 9e6c48c).

9e6c48c ("Overlapping plane zones get DISTINCT fill priorities") added an
orientation test written as `(v > 1e-12) - (v < -1e-12)`. With plain Python
floats that works (bool is an int subclass); with numpy scalars `np.bool_ -
np.bool_` is REMOVED in numpy 2.x and raises TypeError. Zone points reach this
code as numpy scalars on some boards, so route_planes crashed outright -- step 2
wrote no board, every downstream step hit FileNotFoundError, and the board was
recorded as chain=BROKEN and silently DROPPED from corpus grading rather than
reported as a failure. 3 of the first 65 boards in the 0726a wave (urchin,
urchin_kb, glasgow_revC).

Boards must route identically whether their zone coordinates arrive as Python
floats or numpy scalars, so this pins BOTH.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from kicad_writer import _segments_properly_cross, _polygons_overlap

failures = []

def check(name, got, want):
    if got == want:
        print(f"  ok   {name}: {got}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        failures.append(name)

def as_np(pts):
    return [(np.float64(x), np.float64(y)) for x, y in pts]

def main():
    print("1. crossing segments, python floats vs numpy scalars")
    X = ((0., 0.), (1., 1.), (0., 1.), (1., 0.))          # transversal cross
    check("float cross", _segments_properly_cross(*X), True)
    check("numpy cross", _segments_properly_cross(*as_np(X)), True)

    print("2. non-crossing segments agree across both types")
    Y = ((0., 0.), (1., 0.), (0., 1.), (1., 1.))          # parallel
    check("float parallel", _segments_properly_cross(*Y), False)
    check("numpy parallel", _segments_properly_cross(*as_np(Y)), False)

    print("3. touching-but-not-crossing stays excluded (T junction)")
    T = ((0., 0.), (1., 0.), (0.5, 0.), (0.5, 1.))
    check("float T", _segments_properly_cross(*T), False)
    check("numpy T", _segments_properly_cross(*as_np(T)), False)

    print("4. _polygons_overlap: overlapping squares, both types")
    a = [(0., 0.), (2., 0.), (2., 2.), (0., 2.)]
    b = [(1., 1.), (3., 1.), (3., 3.), (1., 3.)]
    check("float overlap", bool(_polygons_overlap(a, b)), True)
    check("numpy overlap", bool(_polygons_overlap(as_np(a), as_np(b))), True)

    print("5. _polygons_overlap: disjoint squares, both types")
    c = [(10., 10.), (11., 10.), (11., 11.), (10., 11.)]
    check("float disjoint", bool(_polygons_overlap(a, c)), False)
    check("numpy disjoint", bool(_polygons_overlap(as_np(a), as_np(c))), False)

    print()
    if failures:
        print(f"FAILED ({len(failures)}): {failures}")
        return 1
    print("PASS: zone-overlap geometry is numpy-scalar safe")
    return 0

if __name__ == "__main__":
    sys.exit(main())
