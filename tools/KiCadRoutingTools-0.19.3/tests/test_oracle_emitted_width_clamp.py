#!/usr/bin/env python3
"""The oracle reconnect must not ship copper WIDER than it validated (#495/#496).

kicad_oracle's plane-link width upgrade runs a ladder (0.2/0.4/0.8) and gates
each rung on wide_route_clear. Two holes let an unvalidated width reach the
board:

  1. A DEGENERATE wide route (fewer than 2 points) has no same-layer legs, and
     wide_route_clear returns True vacuously for a leg-less route -- so the
     upgrade is accepted on an EMPTY check. The caller then re-routes with
     track_margin=0 (geometry sized for the nominal width) and that real path
     inherits the upgraded width.
  2. The same-net weld connectors (_extra_conn) are synthesized after the
     ladder and were never clearance-checked at all.

On rp2350_fpga_eensy this shipped 8 GND repair straps 0.026-0.361 mm inside
J1's 0.65 mm USB-C mounting hole, emitted at 0.8 mm against an obstacle map
stamped for the 0.0762 mm nominal width -- on a board the signal router had
left DRC-clean.

clamp_emitted_width re-checks the exact copper about to be written and steps
the width back down. It floors at the nominal width, so it can only decline an
UPGRADE -- never lose a link that would otherwise have shipped.

Run:  python3 tests/test_oracle_emitted_width_clamp.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from kicad_parser import Pad, PCBData, BoardInfo
from routing_config import GridRouteConfig
from kicad_oracle import clamp_emitted_width, emitted_copper_clear

NOMINAL = 0.0762
FAILURES = []


def _check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got}, want {want}")
    if not ok:
        FAILURES.append(name)


def _npth_pad(x, y, drill=0.65):
    """A no-copper (NPTH) mounting hole, like J1's USB-C shell hole."""
    return Pad(pad_number="", net_id=0, net_name="", global_x=x, global_y=y,
               local_x=0.0, local_y=0.0, size_x=drill, size_y=drill,
               shape="circle", layers=["*.Cu"], drill=drill,
               pad_type="np_thru_hole", component_ref="J1")


def _pcb(pads_by_net):
    bi = BoardInfo(layers={0: "F.Cu", 31: "B.Cu"},
                   copper_layers=["F.Cu", "In1.Cu", "B.Cu"])
    bi.board_bounds = (140.0, 80.0, 165.0, 110.0)
    return PCBData(board_info=bi, nets={}, footprints={}, vias=[], segments=[],
                   pads_by_net=pads_by_net)


def _cfg():
    return GridRouteConfig(track_width=NOMINAL, clearance=0.09, via_size=0.25,
                           via_drill=0.15, grid_step=0.05,
                           hole_to_hole_clearance=0.2, board_edge_clearance=0.0)


def main():
    print("oracle emitted-width clamp (#495/#496):")
    cfg = _cfg()
    # J1's hole and the real rp2350 strap geometry that shipped through it.
    # Closest approach of this path to the hole centre is 0.5636mm. The
    # NPTH-to-track floor is drill/2 + max(clearance, 0.20) + width/2, i.e.
    #   0.8mm    -> 0.325 + 0.20 + 0.400 = 0.925  (violates by 0.361)
    #   0.0762mm -> 0.325 + 0.20 + 0.038 = 0.563  (clears, just)
    pcb = _pcb({0: [_npth_pad(151.39, 91.79)]})
    strap = [(152.05, 92.65, 'In1.Cu'), (152.05, 92.40, 'In1.Cu'),
             (152.00, 92.35, 'In1.Cu'), (152.00, 91.75, 'In1.Cu'),
             (151.95, 91.70, 'In1.Cu'), (151.95, 91.60, 'In1.Cu'),
             (151.90, 91.55, 'In1.Cu'), (151.90, 91.50, 'In1.Cu'),
             (148.40, 88.00, 'In1.Cu')]

    # The #495 case: an 0.8mm upgrade that was never validated on THIS geometry
    # must be clamped back to the nominal width it actually clears.
    _check("0.8mm strap through an NPTH hole clamps to nominal",
           clamp_emitted_width(strap, [], 0.8, NOMINAL, pcb, 2, cfg), NOMINAL)

    # A route far from the hole keeps its upgrade: the clamp must not be a
    # blanket downgrade of every plane link.
    far = [(145.0, 100.0, 'In1.Cu'), (150.0, 100.0, 'In1.Cu')]
    _check("clear route keeps its 0.8mm upgrade",
           clamp_emitted_width(far, [], 0.8, NOMINAL, pcb, 2, cfg), 0.8)

    # Weld connectors are emitted at the same width but were never gated; a
    # connector through the hole must clamp even when the route itself is clear.
    weld = [((152.00, 92.35), (151.90, 91.50), 'In1.Cu')]
    _check("unvalidated weld connector through the hole clamps",
           clamp_emitted_width(far, weld, 0.8, NOMINAL, pcb, 2, cfg), NOMINAL)

    # Intermediate rung: a geometry that clears 0.2 but not 0.4 keeps 0.2
    # rather than collapsing straight to nominal (0.2 needs 0.625 of clearance,
    # 0.4 needs 0.725; this path sits 0.6718 away).
    mid = [(151.39, 92.4618, 'In1.Cu'), (153.0, 92.4618, 'In1.Cu')]
    _check("steps down to the widest rung that clears",
           clamp_emitted_width(mid, [], 0.8, NOMINAL, pcb, 2, cfg), 0.2)

    # A leg-less (degenerate) route is exactly what fooled the ladder into the
    # upgrade. It emits no copper, so the width is irrelevant -- but the clamp
    # must not crash or invent a width above what was asked for.
    _check("degenerate route is a no-op, not an upgrade",
           clamp_emitted_width([(151.39, 91.79, 'In1.Cu')], [], 0.8, NOMINAL,
                               pcb, 2, cfg), 0.8)

    # Never widens: used_width at or below nominal is returned untouched.
    _check("never widens past what was asked",
           clamp_emitted_width(strap, [], NOMINAL, NOMINAL, pcb, 2, cfg), NOMINAL)

    # The nominal width is the floor, so a graze that survives every rung is
    # still reported as NOT clear -- that is the signal the caller declines the
    # link on, rather than shipping a short (rp2350's 25um +1V1 graze from a
    # force_raw seed, which no width rung and no micro-shift can fix).
    # Sits 0.45 from the hole centre; even at nominal the floor is
    # 0.325 + 0.20 + 0.038 = 0.563, so no width clears it.
    unfixable = [(151.39, 92.24, 'In1.Cu'), (153.0, 92.24, 'In1.Cu')]
    _check("a graze surviving every rung is reported, not silently shipped",
           emitted_copper_clear(unfixable, [], NOMINAL, pcb, 2, cfg), False)
    _check("...and the clamp cannot rescue it either (stays at nominal)",
           clamp_emitted_width(unfixable, [], 0.8, NOMINAL, pcb, 2, cfg), NOMINAL)
    _check("clear copper at nominal reports clear",
           emitted_copper_clear(far, [], NOMINAL, pcb, 2, cfg), True)
    _check("an unvalidated weld alone is enough to report not-clear",
           emitted_copper_clear(far, weld, NOMINAL, pcb, 2, cfg), False)

    if FAILURES:
        print(f"\nFAIL: {len(FAILURES)} case(s): {', '.join(FAILURES)}")
        return 1
    print("\nPASS: emitted width is clamped, and an unfixable graze is reported.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
