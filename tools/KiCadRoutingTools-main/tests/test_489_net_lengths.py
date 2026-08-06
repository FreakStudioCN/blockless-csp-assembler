#!/usr/bin/env python3
"""#489 §7: per-net and pin-pair length measurement.

The filed defect was a documented recipe that could not run --
`calculate_route_length(pcb, net_id)` passes a PCBData where a segment LIST is
expected. These cover the supported replacements:

  * net_copper_length / net_copper_lengths agree with each other and with a
    hand-summed segment list, and the old mis-call still raises (so a future
    doc/skill drift back to it fails loudly rather than silently).
  * pin_pair_path_length on hand-built geometry with a KNOWN answer: a straight
    2-segment run, a T-branch whose stub must NOT be counted, a via layer change
    charged at the barrel length, a track landing INSIDE a via's copper rather
    than on its centre (real boards miss the centre by tens of um), and a broken
    net returning None.
  * The invariant that held across a corpus board: a pin-pair path can never
    exceed the net's total copper.

Run with:  python3 tests/test_489_net_lengths.py
"""
import math
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, "rust_router"))

from kicad_parser import (Segment, Via, Pad, PCBData, Net, BoardInfo,  # noqa: E402
                          StackupLayer)
from net_queries import (calculate_route_length, net_copper_length,  # noqa: E402
                         net_copper_lengths, pin_pair_path_length)

NET = 7
LAYERS = ['F.Cu', 'In1.Cu', 'B.Cu']


def _seg(x1, y1, x2, y2, layer='F.Cu', net_id=NET, width=0.2):
    return Segment(start_x=x1, start_y=y1, end_x=x2, end_y=y2,
                   width=width, layer=layer, net_id=net_id)


def _pad(x, y, layer='F.Cu', size=0.5, net_id=NET, ref='U1', num='1'):
    return Pad(pad_number=num, net_id=net_id, net_name='N', shape='rect',
               global_x=x, global_y=y, local_x=x, local_y=y,
               size_x=size, size_y=size, layers=[layer], drill=0.0,
               pad_type='smd', component_ref=ref)


def _via(x, y, layers=('F.Cu', 'B.Cu'), size=0.45, net_id=NET):
    return Via(x=x, y=y, size=size, drill=0.25, layers=list(layers), net_id=net_id)


def _board(segments, vias, pads, stackup=None):
    info = BoardInfo(layers={i: n for i, n in enumerate(LAYERS)},
                     copper_layers=list(LAYERS))
    if stackup is not None:
        info.stackup = stackup
    return PCBData(board_info=info, nets={NET: Net(net_id=NET, name='N')},
                   footprints={}, vias=list(vias), segments=list(segments),
                   pads_by_net={NET: list(pads)})


def run():
    fails = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    def close(a, b, tol=1e-6):
        return a is not None and abs(a - b) <= tol

    # --------------------------------------------------- net_copper_length API
    segs = [_seg(0, 0, 10, 0), _seg(10, 0, 10, 5)]
    pcb = _board(segs, [], [_pad(0, 0), _pad(10, 5, ref='U2')])
    hand = calculate_route_length(segs, None, pcb)
    check(close(net_copper_length(pcb, NET), 15.0),
          f"net_copper_length: expected 15.0, got {net_copper_length(pcb, NET)}")
    check(close(net_copper_length(pcb, NET), hand),
          "net_copper_length must equal the hand-sliced calculate_route_length")
    check(close(net_copper_lengths(pcb, [NET])[NET], 15.0),
          "net_copper_lengths must agree with net_copper_length")
    check(net_copper_lengths(pcb)[NET] == net_copper_lengths(pcb, [NET])[NET],
          "net_copper_lengths(None) must cover every net with copper")
    check(net_copper_length(pcb, 999) == 0.0,
          "a net with no copper must measure 0.0, not raise")

    # The mis-call from the filed defect must still fail loudly.
    try:
        calculate_route_length(pcb, NET)
        fails.append("calculate_route_length(pcb, net_id) must raise, not "
                     "silently return -- the #489 §7 recipe relied on it")
    except Exception:
        pass

    # ------------------------------------------------ pin-pair: straight route
    pa, pb = _pad(0, 0), _pad(10, 5, ref='U2')
    pcb = _board(segs, [], [pa, pb])
    check(close(pin_pair_path_length(pcb, NET, pa, pb), 15.0),
          f"straight 2-seg path: expected 15.0, got "
          f"{pin_pair_path_length(pcb, NET, pa, pb)}")

    # ------------------------------- pin-pair: T-branch stub must NOT be counted
    # A---------B  with a 4mm stub hanging off the middle.
    t_segs = [_seg(0, 0, 10, 0), _seg(5, 0, 5, 4)]
    pa, pb = _pad(0, 0), _pad(10, 0, ref='U2')
    pcb = _board(t_segs, [], [pa, pb])
    path = pin_pair_path_length(pcb, NET, pa, pb)
    check(close(path, 10.0), f"T-branch: path should be 10.0 (stub excluded), got {path}")
    check(close(net_copper_length(pcb, NET), 14.0),
          "T-branch: total copper should still count the 4mm stub")
    check(path < net_copper_length(pcb, NET),
          "T-branch: pin-pair path must be SHORTER than total copper")

    # ------------------------------------------- pin-pair: via barrel is charged
    stack = [StackupLayer(name='F.Cu', layer_type='copper', thickness=0.035),
             StackupLayer(name='dielectric 1', layer_type='core', thickness=1.0),
             StackupLayer(name='B.Cu', layer_type='copper', thickness=0.035)]
    v_segs = [_seg(0, 0, 5, 0, layer='F.Cu'), _seg(5, 0, 9, 0, layer='B.Cu')]
    pa = _pad(0, 0, layer='F.Cu')
    pb = _pad(9, 0, layer='B.Cu', ref='U2')
    pcb = _board(v_segs, [_via(5, 0)], [pa, pb], stackup=stack)
    barrel = pcb.get_via_barrel_length('F.Cu', 'B.Cu')
    path = pin_pair_path_length(pcb, NET, pa, pb)
    check(close(path, 9.0 + barrel, 1e-6),
          f"via path: expected {9.0 + barrel} (9mm track + {barrel}mm barrel), got {path}")
    check(barrel > 0, f"fixture sanity: stackup should give a barrel length, got {barrel}")

    # --------------------- pin-pair: track ends INSIDE via copper, not on centre
    # The real-board case: the endpoint misses the via centre by 43um, which a
    # coincidence-only walk would read as a break.
    off_segs = [_seg(0, 0, 5.0, 0, layer='F.Cu'),
                _seg(5.03, 0.02, 9, 0.02, layer='B.Cu')]
    pcb = _board(off_segs, [_via(5.0, 0.0)], [pa, _pad(9, 0.02, layer='B.Cu', ref='U2')],
                 stackup=stack)
    pb2 = pcb.pads_by_net[NET][1]
    path = pin_pair_path_length(pcb, NET, pa, pb2)
    check(path is not None,
          "a track landing inside the via's copper (not on its centre) must "
          "still be walkable -- got None")

    # ------------------------------------------------- pin-pair: broken net None
    gap_segs = [_seg(0, 0, 4, 0), _seg(6, 0, 10, 0)]
    pa, pb = _pad(0, 0), _pad(10, 0, ref='U2')
    pcb = _board(gap_segs, [], [pa, pb])
    check(pin_pair_path_length(pcb, NET, pa, pb) is None,
          "a 2mm gap between the two halves must return None, not a length")

    # ------------------------------------ pin-pair: zone-only net is not a path
    # No segments at all -> None (pours are deliberately not traversed).
    pcb = _board([], [], [pa, pb])
    check(pin_pair_path_length(pcb, NET, pa, pb) is None,
          "a net with no track copper must return None")

    if fails:
        for f in fails:
            print(f"  FAIL  {f}")
        print(f"\n{len(fails)} check(s) FAILED")
        return False
    print("PASS  #489 §7 net_copper_length / net_copper_lengths / pin_pair_path_length")
    return True


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
