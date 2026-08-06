#!/usr/bin/env python3
"""#545 burndown: terminal-selection paths must not drop same-net anchors.

Unit coverage for the cheaply-testable findings; F1/F2/F9 (Phase-3 island
machinery) and F10/F11 are exercised by the routing test suites and the
set1 replay waves.

  F3  _EndpointStub carries the real net id (all-layer reach on stub-on-via)
  F4  multipoint groups are represented by their VIA (all-layer) when one exists
  F5  diff-pair Case 1 falls back to segment endpoints when a group has no tip
  F7  Case 3 offers a via-in-pad as an all-layer terminal
  F8  Case 2's pad-connected test is layer-gated (no cross-layer phantom)

    python3 tests/test_terminal_anchor_fixes.py
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rust_router"))

from connectivity import get_net_endpoints, get_multipoint_net_pads


def _seg(x1, y1, x2, y2, layer="F.Cu", net=5, width=0.2):
    return SimpleNamespace(start_x=x1, start_y=y1, end_x=x2, end_y=y2,
                           width=width, layer=layer, net_id=net)


def _via(x, y, net=5):
    return SimpleNamespace(x=x, y=y, size=0.5, drill=0.3, net_id=net,
                           layers=["F.Cu", "B.Cu"])


def _pad(x, y, net=5, ref="J1", layers=("F.Cu",), drill=0.0):
    return SimpleNamespace(global_x=x, global_y=y, net_id=net,
                           pad_type="smd" if not drill else "thru_hole",
                           size_x=0.6, size_y=0.6, drill=drill,
                           layers=list(layers), component_ref=ref,
                           pad_number="1", shape="rect", rect_rotation=0.0,
                           roundrect_rratio=0.0, polygons=None)


def _pcb(pads, segs, vias, net=5):
    n = SimpleNamespace(name="N", net_id=net, pads=pads)
    return SimpleNamespace(nets={net: n}, pads_by_net={net: pads},
                           segments=segs, vias=vias, zones=[], footprints={},
                           board_outlines=[], board_info=None)


def _cfg(layers=("F.Cu", "B.Cu")):
    return SimpleNamespace(layers=list(layers), grid_step=0.05)


def test_f3_stub_net_id():
    from connectivity import _make_endpoint_stub
    from single_ended_routing import _pad_all_layer_reach
    stub = _make_endpoint_stub(12.0, 10.0, "F.Cu", net_id=5)
    assert stub.net_id == 5
    pcb = _pcb([], [], [_via(12.0, 10.0)])
    assert _pad_all_layer_reach(pcb, stub), \
        "stub sitting on a same-net via must reach all layers (F3)"
    legacy = _make_endpoint_stub(12.0, 10.0, "F.Cu")
    assert not _pad_all_layer_reach(pcb, legacy), \
        "net-less stub must stay conservative"
    print("  F3: endpoint stub carries net id; stub-on-via reaches all layers")


def test_f4_group_via_representative():
    # 3 groups (multipoint): two pad-stub islands ending in vias + a bare pad.
    pads = [_pad(10, 10, ref="U1"), _pad(40, 40, ref="U2"),
            _pad(25, 25, ref="R9")]
    segs = [_seg(10, 10, 12, 10), _seg(40, 40, 38, 40)]
    vias = [_via(12, 10), _via(38, 40)]
    pcb = _pcb(pads, segs, vias)
    info = get_multipoint_net_pads(pcb, 5, _cfg())
    assert info is not None and len(info) >= 3
    xy = {(round(e[3], 2), round(e[4], 2)) for e in info}
    assert (12.0, 10.0) in xy and (38.0, 40.0) in xy, \
        f"groups must be represented by their vias, got {xy} (F4)"
    # The via-backed representatives carry the net id (F3), so the phase
    # routers grade them all-layer-reachable.
    for e in info:
        if (round(e[3], 2), round(e[4], 2)) in ((12.0, 10.0), (38.0, 40.0)):
            assert getattr(e[5], 'net_id', 0) == 5
    print("  F4: multipoint groups represented by their via, net id attached")


def test_f5_diffpair_case1_fallback():
    # Two groups; group A's both tips sit on pads (no free end), group B has
    # a free tip. Pre-fix: sources side empty -> "Cannot determine endpoints".
    pads = [_pad(10, 10, ref="U1"), _pad(14, 10, ref="C1"),
            _pad(40, 40, ref="U2")]
    segs = [_seg(10, 10, 14, 10),          # A: pad-to-pad, no free tip
            _seg(40, 40, 38, 40)]          # B: stub with a free tip
    pcb = _pcb(pads, segs, [])
    sources, targets, err = get_net_endpoints(pcb, 5, _cfg(),
                                              use_stub_free_ends=True)
    assert err is None, f"diff-pair Case 1 must not fail: {err} (F5)"
    assert sources and targets
    print("  F5: tipless diff-pair group falls back to segment endpoints")


def test_f7_case3_via_terminal():
    # No segments; U1's escape is a via-in-pad. Its terminals must include
    # the via on BOTH layers, not just the pad's own F.Cu.
    pads = [_pad(10, 10, ref="U1"), _pad(40, 40, ref="U2")]
    vias = [_via(10, 10)]
    pcb = _pcb(pads, [], vias)
    sources, targets, err = get_net_endpoints(pcb, 5, _cfg())
    assert err is None
    src_layers = {s[2] for s in sources}
    assert src_layers == {0, 1}, \
        f"via-in-pad must be an all-layer terminal, got layers {src_layers} (F7)"
    print("  F7: Case 3 offers the via-in-pad on every routing layer")


def test_f8_layer_gated_connected_test():
    # One group on In1.Cu (mapped); a B.Cu pad sits 0.03mm from its endpoint
    # with NO via. Pre-fix: graded connected (any-layer 0.05 box), net fell
    # through to Case 4 "already routed". Post-fix: the pad is unconnected
    # and Case 2 routes it.
    pads = [_pad(10, 10, ref="U1", layers=("In1.Cu",)),
            _pad(20.03, 10, ref="U2", layers=("B.Cu",))]
    segs = [_seg(10, 10, 20, 10, layer="In1.Cu")]
    pcb = _pcb(pads, segs, [])
    cfg = _cfg(layers=("In1.Cu", "B.Cu"))
    sources, targets, err = get_net_endpoints(pcb, 5, cfg)
    assert err is None, f"cross-layer graze must not grade connected: {err} (F8)"
    assert sources and targets
    tgt_xy = {(round(t[3], 2), round(t[4], 2)) for t in targets}
    assert (20.03, 10.0) in tgt_xy, \
        f"the B.Cu pad must be the route target, got {tgt_xy}"
    print("  F8: cross-layer endpoint graze no longer counts as connected")


def main():
    test_f3_stub_net_id()
    test_f4_group_via_representative()
    test_f5_diffpair_case1_fallback()
    test_f7_case3_via_terminal()
    test_f8_layer_gated_connected_test()
    print("OK: #545 terminal-anchor fixes")


if __name__ == "__main__":
    main()
