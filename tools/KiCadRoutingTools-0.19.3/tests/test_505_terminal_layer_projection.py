"""#505: the terminal connector must not project a target onto a layer where
the net has no copper.

_path_to_segments_vias draws a stub from the last grid point to the ORIGINAL
endpoint, on the layer the A* actually finished on rather than the target's
own layer. That is right for a plated-through pad -- its barrel puts own-net
copper on every layer, so the stub lands on real copper. It is a fabrication
when the target is layer-specific: the stub joins nothing, and because
terminal cells are obstacle-EXEMPT it gets stamped straight through whatever
occupies that layer.

cparti_fpga hit exactly that: a +3V3 plane-repair route aimed at an F.Cu
target, finished on In2.Cu 0.825mm short, and the connector was laid along an
FPGA_CFG_D3 track on In2.Cu -- a dead short at 0.000mm separation.

Case A (no through-feature at the target) must emit NO In2.Cu stub.
Case B (a plated-through pad there) must still emit it -- the guard must not
over-suppress legitimate through-hole terminals.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from kicad_parser import parse_kicad_pcb  # noqa: E402
from routing_config import GridRouteConfig, GridCoord  # noqa: E402
from single_ended_routing import _path_to_segments_vias  # noqa: E402

HEADER = """(kicad_pcb (version 20240108) (generator test)
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "+3V3")
  (net 2 "FPGA_CFG_D3")
  (gr_line (start 190 70) (end 210 70) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 210 70) (end 210 90) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 210 90) (end 190 90) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 190 90) (end 190 70) (layer "Edge.Cuts") (width 0.1))
  (segment (start 201.275 78.8) (end 200.35 78.8) (width 0.0889) (layer "In2.Cu") (net 2) (uuid "00000000-0000-0000-0000-000000000001"))
"""

# The target sits on F.Cu only -- nothing ties In2.Cu to it.
BOARD_NO_THT = HEADER + ")\n"

# Same board, but a plated-through +3V3 pad at the target: the barrel really
# does carry +3V3 copper on In2.Cu, so the stub is legitimate.
BOARD_THT = HEADER + """  (footprint "test:THT" (at 200.8 78.8) (layer "F.Cu")
    (pad "1" thru_hole circle (at 0 0) (size 0.6 0.6) (drill 0.3) (layers "*.Cu") (net 1 "+3V3"))
  )
)
"""

LAYER_NAMES = ['F.Cu', 'In2.Cu']
# A* finishes on In2.Cu at (201.0, 78.0), 0.825mm from the F.Cu target.
PATH = [(2012, 779, 1), (2011, 780, 1), (2010, 780, 1)]
START_ORIG = (201.2, 77.9, 'In2.Cu')
END_ORIG = (200.8, 78.8, 'F.Cu')


def _parse(text):
    with tempfile.NamedTemporaryFile(suffix='.kicad_pcb', mode='w',
                                     delete=False) as f:
        f.write(text)
        path = f.name
    try:
        return parse_kicad_pcb(path)
    finally:
        os.unlink(path)


def _emit(pcb):
    config = GridRouteConfig(track_width=0.127, clearance=0.1, grid_step=0.1,
                             via_size=0.45, via_drill=0.3, layers=LAYER_NAMES)
    segs, _vias = _path_to_segments_vias(
        PATH, GridCoord(config.grid_step), LAYER_NAMES, 1, config,
        START_ORIG, END_ORIG, None, pcb)
    return segs


def _stub_at_target(segs):
    """In2.Cu segments touching the F.Cu target XY -- the shorting stub."""
    tx, ty = END_ORIG[0], END_ORIG[1]
    hits = []
    for s in segs:
        if s.layer != 'In2.Cu':
            continue
        for (px, py) in ((s.start_x, s.start_y), (s.end_x, s.end_y)):
            if abs(px - tx) < 0.02 and abs(py - ty) < 0.02:
                hits.append(s)
                break
    return hits


def main():
    failures = []

    stubs = _stub_at_target(_emit(_parse(BOARD_NO_THT)))
    if stubs:
        s = stubs[0]
        failures.append(
            "layer-specific target was projected onto In2.Cu: stub "
            f"({s.start_x},{s.start_y})-({s.end_x},{s.end_y}) w={s.width} "
            "lands on the FPGA_CFG_D3 track (expected: no stub emitted)")
    else:
        print("PASS: no In2.Cu stub for an F.Cu-only target")

    stubs = _stub_at_target(_emit(_parse(BOARD_THT)))
    if not stubs:
        failures.append(
            "plated-through +3V3 pad at the target: the In2.Cu connector was "
            "suppressed, but the barrel really does carry copper there")
    else:
        print("PASS: plated-through terminal still gets its connector")

    if failures:
        print("\nFAIL:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == '__main__':
    sys.exit(main())
