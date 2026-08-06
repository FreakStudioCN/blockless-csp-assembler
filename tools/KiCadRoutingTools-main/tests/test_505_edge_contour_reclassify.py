#!/usr/bin/env python3
"""Regression test for issue #505 -- a pad-containing inner Edge.Cuts contour was
DROPPED outright, blinding both the router and check_drc to a real milled edge.

`_classify_contours` calls any ring nested inside another a CUTOUT. On a
panelized board the real board outline nests inside the panel frame, so it is
mis-read as a cutout; `drop_pad_containing_cutouts` (#291) then saw it enclosed
pads and deleted it, because treating it as a hole would have marked every
enclosed pad off-board (bus_pirate5: all 870 pads, chain break).

Deleting it was too strong. The contour is still Edge.Cuts -- KiCad mills the
board along it and grades copper against it -- so it must keep its EDGE
CLEARANCE role even though it has no HOLE role:

  * check_drc.board_edge_geometry must measure clearance against it (rings)
    without adding it to `cutouts` (the inside/on-board test).
  * add_board_edge_obstacles must block a clearance BAND around it, and never
    its interior -- these contours enclose pads by definition, so an interior
    fill would blank the board.

crkbd (a panelized split keyboard) shipped 85 items of copper laid straight
across the milled gap between its two halves, kicad-cli reporting "actual
0.0000 mm", none of which check_drc could see.

    python3 tests/test_505_edge_contour_reclassify.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from check_drc import board_edge_geometry, make_off_board_test, run_drc  # noqa: E402
from kicad_parser import parse_kicad_pcb  # noqa: E402

fails = []


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        fails.append(name)


def _rect_edge(x1, y1, x2, y2):
    pts = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    return "\n".join(
        f' (gr_line (start {pts[i][0]} {pts[i][1]}) '
        f'(end {pts[(i + 1) % 4][0]} {pts[(i + 1) % 4][1]}) '
        f'(layer "Edge.Cuts") (width 0.1))' for i in range(4))


# Panel frame 0..60 x 0..40 with the REAL board outline nested inside at
# 5..55 x 5..35, four pads inside it, and two tracks: one crossing the real
# outline, one comfortably interior.
CROSSING = ' (segment (start 40 20) (end 40 2) (width 0.2) (layer "F.Cu") (net 1) (uuid "x"))'
INTERIOR = ' (segment (start 20 20) (end 30 20) (width 0.2) (layer "F.Cu") (net 1) (uuid "i"))'

BOARD = f'''(kicad_pcb
 (version 20221018)
 (net 0 "")
 (net 1 "SIG")
{_rect_edge(0, 0, 60, 40)}
{_rect_edge(5, 5, 55, 35)}
 (footprint "t:c" (layer "F.Cu") (at 10 10)
  (property "Reference" "U1" (at 0 -2) (layer "F.SilkS"))
  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "SIG"))
  (pad "2" smd rect (at 2 0) (size 1 1) (layers "F.Cu") (net 1 "SIG"))
 )
 (footprint "t:c" (layer "F.Cu") (at 45 28)
  (property "Reference" "U2" (at 0 -2) (layer "F.SilkS"))
  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "SIG"))
  (pad "2" smd rect (at 2 0) (size 1 1) (layers "F.Cu") (net 1 "SIG"))
 )
{{items}}
)'''


def write_board(items):
    with tempfile.NamedTemporaryFile('w', suffix='.kicad_pcb', delete=False) as f:
        f.write(BOARD.format(items=items))
        return f.name


# --------------------------------------------------------------------------
# 1. Parser: the inner contour is RECLASSIFIED, not discarded
# --------------------------------------------------------------------------
path = write_board(INTERIOR)
try:
    pcb = parse_kicad_pcb(path)
    bi = pcb.board_info
    edge_contours = getattr(bi, 'board_edge_contours', None) or []
    check("pad-containing inner contour kept as a milled edge",
          len(edge_contours) == 1)
    check("...and NOT left in board_cutouts (would mark pads off-board)",
          not [c for c in (bi.board_cutouts or []) if len(c) >= 3])

    # 2. board_edge_geometry: in rings (clearance), out of cutouts (inside test)
    rings, outer, cutouts = board_edge_geometry(bi)
    check("contour is in the clearance ring set",
          any(len(r) == 4 and min(p[0] for p in r) == 5.0 for r in rings))
    check("contour is NOT in cutouts", not cutouts)

    # 3. #291 guard: pads it encloses stay ON board
    off_test = make_off_board_test(bi)
    pads = [p for f in pcb.footprints.values() for p in f.pads]
    n_off = sum(1 for p in pads if off_test and off_test(p.global_x, p.global_y))
    check(f"all {len(pads)} enclosed pads stay on-board (#291)", n_off == 0)
finally:
    os.unlink(path)

# --------------------------------------------------------------------------
# 4. check_drc flags copper crossing the reclassified contour, and leaves
#    genuinely interior copper alone.
# --------------------------------------------------------------------------
for label, items, want in (("crossing the milled inner outline", CROSSING, True),
                           ("well inside the board", INTERIOR, False)):
    p = write_board(items)
    try:
        v = [x for x in run_drc(p, clearance=0.2, quiet=True,
                                print_summary=False, board_edge_clearance=0.2)
             if 'board-edge' in x['type']]
        check(f"track {label} -> {'flagged' if want else 'clean'}",
              bool(v) == want)
    finally:
        os.unlink(p)

# --------------------------------------------------------------------------
# 5. Obstacle map: a BAND around the contour is blocked; the interior is not.
#    (An interior fill would blank the board -- these contours enclose pads.)
# --------------------------------------------------------------------------
path = write_board(INTERIOR)
try:
    pcb = parse_kicad_pcb(path)
    from obstacle_map import GridObstacleMap, add_board_edge_obstacles  # noqa: E402
    from routing_config import GridRouteConfig, GridCoord  # noqa: E402

    cfg = GridRouteConfig(layers=["F.Cu", "B.Cu"], grid_step=0.25,
                          clearance=0.2, track_width=0.2,
                          board_edge_clearance=0.5)
    obs = GridObstacleMap(len(cfg.layers))
    add_board_edge_obstacles(obs, pcb, cfg)
    coord = GridCoord(cfg.grid_step)

    def blocked(x, y):
        gx, gy = coord.to_grid(x, y)
        return obs.is_blocked(gx, gy, 0)

    check("cell ON the reclassified contour is blocked", blocked(30.0, 5.0))
    check("cell just inside the contour edge is blocked", blocked(30.0, 5.25))
    check("board interior stays routable (no cutout-style fill)",
          not blocked(30.0, 20.0) and not blocked(10.0, 10.0))
    # Scope note: the fix restores the CLEARANCE role only. Panel waste beyond
    # the real outline is still considered routable, because promoting the
    # contour to an outer ring would change the on-board test and re-open the
    # #291 pad-dropping risk. The band is what stops copper reaching the mill
    # line, which is what the DRC actually reports.
    check("blocking is a BAND, not everything past the contour",
          not blocked(30.0, 2.0))
finally:
    os.unlink(path)

print("-" * 60)
if fails:
    print(f"{len(fails)} FAILED: " + "; ".join(fails))
    sys.exit(1)
print("ALL PASS")
