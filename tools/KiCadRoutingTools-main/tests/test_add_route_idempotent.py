#!/usr/bin/env python3
"""A result's copper lands on the board EXACTLY ONCE (ulx3s).

add_route_to_pcb_data is the single choke point every path uses to commit a
routing result: initial route, retry, polarity swap, and rip_up_reroute's
``trace_event='restore'``. The restore/retry paths hand back a SAVED result
whose Segment/Via objects can still be on the board, and the commit appended
them unconditionally -- putting the same object into pcb_data.segments twice.

That is always a bug, and it is invisible to KiCad:
  * the write model holds each object once, so the BOARD/FILE ledgers report
    phantom "board-only" copper and the file legitimately disagrees with the
    model;
  * obstacles get double-stamped (the #208/#309 ref-count class);
  * gates that treat pcb_data.segments as a node set are defeated (#195).

ulx3s shipped it in the 0804 arch sweep, in the same breath as "Dropped 2
superseded rip-reroute result(s) from the write-list":
    DUPLICATE board entry: net 38 (142.88,81.0)-(142.605,80.725) F.Cu
    DUPLICATE board via:   net 93 at (140.48,83.4)
    -> FPDI_D0+ segs board-only=2, GN27 vias board-only=2

    python3 tests/test_add_route_idempotent.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from kicad_parser import PCBData, Segment, Via  # noqa: E402
from pcb_modification import add_route_to_pcb_data  # noqa: E402

_fail = []


def check(label, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}: {label}" + (f"  ({detail})" if detail and not cond else ''))
    if not cond:
        _fail.append(label)


def board():
    return PCBData(board_info=None, nets={}, footprints={}, vias=[],
                   segments=[], pads_by_net={})


def seg(x0, y0, x1, y1, net=38, layer='F.Cu', w=0.12):
    return Segment(start_x=x0, start_y=y0, end_x=x1, end_y=y1,
                   width=w, layer=layer, net_id=net)


def dup_ids(lst):
    seen, dups = set(), 0
    for o in lst:
        if id(o) in seen:
            dups += 1
        seen.add(id(o))
    return dups


def main():
    print("1. committing a result twice does not duplicate it (the ulx3s shape)")
    pcb = board()
    s = seg(142.605, 80.725, 142.88, 81.0)
    v = Via(x=140.48, y=83.4, size=0.45, drill=0.2,
            layers=['F.Cu', 'B.Cu'], net_id=93)
    r = {'new_segments': [s], 'new_vias': [v]}
    add_route_to_pcb_data(pcb, r)
    n_s, n_v = len(pcb.segments), len(pcb.vias)
    add_route_to_pcb_data(pcb, r)          # the restore/retry re-commit
    check("segment count unchanged", len(pcb.segments) == n_s,
          f"{n_s} -> {len(pcb.segments)}")
    check("via count unchanged", len(pcb.vias) == n_v,
          f"{n_v} -> {len(pcb.vias)}")
    check("no duplicate segment identities", dup_ids(pcb.segments) == 0)
    check("no duplicate via identities", dup_ids(pcb.vias) == 0)

    print("2. a third commit is still a no-op (ulx3s tripled its entries)")
    add_route_to_pcb_data(pcb, r)
    check("still no duplicates", dup_ids(pcb.segments) == 0
          and dup_ids(pcb.vias) == 0)

    print("3. a GENUINE restore after a rip still lands")
    # Fresh board + fresh result: reusing the result above would prove
    # nothing, because add_route_to_pcb_data MUTATES it
    # (result['new_segments'] = cleaned_segments), so a re-committed result
    # can legitimately be empty by then. This is the real rip/restore shape:
    # the copper is off the board, the saved result still holds the objects.
    pcb3 = board()
    s3 = seg(10.0, 10.0, 11.0, 10.0)
    v3 = Via(x=10.0, y=10.0, size=0.45, drill=0.2,
             layers=['F.Cu', 'B.Cu'], net_id=38)
    saved = {'new_segments': [s3], 'new_vias': [v3]}
    add_route_to_pcb_data(pcb3, saved)
    check("committed once", any(x is s3 for x in pcb3.segments))
    # the rip
    pcb3.segments[:] = [x for x in pcb3.segments if x is not s3]
    pcb3.vias[:] = [x for x in pcb3.vias if x is not v3]
    check("the rip removed it", not any(x is s3 for x in pcb3.segments))
    # the restore re-commits the SAME objects
    add_route_to_pcb_data(pcb3, {'new_segments': [s3], 'new_vias': [v3]})
    check("the ripped segment came back", any(x is s3 for x in pcb3.segments))
    check("the ripped via came back", any(x is v3 for x in pcb3.vias))
    check("and only once", dup_ids(pcb3.segments) == 0
          and dup_ids(pcb3.vias) == 0)

    print("4. distinct objects of EQUAL geometry are both kept")
    # Identity, not value: two separate segments at the same place are two
    # real pieces of copper (the writer's own dedup is a separate concern).
    pcb2 = board()
    a = seg(1.0, 1.0, 2.0, 1.0)
    b = seg(1.0, 1.0, 2.0, 1.0)
    add_route_to_pcb_data(pcb2, {'new_segments': [a, b], 'new_vias': []})
    kept = [x for x in pcb2.segments if x is a or x is b]
    check("both distinct twins are on the board", len(kept) == 2,
          f"kept={len(kept)}")

    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): " + "; ".join(_fail))
        sys.exit(1)
    print("All checks passed")


if __name__ == '__main__':
    main()
