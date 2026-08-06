#!/usr/bin/env python3
"""Mode-0 island removal only applies when the net HAS a reachable anchor (#499).

KiCad's zone island removal is relative to the net's anchored fill: it deletes
islands that are isolated FROM the anchored part. With no anchor at all it keeps
every island rather than erasing the whole pour. Verified against KiCad 10 on a
board built from kbic65's own outline/zone/keep-out geometry:

    + a GND pad  -> F.Cu fills as 1 island  (4 unanchored strips deleted)
    - the GND pad -> F.Cu fills as 5 islands (nothing deleted)

kbic65 is the second case in the wild: GND has 3 pads, 0 vias, and all three sit
inside the board's copperpour keep-out band, so no island can touch an anchor.
All 482 islands survive and 199 are DRC-flagged isolated_copper. We assumed mode
0 had deleted them, so plane repair skipped every one and the post-write oracle
inherited 483 links to weld individually -- 6905s for that board.

An anchor inside a copperpour keep-out anchors NOTHING: no copper is poured
there to touch it. Pure geometry, no pcbnew/KiCad needed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plane_region_connector import (_net_has_pourable_anchor,
                                    _point_in_copperpour_keepout)

failures = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}: {got}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        failures.append(name)


class _Pad:
    def __init__(self, x, y, layers=('F.Cu',), drill=0.0):
        self.global_x, self.global_y = x, y
        self.layers, self.drill = list(layers), drill


class _Via:
    def __init__(self, x, y, net_id):
        self.x, self.y, self.net_id = x, y, net_id


class _Seg:
    def __init__(self, x, y, net_id, layer='F.Cu'):
        self.start_x = self.end_x = x
        self.start_y = self.end_y = y
        self.net_id, self.layer = net_id, layer


class _BI:
    def __init__(self, keepouts):
        self.keepouts = keepouts


class _PCB:
    def __init__(self, keepouts, pads=(), vias=(), segments=()):
        self.board_info = _BI(keepouts)
        self.pads_by_net = {1: list(pads)}
        self.vias = list(vias)
        self.segments = list(segments)


# A kbic65-shaped band: full width across the top of the board.
BAND = {'polygon': [(0, 0), (300, 0), (300, 30), (0, 30)], 'holes': [],
        'layers': {'F.Cu'}, 'copper_pour_allowed': False}
# A pour-allowed rule area must never count (it only bans tracks/vias).
TRACKS_ONLY = {'polygon': [(0, 100), (300, 100), (300, 130), (0, 130)],
               'holes': [], 'layers': {'F.Cu'}, 'copper_pour_allowed': True}


def main():
    print("1. point-in-keepout honours layer and the copperpour flag")
    pcb = _PCB([BAND])
    check("inside band", _point_in_copperpour_keepout(pcb, 150, 15, 'F.Cu'), True)
    check("outside band", _point_in_copperpour_keepout(pcb, 150, 60, 'F.Cu'), False)
    check("other layer", _point_in_copperpour_keepout(pcb, 150, 15, 'B.Cu'), False)
    check("pour-allowed area", _point_in_copperpour_keepout(
        _PCB([TRACKS_ONLY]), 150, 115, 'F.Cu'), False)

    print("2. the kbic65 case: every anchor sits inside the keep-out band")
    pcb = _PCB([BAND], pads=[_Pad(57, 17.9, drill=1.09),
                             _Pad(59, 17.4, drill=1.09),
                             _Pad(54, 12.6, drill=1.09)])
    check("no pourable anchor", _net_has_pourable_anchor(pcb, 1, 'F.Cu'), False)

    print("3. one pad outside the band is enough to anchor")
    pcb = _PCB([BAND], pads=[_Pad(57, 17.9, drill=1.09), _Pad(60, 90)])
    check("anchored", _net_has_pourable_anchor(pcb, 1, 'F.Cu'), True)

    print("4. a via or a same-layer track outside the band also anchors")
    check("via", _net_has_pourable_anchor(
        _PCB([BAND], vias=[_Via(60, 90, 1)]), 1, 'F.Cu'), True)
    check("track", _net_has_pourable_anchor(
        _PCB([BAND], segments=[_Seg(60, 90, 1)]), 1, 'F.Cu'), True)
    check("via inside band does NOT anchor", _net_has_pourable_anchor(
        _PCB([BAND], vias=[_Via(60, 15, 1)]), 1, 'F.Cu'), False)

    print("5. NO anchors on this layer at all -> NOT the kbic65 case")
    # Deliberately True. A net absent from this layer (an SMD pad on the other
    # side; an inner plane before its stitching vias exist) also has no anchored
    # island, so KiCad would keep its islands too -- but joining them would wire
    # them to each other and to NOTHING else, which is the copper clutter the
    # mode-0 policy exists to prevent. Only "anchors exist but every one is
    # keep-out blocked" gets the new treatment. Measured on the corpus: telling
    # these apart is the difference between flipping 3 of 244 mid-chain
    # (net, layer) pairs and flipping 0.
    check("pad on the other layer", _net_has_pourable_anchor(
        _PCB([], pads=[_Pad(60, 90, layers=('B.Cu',))]), 1, 'F.Cu'), True)
    check("no anchors anywhere", _net_has_pourable_anchor(
        _PCB([]), 1, 'F.Cu'), True)

    print("6. no keep-outs at all -> ordinary anchoring still works")
    check("plain board", _net_has_pourable_anchor(
        _PCB([], pads=[_Pad(60, 90)]), 1, 'F.Cu'), True)

    print()
    if failures:
        print(f"FAILED ({len(failures)}): {failures}")
        return 1
    print("PASS: mode-0 island removal is gated on a reachable anchor")
    return 0


if __name__ == '__main__':
    sys.exit(main())
