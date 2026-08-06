"""#463 spartan6_6layer: a partial restore must not ship copper the reconnect withdrew.

route_disconnected_planes emits a partial restore's kept-set into the write
list BEFORE the ripped-net reconnect runs, and unconditionally. The same net is
also queued as a reconnect casualty, so the reconnect can RE-ROUTE it and
delete that copper from pcb_data -- while the write list still carries it. The
output then ships copper that exists in no in-memory state.

On spartan6_6layer /RAM/DDR-LDM was partially restored on In3.Cu, the reconnect
moved it to In1.Cu and handed the corridor to /RAM/DDR-D11, and the stale
In3.Cu run was written anyway: a 0.220mm COLLINEAR overlap between two
different DDR nets -- a hard short both DRC engines flag.

Two guards already existed (a net re-ripped to a FULL rip must not emit its
earlier kept-set; a superseded kept-set must not be emitted). Neither sees a
reconnect reroute. This pins the third rule: emit a kept piece only while it is
still live in pcb_data.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from kicad_parser import Segment, Via  # noqa: E402
# Drive the REAL function, not a copy of its logic: a hand-mirrored gate
# silently drifts from the code it is meant to pin (the GUI-parity lesson).
from route_disconnected_planes import (  # noqa: E402
    drop_withdrawn_partial_restores)


class _Pcb:
    """Minimal stand-in: the reconciliation reads only these three fields."""

    def __init__(self, segments, vias, nets=None):
        self.segments = segments
        self.vias = vias
        self.nets = nets or {}


def _reconcile(partial_segs, partial_vias, all_new_segments, all_new_vias,
               pcb_segments, pcb_vias):
    n_s, n_v, _names = drop_withdrawn_partial_restores(
        partial_segs, partial_vias, all_new_segments, all_new_vias,
        _Pcb(pcb_segments, pcb_vias))
    return n_s, n_v


def main():
    failures = []

    LDM, D11 = 160, 145
    # LDM's kept In3.Cu run, emitted by the partial restore...
    ldm_stale = {'start': (171.1, 84.1), 'end': (171.1, 81.0),
                 'width': 0.13, 'layer': 'In3.Cu', 'net_id': LDM}
    # ...and a kept piece the reconnect did NOT touch, which must survive.
    ldm_kept = {'start': (172.7, 86.9), 'end': (171.8, 86.0),
                'width': 0.13, 'layer': 'In3.Cu', 'net_id': LDM}
    ldm_via = {'x': 173.0, 'y': 75.5, 'size': 0.25, 'drill': 0.15,
               'layers': ['F.Cu', 'B.Cu'], 'net_id': LDM}
    # Copper from another pass that happens to look like the stale run but is
    # a different dict: identity filtering must not remove it.
    lookalike = dict(ldm_stale)

    all_segs = [ldm_stale, ldm_kept, lookalike]
    all_vias = [ldm_via]

    # pcb_data AFTER the reconnect: LDM moved off In3.Cu, keeping only the far
    # piece; D11 now owns the corridor. The via survived.
    pcb_segs = [
        Segment(start_x=172.7, start_y=86.9, end_x=171.8, end_y=86.0,
                width=0.13, layer='In3.Cu', net_id=LDM),
        Segment(start_x=171.1, start_y=82.5, end_x=171.1, end_y=81.0,
                width=0.13, layer='In3.Cu', net_id=D11),
    ]
    pcb_vias = [Via(x=173.0, y=75.5, size=0.25, drill=0.15,
                    layers=['F.Cu', 'B.Cu'], net_id=LDM)]

    n_s, n_v = _reconcile([ldm_stale, ldm_kept], [ldm_via],
                          all_segs, all_vias, pcb_segs, pcb_vias)

    if any(d is ldm_stale for d in all_segs):
        failures.append(
            "the withdrawn In3.Cu run (171.1,84.1)-(171.1,81.0) was still "
            "written -- it collides collinearly with DDR-D11, a hard short")
    else:
        print("PASS: copper the reconnect withdrew is not written")

    if not any(d is ldm_kept for d in all_segs):
        failures.append("a kept piece still live in pcb_data was dropped -- "
                        "the filter is over-eager and would strand the net")
    else:
        print("PASS: still-live kept copper survives")

    if not any(d is ldm_via for d in all_vias):
        failures.append("a still-live kept VIA was dropped")
    else:
        print("PASS: still-live kept via survives")

    if not any(d is lookalike for d in all_segs):
        failures.append("an equal-VALUED dict from another pass was removed -- "
                        "the filter must key on identity, not value")
    else:
        print("PASS: value-equal copper from another pass is untouched")

    if (n_s, n_v) != (1, 0):
        failures.append(f"expected exactly 1 stale segment / 0 vias, got {n_s}/{n_v}")

    if failures:
        print("\nFAIL:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == '__main__':
    sys.exit(main())
