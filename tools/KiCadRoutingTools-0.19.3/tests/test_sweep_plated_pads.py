#!/usr/bin/env python3
"""Issue #494: the fill-aware re-check sweep must not skip plated pads.

The sweep ("pad(s) reported tapped but still floating -- forcing a via")
iterates res['disconnected_pads'] and then skipped plated-through pads on
the premise "plated barrels are already plane-tied by the fill". Every pad
reaching that guard has JUST been reported still floating by the fill-aware
check, so the premise is false for exactly the population the pass looks at.
Split from #492, where the same contradiction was measured in the gate tap.

Two independent blocks, as in #492 -- fixing either alone leaves the pass
inert on plated pads:
  1. the `pad_is_plated_through` skip;
  2. the launch-layer resolution, which required a CONCRETE copper layer, so
     a '*.Cu' through-hole pad resolved to None and was skipped anyway.

Third, custody. The pre-#494 rule vetoed on ANY remaining entry for the pad,
which throws away a genuine repair on a plated pad: check_net_connectivity
can list such a pad once per copper layer, so one joined on B.Cu stays listed
on F.Cu. Keying on (pad, repaired layer) instead is WORSE -- it passes
vacuously whenever the pad's entry sits on a different layer than the tap
launched from (measured: nrfmicro U1.24 kept a via that changed nothing).
Custody is therefore PROGRESS: keep only if the pad's entry count strictly
drops. The old rule stays reachable behind KICAD_NO_SWEEP_PLATED=1 so the
A/B differs by exactly one variable.

NPTH holes stay skipped -- no copper to tap at all (#328).

    python3 tests/test_sweep_plated_pads.py
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from route_disconnected_planes import (plane_tap_launch_layers,  # noqa: E402
                                       pad_floating_entries,
                                       pad_repair_made_progress,
                                       pad_repair_rejected)

LAYERS = ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu']
ZONES = ['In2.Cu']


def _pad(layers, drill=0.0, pad_type='smd'):
    return SimpleNamespace(layers=list(layers), drill=drill,
                           pad_type=pad_type)


def _placed(x, y, ref, layers, drill=0.0, pad_type='smd'):
    return SimpleNamespace(global_x=x, global_y=y, component_ref=ref,
                           layers=list(layers), drill=drill,
                           pad_type=pad_type)


def check(label, got, want):
    if got != want:
        print(f"FAIL: {label}: got {got}, want {want}")
        return 1
    print(f"  ok: {label} -> {got}")
    return 0


def main():
    fails = 0

    # --- launch layers ---------------------------------------------------
    fails += check("smd launches from its own layer",
                   plane_tap_launch_layers(_pad(['F.Cu', 'F.Mask']),
                                           ZONES, LAYERS),
                   ['F.Cu'])

    # THE #494 CASE: a plated barrel. Previously skipped twice over -- by the
    # plated guard, and by '*.Cu' failing the concrete-layer filter.
    fails += check("plated '*.Cu' prefers the net's zone layer",
                   plane_tap_launch_layers(
                       _pad(['*.Cu', '*.Mask'], drill=0.8,
                            pad_type='thru_hole'), ZONES, LAYERS),
                   ['In2.Cu'])

    # Multi-layer pour: all of them, in routing-layer order, so repeated
    # runs try the same layer first (determinism).
    fails += check("plated, multi-layer pour, routing order",
                   plane_tap_launch_layers(
                       _pad(['*.Cu'], drill=0.8, pad_type='thru_hole'),
                       ['B.Cu', 'F.Cu'], LAYERS),
                   ['F.Cu', 'B.Cu'])

    # Zone on no routing layer: still a real barrel, so offer every layer
    # rather than giving up.
    fails += check("plated, zone off routing layers -> all",
                   plane_tap_launch_layers(
                       _pad(['*.Cu'], drill=0.8, pad_type='thru_hole'),
                       ['Inner9.Cu'], LAYERS),
                   LAYERS)

    # NPTH has NO copper (#328) even though its layer list names '*.Cu'.
    fails += check("npth stays skipped",
                   plane_tap_launch_layers(
                       _pad(['*.Cu', '*.Mask'], drill=3.2,
                            pad_type='np_thru_hole'), ZONES, LAYERS),
                   [])

    fails += check("smd on a non-routing layer contributes nothing",
                   plane_tap_launch_layers(_pad(['In7.Cu']), ZONES, LAYERS),
                   [])

    # --- custody: PROGRESS, not absence ---------------------------------
    thru = _placed(131.11, 125.75, 'J4', ['*.Cu', '*.Mask'], drill=0.8,
                   pad_type='thru_hole')
    smd = _placed(10.0, 20.0, 'C6', ['F.Cu'])

    # entry counting is per pad, across layers, and does not confuse
    # a same-position pad of another component
    fails += check("plated pad listed on two layers counts 2",
                   pad_floating_entries(
                       [(131.11, 125.75, 'F.Cu', 'J4'),
                        (131.11, 125.75, 'B.Cu', 'J4'),
                        (10.0, 20.0, 'F.Cu', 'C6')], thru),
                   2)
    fails += check("a different component is not counted",
                   pad_floating_entries(
                       [(10.0, 20.0, 'F.Cu', 'C7')], smd), 0)

    # A plated pad genuinely joined on one layer: one of its two entries
    # clears. That is progress -- keep the copper. (The old pad-keyed rule
    # threw this away; nhyodyne_6809 J4, #492.)
    fails += check("plated: one of two entries cleared -> keep",
                   pad_repair_made_progress(2, 1, thru), True)

    # THE nrfmicro U1.24 FALSE ACCEPT: the pad's only entry is on F.Cu and
    # the sweep repaired from B.Cu. A (pad, repaired-layer) key passes
    # vacuously; counting entries does not, because nothing changed.
    fails += check("verdict unchanged -> undo (no vacuous pass)",
                   pad_repair_made_progress(1, 1, thru), False)

    fails += check("smd: entry cleared -> keep",
                   pad_repair_made_progress(1, 0, smd), True)
    fails += check("smd: still listed -> undo",
                   pad_repair_made_progress(1, 1, smd), False)

    # A repair that makes things WORSE is never kept.
    fails += check("regression -> undo",
                   pad_repair_made_progress(1, 2, thru), False)

    # --- legacy dispatch, so KICAD_NO_SWEEP_PLATED=1 == main ------------
    # The pre-#494 rule: ANY remaining entry for the pad vetoes. Keeping it
    # behind the switch is what makes the A/B differ by one variable, and
    # what lets the A/A check prove the refactor moved nothing else.
    two = [(131.11, 125.75, 'F.Cu', 'J4'), (131.11, 125.75, 'B.Cu', 'J4')]
    one = [(131.11, 125.75, 'F.Cu', 'J4')]
    fails += check("legacy: one entry left still vetoes",
                   pad_repair_rejected(two, one, thru, legacy=True), True)
    fails += check("legacy: fully cleared -> keep",
                   pad_repair_rejected(two, [], thru, legacy=True), False)
    fails += check("new: same case is progress -> keep",
                   pad_repair_rejected(two, one, thru, legacy=False), False)
    fails += check("new: unchanged verdict -> undo",
                   pad_repair_rejected(one, one, thru, legacy=False), True)

    if fails:
        print(f"\n{fails} FAILURE(S)")
        return 1
    print("\nPASS: sweep plated pads (launch layers + custody)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
