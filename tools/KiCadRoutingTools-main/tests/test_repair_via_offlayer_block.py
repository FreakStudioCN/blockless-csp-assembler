"""Repair joiner via placement must respect copper OUTSIDE routing_layers
(audit H8): repair joins route only the plane layers, but every repair via
is a THROUGH via whose annulus spans the whole stack. build_base_obstacles
skipped foreign segments/pads on non-routing layers entirely, so a join via
could land its F.Cu annulus straight on a foreign F.Cu track or SMD pad
(obstacle_map.py grew the same guard earlier for butterstick DQ11).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from kicad_parser import parse_kicad_pcb  # noqa: E402
from routing_config import GridRouteConfig, GridCoord  # noqa: E402
from plane_region_connector import build_base_obstacles  # noqa: E402

BOARD = """(kicad_pcb (version 20240108) (generator test)
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG")
  (gr_line (start 0 0) (end 30 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 30 0) (end 30 20) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 30 20) (end 0 20) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 0 20) (end 0 0) (layer "Edge.Cuts") (width 0.1))
  (segment (start 8 10) (end 14 10) (width 0.2) (layer "F.Cu") (net 2) (uuid "00000000-0000-0000-0000-000000000001"))
  (segment (start 8 15) (end 14 15) (width 0.2) (layer "F.Cu") (net 1) (uuid "00000000-0000-0000-0000-000000000002"))
  (footprint "test:SMD" (at 20 10) (layer "F.Cu")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 "SIG"))
  )
)
"""


def main():
    with tempfile.NamedTemporaryFile(suffix='.kicad_pcb', mode='w',
                                     delete=False) as f:
        f.write(BOARD)
        path = f.name
    try:
        pcb = parse_kicad_pcb(path)
    finally:
        os.unlink(path)

    layers = ['In1.Cu', 'In2.Cu']
    config = GridRouteConfig(track_width=0.2, clearance=0.2, grid_step=0.1,
                             via_size=0.6, via_drill=0.3, layers=layers)
    obstacles, _ = build_base_obstacles(
        exclude_net_ids={1}, routing_layers=layers, pcb_data=pcb,
        config=config, track_width=0.2, track_via_clearance=0.3)
    coord = GridCoord(config.grid_step)

    # Foreign F.Cu track: via blocked even though F.Cu is not a routing layer.
    gx, gy = coord.to_grid(11.0, 10.0)
    assert obstacles.is_via_blocked(gx, gy), \
        "via NOT blocked on foreign off-layer track"

    # Foreign F.Cu-only SMD pad: same.
    gx, gy = coord.to_grid(20.0, 10.0)
    assert obstacles.is_via_blocked(gx, gy), \
        "via NOT blocked on foreign off-layer SMD pad"

    # Excluded (plane) net's own off-layer copper must NOT via-block.
    gx, gy = coord.to_grid(11.0, 15.0)
    assert not obstacles.is_via_blocked(gx, gy), \
        "via wrongly blocked on the plane net's own copper"

    # Open area stays open.
    gx, gy = coord.to_grid(25.0, 16.0)
    assert not obstacles.is_via_blocked(gx, gy), "open area blocked"

    print("PASS: repair via placement respects off-layer copper (H8)")


if __name__ == '__main__':
    main()
