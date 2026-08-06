"""Soft reservation of vacated ripped-net corridors in the plane-repair pass
(#517 arm 2, the #343 proposal made deliberate).

When `repair_planes --rip-blocker-nets` rips a signal net to clear
a pad tap, the vacated corridor is claimed first-come-first-served by whatever
routes next (later pads' taps, region-join straps, other nets' reconnects).
The #517 arm-1 attribution measured exactly that: under the default full rip,
refusal corridors were occupied by pad-taps (671 items), reconnects (184) and
region joins (140). This registry keeps a "ghost" of each ripped net's
unrestored copper and steers OTHER claimants away from it:

- soft mode (`KICAD_PLANE_RIP_SOFTBLOCK=1`/`soft`): cost stamps (the existing
  route.py rip-up avoidance mechanism, obstacle_costs.compute_ripped_route_
  costs) merged into every tap-trace and region-join routing map built while
  the ghost is armed, plus a soft via-site preference (find_via_position's
  position_preference hook -- never excludes, only ranks). The via maps stay
  #342-precise.
- hard mode (`KICAD_PLANE_RIP_SOFTBLOCK=hard`): the cheap pre-#342 baseline --
  repair_planes skips SharedViaMaps.note_net_ripped so the ripped
  copper's via-map stamps stay blocked. No routing-map costs (that replica is
  what the 0708 vs 0708a waves accidentally measured at ~+8 nets). Breaks the
  TAP_MAP_VERIFY invariant by design; never run both together.

Lifetime is custody-defined (#517): armed at the rip, shrunk to the DROPPED
pieces when a restore puts copper back (restored copper is its own obstacle),
dropped entirely at reconnect success. The net(s) ripped for the CURRENT tap
transaction are excluded from the merge -- the rip exists so that tap can use
the corridor; the reservation is against everyone else.

Deterministic: nets merge in sorted order; the geometry comes from pcb_data
copper snapshots taken at the rip.
"""
import os
from dataclasses import replace
from typing import Dict, List, Optional, Tuple

import numpy as np

from routing_config import GridCoord, GridRouteConfig


def softblock_mode() -> Optional[str]:
    """'soft', 'hard', 'reconnect', or None (disabled). Read once per call so
    tests can flip the env var.

    #540 item 2: 'reconnect' arms the registry for the RECONNECT batch_route
    pass-through only -- no tap/region cost stamps, no via-site preference
    (both gate on mode == 'soft'). 'soft' now enables the pass-through TOO,
    closing the #517 coverage hole where the biggest corridor thief (the
    reconnect pass, 249 rising claims in the attribution histogram) was the
    one pass the soft arm did not price."""
    v = os.environ.get('KICAD_PLANE_RIP_SOFTBLOCK', '').lower()
    if v in ('1', 'soft'):
        return 'soft'
    if v == 'hard':
        return 'hard'
    if v == 'reconnect':
        return 'reconnect'
    return None


class CorridorGhosts:
    """Registry of ripped-net corridor ghosts for one plane-repair run."""

    def __init__(self, mode: str):
        self.mode = mode
        # net_id -> (segments, vias): the net's copper NOT currently on the
        # board (whole net right after a full rip; only the dropped pieces
        # after a partial/failure-path restore; absent once reconnected).
        self._nets: Dict[int, Tuple[list, list]] = {}
        self.stamped = 0   # instrumentation: maps that received ghost costs
        self.armed_ever = 0

    # ---- custody events -------------------------------------------------
    def set_net(self, net_id: int, segments, vias):
        """Arm/update a net's ghost to exactly the given unrestored pieces."""
        segments = list(segments or [])
        vias = list(vias or [])
        if segments or vias:
            if net_id not in self._nets:
                self.armed_ever += 1
            self._nets[net_id] = (segments, vias)
        else:
            self._nets.pop(net_id, None)

    def drop_net(self, net_id: int):
        self._nets.pop(net_id, None)

    def net_ids(self) -> List[int]:
        return sorted(self._nets)

    @property
    def reconnect_passthrough(self) -> bool:
        """#540 item 2: should reconnect batch_route calls receive ghosts?"""
        return self.mode in ('soft', 'reconnect')

    def external_ghosts(self, exclude_net_ids=()) -> Dict[int, dict]:
        """Armed ghosts as batch_route's `external_ripped_ghosts` payload
        (#540 item 2): {net_id: {'new_segments': [...], 'new_vias': [...]}}.
        exclude_net_ids must hold the nets the batch is about to route --
        their corridors are their own to reclaim."""
        excl = set(exclude_net_ids or ())
        return {nid: {'new_segments': segs, 'new_vias': vias}
                for nid, (segs, vias) in self._nets.items()
                if nid not in excl and (segs or vias)}

    # ---- consumers ------------------------------------------------------
    def merge_into_routing_map(self, obstacles, config: GridRouteConfig,
                               layer_map: Dict[str, int],
                               exclude_net_ids=()) -> int:
        """Stamp soft costs for every armed ghost (minus exclusions) into a
        routing obstacle map. layer_map maps layer name -> map layer index
        (single-layer tap maps pass {route_layer: 0}). Returns cells stamped
        (0 in hard mode: that arm reserves via maps only)."""
        if self.mode != 'soft' or not self._nets:
            return 0
        from obstacle_costs import (compute_ripped_route_costs,
                                    merge_track_proximity_costs)
        eff = config
        if eff.ripped_route_avoidance_cost <= 0 or \
                eff.ripped_route_avoidance_radius <= 0:
            # The plane-repair configs don't surface these knobs; fall back to
            # route.py's defaults rather than silently no-op.
            eff = replace(config,
                          ripped_route_avoidance_cost=0.1,
                          ripped_route_avoidance_radius=1.0)
        excl = set(exclude_net_ids or ())
        per_net: Dict[int, np.ndarray] = {}
        all_via_positions: List[Tuple[int, int]] = []
        n = 0
        for nid in self.net_ids():
            if nid in excl:
                continue
            segs, vias = self._nets[nid]
            lc, vp = compute_ripped_route_costs(
                {'new_segments': segs, 'new_vias': vias}, eff, layer_map)
            if len(lc):
                per_net[nid] = lc
                n += len(lc)
            all_via_positions.extend(vp)
        if per_net:
            merge_track_proximity_costs(obstacles, per_net)
        if all_via_positions:
            coord = GridCoord(eff.grid_step)
            radius_grid = coord.to_grid_dist(eff.ripped_route_avoidance_radius)
            cost_grid = eff.cell_cost(eff.ripped_route_avoidance_cost)
            obstacles.add_stub_proximity_costs_batch(
                all_via_positions, radius_grid, cost_grid, False)
            n += len(all_via_positions)
        if n:
            self.stamped += 1
        return n

    def via_site_clear(self, x: float, y: float, via_size: float,
                       exclude_net_ids=()) -> bool:
        """position_preference predicate: True when (x, y) is NOT inside any
        armed ghost's reserved footprint (any layer -- a through via spans
        them all). Soft by contract: find_via_position only prefers clear
        sites, it never rejects the last resort."""
        if self.mode != 'soft' or not self._nets:
            return True
        excl = set(exclude_net_ids or ())
        r_extra = via_size / 2.0
        for nid, (segs, vias) in self._nets.items():
            if nid in excl:
                continue
            for s in segs:
                thr = r_extra + s.width / 2.0 + 0.2
                if _pt_seg_dist_sq(x, y, s.start_x, s.start_y,
                                   s.end_x, s.end_y) < thr * thr:
                    return False
            for v in vias:
                thr = r_extra + v.size / 2.0 + 0.2
                if (x - v.x) ** 2 + (y - v.y) ** 2 < thr * thr:
                    return False
        return True


def _pt_seg_dist_sq(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        ddx, ddy = px - x1, py - y1
        return ddx * ddx + ddy * ddy
    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx, cy = x1 + t * dx, y1 + t * dy
    ddx, ddy = px - cx, py - cy
    return ddx * ddx + ddy * ddy
