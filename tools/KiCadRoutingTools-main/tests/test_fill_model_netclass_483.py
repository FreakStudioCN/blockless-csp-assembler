"""ZoneFillModel pairwise-netclass stamping (#483 item 5).

Item 5: KiCad refills a zone at max(zone clearance, pairwise netclass) from
every foreign object, so on an honor-classes chain (no --clearance ceiling) a
foreign net with a LOOSER class carves more copper than the zone clearance
alone -- a model stamping flat zc predicts fill the refill does not produce.
Mirrors _neck_plane_segments' _pair_clearance: both sides of the pair count.

Item 6 (neighbour-zone pullback) is NOT modelled on main: it was measured at
scale on a deterministic grader (A/A gate flat first) and REJECTED -- a full
trigger-set wave improved 3 boards, regressed 3, left 2 neutral, aggregate
incomplete +2 / DRC +5 / connection-width -1, perturbing -91 to +366 segments
per board with the sign varying board to board. Code preserved on branches
plane-483-fill-fidelity and item6-wave. One assertion below pins the current
"does not carve" contract so landing item 6 must flip it deliberately.
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from plane_fill_model import (  # noqa: E402
    ZoneFillModel, set_board_net_clearances, get_zone_model, _NC_ATTR)

ZONE_POLY = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]


def make_pcb(zones=(), pad_net=2, seg_net=None, via_net=None):
    """Board with one foreign 1x1mm pad at (5,5); optional foreign seg/via."""
    pad = SimpleNamespace(net_id=pad_net, global_x=5.0, global_y=5.0,
                          size_x=1.0, size_y=1.0, shape='rect',
                          rect_rotation=0.0, layers=['F.Cu'], drill=0,
                          pad_type='smd', local_clearance=0.0)
    segs = []
    if seg_net is not None:
        segs.append(SimpleNamespace(net_id=seg_net, layer='F.Cu', width=0.2,
                                    start_x=2.0, start_y=8.0,
                                    end_x=8.0, end_y=8.0))
    vias = []
    if via_net is not None:
        vias.append(SimpleNamespace(net_id=via_net, x=2.0, y=2.0,
                                    size=0.6, drill=0.3))
    return SimpleNamespace(
        vias=vias, segments=segs, pads_by_net={pad_net: [pad]},
        zones=list(zones), board_info=SimpleNamespace(keepouts=[]))


def make_zone(net_id=1, priority=0, polygon=None, clearance=0.2):
    return SimpleNamespace(net_id=net_id, layer='F.Cu', clearance=clearance,
                           min_thickness=0.2, priority=priority,
                           polygon=list(polygon or ZONE_POLY))


def main():
    # ---- item 5: foreign side of the pair ----------------------------------
    # Probe 0.5mm from the 1x1 pad's edge (pad edge x=5.5, probe x=6.0).
    # Flat zc=0.2 -> fill exists.
    m = ZoneFillModel(make_pcb(), make_zone())
    assert m.ok and m.query_component(6.0, 5.0), \
        "baseline: fill missing outside the flat zone-clearance guard"

    # Foreign pad's net carries a 0.8mm class -> KiCad carves to 0.8.
    m = ZoneFillModel(make_pcb(), make_zone(), net_clearances={2: 0.8})
    assert m.ok
    assert not m.query_component(6.0, 5.0), \
        "foreign netclass not honored: fill credited inside the class ring"
    assert m.query_component(7.0, 5.0), \
        "over-carved: no fill beyond the foreign class ring"

    # ---- item 5: the ZONE's OWN class counts too ---------------------------
    # A looser class on the PLANE net widens every stamp, even when the
    # foreign net is Default (absent from the map).
    m = ZoneFillModel(make_pcb(), make_zone(), net_clearances={1: 0.8})
    assert m.ok and not m.query_component(6.0, 5.0), \
        "zone's own netclass not honored (only the foreign side was applied)"

    # Absent nets are inert -- an all-Default board must be bit-identical to
    # the flat-zc baseline, or every clamped chain shifts.
    m = ZoneFillModel(make_pcb(), make_zone(), net_clearances={})
    assert m.ok and m.query_component(6.0, 5.0), \
        "empty class map is not inert"

    # ---- item 5: segments and vias, not just pads --------------------------
    # Segment at y=8 (half-width 0.1); probe 0.35mm off its edge.
    pcb = make_pcb(seg_net=3)
    m = ZoneFillModel(pcb, make_zone())
    assert m.ok and m.query_component(5.0, 8.45), \
        "baseline: fill missing beside a foreign segment"
    m = ZoneFillModel(pcb, make_zone(), net_clearances={3: 0.8})
    assert m.ok and not m.query_component(5.0, 8.45), \
        "foreign netclass not applied to segments"

    # Via at (2,2) r=0.3; probe 0.35mm off the barrel.
    pcb = make_pcb(via_net=4)
    m = ZoneFillModel(pcb, make_zone())
    assert m.ok and m.query_component(2.65, 2.0), \
        "baseline: fill missing beside a foreign via"
    m = ZoneFillModel(pcb, make_zone(), net_clearances={4: 0.8})
    assert m.ok and not m.query_component(2.65, 2.0), \
        "foreign netclass not applied to vias"

    # A sibling pour on the same layer must NOT carve this fill: neighbour-zone
    # pullback (#483 item 6) is deliberately not modelled on main. It is real
    # in KiCad, but it measured mixed on a deterministic grader (duet2_3dp
    # improved, scalenode_cm4 regressed) and is held on branch
    # plane-483-fill-fidelity. This asserts the CURRENT contract, so that
    # landing item 6 has to come here and flip it consciously.
    nb = make_zone(net_id=9, polygon=[(10.0, 0.0), (20.0, 0.0),
                                      (20.0, 10.0), (10.0, 10.0)])
    ours = make_zone()
    m = ZoneFillModel(make_pcb(zones=[ours, nb]), ours)
    assert m.ok and m.query_component(9.9, 5.0), \
        "neighbour-zone pullback is not modelled on main (see #483 item 6)"

    # ---- board-level publication + cache coherence -------------------------
    ours = make_zone()
    pcb = make_pcb()
    m1 = get_zone_model(pcb, ours)
    assert m1 is not None and m1.query_component(6.0, 5.0), \
        "baseline model via get_zone_model"
    set_board_net_clearances(pcb, {2: 0.8})
    assert getattr(pcb, _NC_ATTR) == {2: 0.8}
    m2 = get_zone_model(pcb, ours)
    assert m2 is not None and m2 is not m1, \
        "publishing a new class map did not invalidate the model cache"
    assert not m2.query_component(6.0, 5.0), \
        "board-level class map not picked up by the rebuilt model"

    # Republishing the same map is a no-op (callers may call unconditionally).
    m3 = get_zone_model(pcb, ours)
    set_board_net_clearances(pcb, {2: 0.8})
    assert get_zone_model(pcb, ours) is m3, \
        "republishing an identical map needlessly dropped the cache"

    # Zero/falsey entries are dropped, so a map of Default-class nets stays
    # inert rather than thrashing the cache.
    set_board_net_clearances(pcb, {2: 0.8, 7: 0.0})
    assert getattr(pcb, _NC_ATTR) == {2: 0.8}

    print("PASS: fill model honors pairwise netclasses (#483 item 5)")


if __name__ == '__main__':
    main()
