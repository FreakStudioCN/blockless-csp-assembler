#!/usr/bin/env python3
"""#485 stitch-site gates: floating pours are skipped, anchored pours stitch.

kbic65 finding: the board's GND net is 3 pads + 3 tracks inside a
copperpour-keepout band, so the full-board GND pours are FLOATING copper.
The old gate asked only "is there 2-layer fill at this site" -- true almost
everywhere -- and placed 38 stitch vias, each of which became a KiCad
ratsnest item disconnected from the pad network; the next repair step's
oracle then strapped to them for 2045s and left 4 min-copper-web necks.

Two gates now sit in front of placement:
  1. anchor gate (model-only, no KiCad needed): the net's main pour must
     hold at least one same-net pad or via, else there is nothing to bond;
  2. exact-fill gate (when KiCad python is available): a site must land on
     the net's MAIN (pad-connected) cluster of KiCad's own filled islands.

This test isolates gate 1 with a synthetic board: the same pour with the
net's only pad outside it must be skipped; with a plated pad inside it,
stitching must place vias. (Gate 2 needs pcbnew and is covered by the
corpus replay, not here; without a source file it degrades to gate 1 with
a printed warning, which is also the path this test exercises.)

Run:  python3 tests/test_485_stitch_gates.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from kicad_parser import BoardInfo, Net, PCBData, Pad
from routing_config import GridCoord, GridRouteConfig
from route_planes import _stitch_plane_area_vias


def _board(pad_x, pad_y):
    pad = Pad(component_ref="U1", pad_number="1", global_x=pad_x,
              global_y=pad_y, local_x=0.0, local_y=0.0, size_x=0.8,
              size_y=0.8, shape="circle", layers=["*.Cu"], net_id=1,
              net_name="GND", drill=0.4, pad_type="thru_hole")
    bi = BoardInfo(layers={0: "F.Cu", 31: "B.Cu"},
                   copper_layers=["F.Cu", "B.Cu"])
    bi.board_bounds = (0.0, 0.0, 40.0, 40.0)
    net = Net(net_id=1, name="GND", pads=[pad])
    pcb = PCBData(board_info=bi, nets={1: net}, footprints={}, vias=[],
                  segments=[], pads_by_net={1: [pad]})
    return pcb


def _zone_data():
    """One 25x25mm GND pour per layer, well inside the 40x40 board."""
    poly = [(5.0, 5.0), (30.0, 5.0), (30.0, 30.0), (5.0, 30.0)]
    return [{"net_id": 1, "layer": layer, "clearance": 0.2,
             "min_thickness": 0.25, "polygon_points": poly}
            for layer in ("F.Cu", "B.Cu")]


def _stitch(pcb):
    config = GridRouteConfig(track_width=0.15, clearance=0.15, via_size=0.45,
                             via_drill=0.25, grid_step=0.1,
                             board_edge_clearance=0.3,
                             hole_to_hole_clearance=0.25)
    return _stitch_plane_area_vias(
        pcb, ["GND", "GND"], ["F.Cu", "B.Cu"], [1, 1], _zone_data(),
        config, GridCoord(config.grid_step), 10.0, 0.45, 0.25, 0.25, -1.0,
        stitch_lattice=True, stitch_edge_fence=False)


def run():
    fails = []

    # Pad OUTSIDE the pour: the pour system is floating copper -- the anchor
    # gate must skip the net entirely (kbic65 would have been 38 vias here).
    vias = _stitch(_board(37.0, 37.0))
    if vias:
        fails.append(f"floating pour was stitched: {len(vias)} via(s)")

    # Plated pad INSIDE the pour: anchored -- the lattice must place vias.
    vias = _stitch(_board(17.0, 17.0))
    if not vias:
        fails.append("anchored pour placed no stitch vias")
    else:
        poly_min, poly_max = 5.0, 30.0
        off = [v for v in vias
               if not (poly_min <= v["x"] <= poly_max
                       and poly_min <= v["y"] <= poly_max)]
        if off:
            fails.append(f"{len(off)} stitch via(s) outside the pour")

    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        return 1
    print("PASS: floating pour skipped, anchored pour stitched")
    return 0


if __name__ == "__main__":
    sys.exit(run())
