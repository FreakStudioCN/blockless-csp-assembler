#!/usr/bin/env python3
"""try_terminal_restore must never insert copper that is already on the board.

The 'stub' path inserts the escape stub itself and returns; unlike
'full'/'full_open' it does NOT go through restore_net, so the caller leaves
the _rip_saved registry entry in place. A net that terminally fails again is
then re-offered the SAME saved payload -- measured on ulx3s as four stub
restores of one net, which put the same Segment and Via objects into
pcb_data twice.

A duplicate object entry is never harmless: the write model holds the object
once (so the board ledger reports phantom board-only copper), obstacles get
double-stamped (#208/#309), and gates treating the list as a node set are
defeated (#195). The same-net exemption in _copper_conflicts means the
conflict check cannot catch this -- object identity is what matters.

    python3 tests/test_terminal_restore_idempotent.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kicad_parser import PCBData, BoardInfo, Net, Pad, Segment, Via  # noqa: E402
from routing_config import GridRouteConfig                           # noqa: E402
from rip_restore import try_terminal_restore                         # noqa: E402


def _board():
    """Two pads on one net whose saved route is BLOCKED, so only the escape
    stub can come back -- the 'stub' verdict is the path under test.

    The blocking matters: a conflict-free payload returns 'full'/'full_open'
    and the caller (not this function) reinserts it via restore_net, which
    pops the registry -- that path cannot double-insert and would make this
    test pass vacuously. Foreign copper is therefore parked across the LONG
    saved segment but clear of the stub.
    """
    pcb = PCBData(
        board_info=BoardInfo(layers={0: 'F.Cu', 31: 'B.Cu'},
                             copper_layers=['F.Cu', 'B.Cu'],
                             board_bounds=(0.0, 0.0, 50.0, 50.0)),
        nets={7: Net(net_id=7, name='NET7'), 8: Net(net_id=8, name='NET8')},
        footprints={}, vias=[], segments=[], pads_by_net={}, zones=[])
    pads = []
    for i, (x, y) in enumerate(((10.0, 10.0), (20.0, 10.0))):
        pads.append(Pad(pad_number=str(i + 1), net_id=7, net_name='NET7',
                        global_x=x, global_y=y, local_x=0.0, local_y=0.0,
                        size_x=0.5, size_y=0.5, shape='circle',
                        layers=['F.Cu'], drill=0.0, pad_type='smd',
                        component_ref='U1'))
    pcb.pads_by_net = {7: pads}
    # foreign copper routed since the rip, straight across the long segment
    pcb.segments.append(Segment(start_x=15.0, start_y=9.0, end_x=15.0,
                                end_y=11.0, width=0.2, layer='F.Cu',
                                net_id=8))
    # saved payload: a 0.4mm stub off pad 1 (+ via), then the long run that
    # the foreign copper now blocks
    seg = Segment(start_x=10.0, start_y=10.0, end_x=10.4, end_y=10.0,
                  width=0.2, layer='F.Cu', net_id=7)
    via = Via(x=10.4, y=10.0, size=0.6, drill=0.3,
              layers=['F.Cu', 'B.Cu'], net_id=7)
    long_seg = Segment(start_x=10.4, start_y=10.0, end_x=20.0, end_y=10.0,
                       width=0.2, layer='F.Cu', net_id=7)
    pcb._rip_saved = {7: ({'new_segments': [seg, long_seg],
                           'new_vias': [via]}, set(), True)}
    return pcb, seg, via


def main():
    cfg = GridRouteConfig(clearance=0.2, track_width=0.2, via_size=0.6,
                          via_drill=0.3, grid_step=0.1,
                          layers=['F.Cu', 'B.Cu'])
    pcb, seg, via = _board()

    first = try_terminal_restore(pcb, cfg, 7)
    n_s1, n_v1 = len(pcb.segments), len(pcb.vias)
    # Registry deliberately left in place, exactly as the caller leaves it.
    second = try_terminal_restore(pcb, cfg, 7)
    n_s2, n_v2 = len(pcb.segments), len(pcb.vias)

    failures = []
    # Guard against a VACUOUS pass: only the 'stub' verdict inserts copper
    # inside try_terminal_restore, so any other verdict means the fixture
    # stopped exercising the double-insert path (the first version of this
    # test drifted to 'full_open' and "passed" having added nothing).
    if first != 'stub':
        print(f"FAIL: fixture no longer reaches the 'stub' path "
              f"(verdict={first!r}); it cannot detect the regression")
        return 1
    if n_s2 != n_s1 or n_v2 != n_v1:
        failures.append(
            f"second try_terminal_restore ADDED copper again: segments "
            f"{n_s1}->{n_s2}, vias {n_v1}->{n_v2} (verdicts {first!r} then "
            f"{second!r})")
    if sum(1 for s in pcb.segments if s is seg) > 1:
        failures.append("the SAME Segment object is in pcb_data.segments twice")
    if sum(1 for v in pcb.vias if v is via) > 1:
        failures.append("the SAME Via object is in pcb_data.vias twice")

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"terminal restore is idempotent: verdicts {first!r} then "
          f"{second!r}, copper unchanged ({n_s1} seg / {n_v1} via). OK")
    return 0


if __name__ == '__main__':
    sys.exit(main())
