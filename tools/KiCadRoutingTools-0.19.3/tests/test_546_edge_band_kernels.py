#!/usr/bin/env python3
"""#546: scanline/banded board-edge kernels vs the dense (cells x edges) ones.

The plane-repair profile put 71% of crkbd's runtime in _points_edge_distance
+ _points_inside_polygon re-run per obstacle-map rebuild. The replacements
must be BIT-identical where it matters:
- inside: identical everywhere (same x-intercept expression, strict `px < xi`
  tie handling preserved via searchsorted side='right');
- edge_dist: identical wherever the true distance is < threshold; >= threshold
  (up to +inf) elsewhere, so consumer comparisons against any clearance <=
  threshold answer identically.

Run:  python3 tests/test_546_edge_band_kernels.py
"""
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from obstacle_map import (_points_inside_polygon, _points_edge_distance,
                          _scanline_inside_rows, _banded_edge_distance_rows)

GRID = 0.05


def _axes_and_flat(gx_lo, gx_hi, gy_lo, gy_hi):
    gx_range = np.arange(gx_lo, gx_hi + 1, dtype=np.int32)
    gy_range = np.arange(gy_lo, gy_hi + 1, dtype=np.int32)
    gx_grid, gy_grid = np.meshgrid(gx_range, gy_range)
    px = gx_grid.ravel().astype(np.float64) * GRID
    py = gy_grid.ravel().astype(np.float64) * GRID
    px_axis = gx_range.astype(np.float64) * GRID
    py_axis = gy_range.astype(np.float64) * GRID
    return px_axis, py_axis, px, py


def _edges(poly):
    p = np.array(poly, dtype=np.float64)
    return p[:, 0], p[:, 1], np.roll(p[:, 0], -1), np.roll(p[:, 1], -1)


def _check(poly, gx_lo, gx_hi, gy_lo, gy_hi, threshold, clearances):
    px_axis, py_axis, px, py = _axes_and_flat(gx_lo, gx_hi, gy_lo, gy_hi)
    x1, y1, x2, y2 = _edges(poly)

    ins_old = _points_inside_polygon(px, py, x1, y1, x2, y2)
    ins_new = _scanline_inside_rows(px_axis, py_axis, x1, y1, x2, y2).ravel()
    assert np.array_equal(ins_old, ins_new), 'inside mask diverged'

    d_old = _points_edge_distance(px, py, x1, y1, x2, y2)
    d_new = _banded_edge_distance_rows(px_axis, py_axis, x1, y1, x2, y2,
                                       threshold).ravel()
    near = d_old < threshold
    assert np.array_equal(d_old[near], d_new[near]), \
        'near-band distances not bit-identical'
    assert np.all(d_new[~near] >= threshold), \
        'far cells fell below the threshold'
    for clr in clearances:
        assert clr <= threshold
        assert np.array_equal(d_old < clr, d_new < clr), f'< {clr} mask diverged'
        assert np.array_equal(d_old >= clr, d_new >= clr), f'>= {clr} mask diverged'


def test_simple_and_concave():
    # Rectangle-ish, concave L, and a sliver triangle.
    _check([(0.5, 0.5), (6.0, 0.5), (6.0, 4.0), (0.5, 4.0)],
           -10, 130, -10, 90, 0.35, [0.15, 0.3])
    _check([(0.0, 0.0), (5.0, 0.0), (5.0, 2.0), (2.0, 2.0), (2.0, 5.0),
            (0.0, 5.0)], -10, 110, -10, 110, 0.35, [0.15, 0.3])
    _check([(0.0, 0.0), (7.0, 0.05), (0.1, 0.4)], -10, 150, -10, 20,
           0.35, [0.15, 0.3])


def test_degenerate_and_horizontal_edges():
    # Repeated vertex (zero-length edge -> vertex-distance branch) and
    # horizontal edges (excluded from the ray cast by the y-condition).
    _check([(0.0, 0.0), (3.0, 0.0), (3.0, 0.0), (3.0, 3.0), (1.5, 3.0),
            (0.0, 3.0)], -8, 70, -8, 70, 0.35, [0.15, 0.3])


def test_vertex_on_scanline_ties():
    # Vertices landing EXACTLY on cell-center rows/columns exercise the
    # strict `px < xi` tie handling.
    _check([(0.5, 0.5), (2.5, 0.5), (2.5, 2.5), (0.5, 2.5)],
           0, 60, 0, 60, 0.35, [0.15, 0.3])
    # Diamond whose apex sits on a row of cell centers.
    _check([(1.0, 0.5), (2.0, 1.5), (1.0, 2.5), (0.0, 1.5)],
           -5, 50, -5, 55, 0.35, [0.15, 0.3])


def test_random_polygons():
    rng = np.random.default_rng(546)
    for trial in range(25):
        n = int(rng.integers(3, 14))
        ang = np.sort(rng.uniform(0, 2 * np.pi, n))
        r = rng.uniform(0.4, 4.0, n)
        poly = list(zip(3.0 + r * np.cos(ang), 3.0 + r * np.sin(ang)))
        thr = float(rng.uniform(0.1, 0.6))
        _check(poly, -40, 160, -40, 160, thr, [thr * 0.4, thr * 0.9, thr])


def test_crkbd_real_outline():
    board = os.path.expanduser(
        '~/Documents/kicad_stress_test/runs_set2/crkbd/crkbd_planes.kicad_pcb')
    if not os.path.exists(board):
        print('  (crkbd board not present -- skipping real-outline case)')
        return
    from kicad_parser import parse_kicad_pcb
    pcb = parse_kicad_pcb(board)
    outlines = [o for o in (getattr(pcb.board_info, 'board_outlines', None)
                            or []) if len(o) >= 3]
    if not outlines:
        outlines = [pcb.board_info.board_outline]
    ring = max(outlines, key=len)
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    # Sub-window straddling the outline's left edge (full board would be slow
    # for the DENSE reference kernel -- the whole point of #546).
    gx_lo = int(min(xs) / GRID) - 20
    gx_hi = gx_lo + 400
    gy_lo = int(min(ys) / GRID) - 20
    gy_hi = gy_lo + 400
    _check(ring, gx_lo, gx_hi, gy_lo, gy_hi, 0.4, [0.15, 0.3, 0.4])
    for cut in (getattr(pcb.board_info, 'board_cutouts', None) or [])[:10]:
        if len(cut) < 3:
            continue
        cxs = [p[0] for p in cut]
        cys = [p[1] for p in cut]
        _check(cut, int(min(cxs) / GRID) - 15, int(max(cxs) / GRID) + 15,
               int(min(cys) / GRID) - 15, int(max(cys) / GRID) + 15,
               0.4, [0.15, 0.3])


if __name__ == '__main__':
    test_simple_and_concave()
    test_degenerate_and_horizontal_edges()
    test_vertex_on_scanline_ties()
    test_random_polygons()
    test_crkbd_real_outline()
    print('All #546 kernel-equivalence tests passed')
