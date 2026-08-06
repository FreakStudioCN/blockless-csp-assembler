#!/usr/bin/env python3
"""Regression tests for issue #550 -- ``extract_board_bounds`` scanned Edge.Cuts
``gr_rect``/``gr_line``/``gr_arc``/``gr_poly`` but NOT ``gr_circle`` or
``gr_curve``, while ``_collect_edge_cuts_segments`` (the contour chainer) read
both. The two scans disagreed about the same board:

1. ROUND BOARD (the severe case): KiCad writes a circular outline as ONE
   ``gr_circle``. The contour side parsed it into a 64-point ring; the bbox side
   returned ``None``. ``obstacle_map.add_board_edge_obstacles`` opens with
   ``if not board_bounds: return``, so a round board got NO board-edge keep-out
   at all -- the router would run copper off the board with nothing to stop it.
   (``placement/quench`` raised ``ValueError("No board boundary (Edge.Cuts)
   found")`` and ``route_planes`` told the user to add an outline it already had.)

2. CURVED EDGE: a bezier's four CONTROL points do not bound the curve, so an
   outward-bulging ``gr_curve`` edge left the bbox short by the whole bulge.

3. CLI/GUI DIVERGENCE: the pcbnew path (``_extract_board_bounds_from_pcbnew``)
   unions each drawing's own bounding box, so circles counted there. A round
   board therefore read ``None`` on the CLI and correct bounds under the GUI.

The invariant worth pinning, and what test 4 asserts over the whole in-repo
corpus: the two scans read the SAME primitive set, so if ``board_outlines`` is
non-empty then ``board_bounds`` must contain every ring vertex.

    python3 tests/test_550_circle_curve_bounds.py
"""
import glob
import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

fails = []


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        fails.append(name)


_HDR = ('(kicad_pcb (version 20240108) (generator pcbnew)\n'
        '  (general (thickness 1.6))\n'
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n')

_STROKE = '(stroke (width 0.1) (type default))'


def _parse(text):
    from kicad_parser import parse_kicad_pcb
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 'b.kicad_pcb')
        with open(p, 'w') as f:
            f.write(text)
        return parse_kicad_pcb(p).board_info


def _contains(bounds, pts, tol=1e-6):
    """Every point inside the bbox?"""
    if bounds is None:
        return False
    x0, y0, x1, y1 = bounds
    return all(x0 - tol <= x <= x1 + tol and y0 - tol <= y <= y1 + tol
               for x, y in pts)


# --------------------------------------------------------------------------
# 1. A round board drawn as one gr_circle: bounds must be the circle's bbox,
#    NOT None. This is the case that disabled the edge keep-out entirely.
# --------------------------------------------------------------------------
def test_round_board_gr_circle():
    bi = _parse(_HDR + '  (gr_circle (center 50 50) (end 70 50) %s '
                       '(layer "Edge.Cuts"))\n)\n' % _STROKE)
    b = bi.board_bounds
    check("round gr_circle board: board_bounds is not None", b is not None)
    if b is None:
        return
    # center (50,50), radius 20 -> (30,30,70,70)
    check("round gr_circle board: bounds == circle bbox",
          all(abs(g - e) < 1e-6 for g, e in zip(b, (30.0, 30.0, 70.0, 70.0))))
    rings = bi.board_outlines or []
    check("round gr_circle board: outline still parses to a ring", len(rings) == 1)
    check("round gr_circle board: bounds contain every ring vertex",
          _contains(b, [p for r in rings for p in r]))


# --------------------------------------------------------------------------
# 2. A curve-cornered board: four sides with the right edge replaced by a
#    bezier bulging out to x=55. The control points reach x=60 but the CURVE
#    only reaches 55 -- so bounds must come from the linearized curve, and must
#    be >= the true extent (the pre-fix bbox stopped at the 40 of the corners).
# --------------------------------------------------------------------------
def test_curved_edge_gr_curve():
    bi = _parse(_HDR + '\n'.join([
        '  (gr_line (start 0 0) (end 40 0) %s (layer "Edge.Cuts"))' % _STROKE,
        '  (gr_curve (pts (xy 40 0) (xy 60 10) (xy 60 30) (xy 40 40)) %s '
        '(layer "Edge.Cuts"))' % _STROKE,
        '  (gr_line (start 40 40) (end 0 40) %s (layer "Edge.Cuts"))' % _STROKE,
        '  (gr_line (start 0 40) (end 0 0) %s (layer "Edge.Cuts"))' % _STROKE,
        ')', '']))
    b = bi.board_bounds
    check("gr_curve board: board_bounds is not None", b is not None)
    if b is None:
        return
    # Pre-fix this was (0,0,40,40) -- short by the whole 15mm bulge.
    check("gr_curve board: bbox includes the bezier bulge (max_x ~ 55)",
          abs(b[2] - 55.0) < 0.05)
    check("gr_curve board: bbox does NOT over-report to the control hull (60)",
          b[2] < 59.0)
    rings = bi.board_outlines or []
    check("gr_curve board: bounds contain every ring vertex",
          rings and _contains(b, [p for r in rings for p in r]))


# --------------------------------------------------------------------------
# 3. A gr_circle CUTOUT inside a rect outline must NOT change the bbox: the
#    fix must only ever add extent the outline really has (guards against a
#    naive "union every circle" that would still be right on test 1).
# --------------------------------------------------------------------------
def test_interior_circle_cutout_does_not_grow_bounds():
    bi = _parse(_HDR + '\n'.join([
        '  (gr_rect (start 0 0) (end 100 60) %s (layer "Edge.Cuts"))' % _STROKE,
        '  (gr_circle (center 50 30) (end 53 30) %s (layer "Edge.Cuts"))' % _STROKE,
        ')', '']))
    b = bi.board_bounds
    check("interior circle cutout: bounds stay the rect",
          b is not None and all(abs(g - e) < 1e-6
                                for g, e in zip(b, (0.0, 0.0, 100.0, 60.0))))


# --------------------------------------------------------------------------
# 4. A gr_circle on a NON-Edge.Cuts layer (silkscreen logo, courtyard) must be
#    ignored -- the layer filter still applies.
# --------------------------------------------------------------------------
def test_non_edge_cuts_circle_ignored():
    bi = _parse(_HDR + '\n'.join([
        '  (gr_rect (start 0 0) (end 20 20) %s (layer "Edge.Cuts"))' % _STROKE,
        '  (gr_circle (center 500 500) (end 600 500) %s (layer "F.SilkS"))' % _STROKE,
        '  (gr_curve (pts (xy 900 900) (xy 910 900) (xy 920 900) (xy 930 900)) %s '
        '(layer "B.SilkS"))' % _STROKE,
        ')', '']))
    b = bi.board_bounds
    check("off-layer circle/curve ignored: bounds stay the Edge.Cuts rect",
          b is not None and all(abs(g - e) < 1e-6
                                for g, e in zip(b, (0.0, 0.0, 20.0, 20.0))))


# --------------------------------------------------------------------------
# 5. A degenerate zero-radius gr_circle must not fabricate a bbox -- the
#    contour scan drops these (r > 1e-6), so the bbox scan must agree.
# --------------------------------------------------------------------------
def test_degenerate_circle_ignored():
    bi = _parse(_HDR + '  (gr_circle (center 10 10) (end 10 10) %s '
                       '(layer "Edge.Cuts"))\n)\n' % _STROKE)
    check("degenerate zero-radius circle: no bounds fabricated",
          bi.board_bounds is None)


# --------------------------------------------------------------------------
# 6-8. THE FOOTPRINT-LEVEL TWIN of the same bug. Boards keep outlines inside a
#    footprint (an "outline" footprint, split-keyboard halves), and the two
#    footprint scans had the identical asymmetry:
#
#      _footprint_edge_points      (bbox)    arc circle line poly rect
#      _collect_footprint_edge_segments      arc circle line      rect
#
#    fp_curve was read by NEITHER -- a footprint-embedded bezier edge was
#    invisible to both, so the bulge was missing from bounds AND the outline
#    never chained through it. fp_poly was bbox-only: bounds looked right while
#    board_outlines came back EMPTY, which silently degrades the keep-out to the
#    bounding box (concave regions outside the polygon go unblocked) and drops
#    footprint-embedded CUTOUTS entirely -- on tallytime_lora that was 2 real
#    holes the router would route straight through (0% blocked -> 100%).
# --------------------------------------------------------------------------
def _fp_board(shapes, at='(at 0 0)'):
    return (_HDR + '  (footprint "outline:board" (layer "F.Cu") %s\n'
            '    (property "Reference" "G1" (at 0 0) (layer "F.SilkS"))\n' % at
            + shapes + '\n  )\n)\n')


def test_footprint_fp_curve_outline():
    """Outline inside a footprint whose right edge is a bezier bulging to x=55."""
    bi = _parse(_fp_board('\n'.join([
        '    (fp_line (start 0 0) (end 40 0) %s (layer "Edge.Cuts"))' % _STROKE,
        '    (fp_curve (pts (xy 40 0) (xy 60 10) (xy 60 30) (xy 40 40)) %s '
        '(layer "Edge.Cuts"))' % _STROKE,
        '    (fp_line (start 40 40) (end 0 40) %s (layer "Edge.Cuts"))' % _STROKE,
        '    (fp_line (start 0 40) (end 0 0) %s (layer "Edge.Cuts"))' % _STROKE])))
    b = bi.board_bounds
    # Pre-fix: bounds (0,0,40,40) and a 4-point ring -- the curved edge was
    # missing from BOTH, so the bulge did not exist as far as the router knew.
    check("fp_curve: bbox includes the bezier bulge (max_x ~ 55)",
          b is not None and abs(b[2] - 55.0) < 0.05)
    check("fp_curve: bbox does NOT over-report to the control hull (60)",
          b is not None and b[2] < 59.0)
    rings = [r for r in (bi.board_outlines or []) if r]
    check("fp_curve: outline chains THROUGH the curve (not just the 3 lines)",
          bool(rings) and max(len(r) for r in rings) > 4)
    check("fp_curve: bounds contain every ring vertex",
          bool(rings) and _contains(b, [p for r in rings for p in r]))


def test_footprint_fp_poly_outline():
    """A whole outline drawn as one fp_poly must produce a real outline RING,
    not just bounds -- otherwise the keep-out silently falls back to the bbox."""
    bi = _parse(_fp_board(
        '    (fp_poly (pts (xy 0 0) (xy 30 0) (xy 30 20) (xy 15 25) (xy 0 20)) '
        '%s (layer "Edge.Cuts"))' % _STROKE))
    rings = [r for r in (bi.board_outlines or []) if r]
    check("fp_poly: contour scan yields an outline ring (was 0)", len(rings) == 1)
    check("fp_poly: ring carries the polygon vertices",
          bool(rings) and len(rings[0]) >= 5)
    check("fp_poly: bounds still the polygon extent",
          bi.board_bounds is not None
          and all(abs(g - e) < 1e-6
                  for g, e in zip(bi.board_bounds, (0.0, 0.0, 30.0, 25.0))))
    check("fp_poly: bounds contain every ring vertex",
          _contains(bi.board_bounds, [p for r in rings for p in r]))


def test_footprint_rotation_parity():
    """The two footprint scans use DIFFERENT transform helpers
    (_footprint_edge_points -> local_to_global, _collect_footprint_edge_segments
    -> its own to_g). If their rotation conventions disagreed, the ring would
    land outside the bbox. Pin it at a non-orthogonal angle and an offset."""
    shapes = '\n'.join([
        '    (fp_line (start 0 0) (end 40 0) %s (layer "Edge.Cuts"))' % _STROKE,
        '    (fp_curve (pts (xy 40 0) (xy 60 10) (xy 60 30) (xy 40 40)) %s '
        '(layer "Edge.Cuts"))' % _STROKE,
        '    (fp_line (start 40 40) (end 0 40) %s (layer "Edge.Cuts"))' % _STROKE,
        '    (fp_line (start 0 40) (end 0 0) %s (layer "Edge.Cuts"))' % _STROKE])
    for at in ('(at 12 34)', '(at 12 34 90)', '(at -5 7 37.5)'):
        bi = _parse(_fp_board(shapes, at=at))
        rings = [r for r in (bi.board_outlines or []) if r]
        ok = bool(rings) and _contains(bi.board_bounds,
                                       [p for r in rings for p in r], tol=1e-4)
        check("fp rotation parity %-16s bbox contains the chained ring" % at, ok)
        # Rotation-invariant evidence that the CURVE itself survived the
        # transform: a ring built from the 3 fp_lines alone is 4 points. (An
        # extent check would be wrong here -- the axis-aligned bbox of a
        # rotated shape is not the shape's own extent.)
        check("fp rotation parity %-16s curve present in the ring" % at,
              bool(rings) and max(len(r) for r in rings) > 4)
        # Only at orthogonal angles does the bbox equal the shape's extent.
        if bi.board_bounds and at.split()[-1].rstrip(')') in ('34', '90'):
            w = bi.board_bounds[2] - bi.board_bounds[0]
            h = bi.board_bounds[3] - bi.board_bounds[1]
            check("fp rotation parity %-16s extent preserved" % at,
                  abs(max(w, h) - 55.0) < 0.6)


# --------------------------------------------------------------------------
# 9. THE INVARIANT, over every board in kicad_files/: the bbox scan and the
#    contour chainer read the same primitive set, so a non-empty
#    board_outlines implies board_bounds contains every ring vertex.
# --------------------------------------------------------------------------
def test_corpus_bounds_contain_outlines():
    from kicad_parser import parse_kicad_pcb
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    boards = sorted(glob.glob(os.path.join(root, 'kicad_files', '*.kicad_pcb')))
    if not boards:
        print("  SKIP corpus invariant (no boards in kicad_files/)")
        return
    bad = []
    for p in boards:
        try:
            bi = parse_kicad_pcb(p).board_info
        except Exception as e:  # noqa: BLE001 -- parse failures aren't this bug
            print("    (skipped %s: %s)" % (os.path.basename(p), type(e).__name__))
            continue
        rings = [r for r in (bi.board_outlines or []) if r]
        if not rings:
            continue
        if not _contains(bi.board_bounds, [pt for r in rings for pt in r]):
            bad.append(os.path.basename(p))
    for name in bad:
        print("    outline outside board_bounds:", name)
    check("corpus (%d boards): board_bounds contains every outline vertex"
          % len(boards), not bad)


def run():
    test_round_board_gr_circle()
    test_curved_edge_gr_curve()
    test_interior_circle_cutout_does_not_grow_bounds()
    test_non_edge_cuts_circle_ignored()
    test_degenerate_circle_ignored()
    test_footprint_fp_curve_outline()
    test_footprint_fp_poly_outline()
    test_footprint_rotation_parity()
    test_corpus_bounds_contain_outlines()
    print()
    if fails:
        print("FAILED: %d check(s): %s" % (len(fails), fails))
        return 1
    print("All checks passed")
    return 0


if __name__ == '__main__':
    sys.exit(run())
