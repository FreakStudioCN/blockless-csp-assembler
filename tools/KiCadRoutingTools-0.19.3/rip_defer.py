#!/usr/bin/env python3
"""Defer a repeatedly-ripped net to a FINAL round instead of churning it.

MEASURED PATHOLOGY (muzy_zynq2, board_step2_route_routetrace.json):

    +3V3 (87 pads)   54 full re-routes, median 168 segments added each
                     47 rips,           median 388 segments removed each
                     route -> next rip gap: MEDIAN 4 EVENTS, minimum 1
                     17115 segments torn = 40% of ALL destroyed copper on the board

Those are not phase-3 taps around a stable trunk -- only 11 of the 54 routes are
tap-sized, so the whole 87-pad tree is rebuilt and destroyed over and over. Each
rebuild lands in a board that is still moving and is torn out a few events later,
so nearly all 54 are WASTED. The churn is spread evenly across all ten deciles of
the run: it is a self-sustaining loop, not a phase artifact.

WHY DEFER RATHER THAN PROTECT. Refusing to rip a hot net (a rip budget), or
skipping rips predicted to fail, both convert churn into UNROUTED NETS -- the
blocked net simply fails instead. Deferring costs nothing in connectivity: the
net is still routed, just later, once its neighbours have stopped moving. The
board already demonstrates the win -- +5V is the one big rail that happened to
route late (#131 of 136) and was ripped 7 times, against 47 for +3V3 at #2.

WHY THE GATE IS NET COST, NOT RIP COUNT. Rip count looked like the obvious
trigger and is nearly useless. Measured hazard on this board -- P(net is ripped
again | already ripped k times):

    k          1     2     3     4     5     6     8    10
    P(again) 0.93  0.93  0.87  0.91  0.96  0.89  0.91  0.87

FLAT at ~0.9 for every k, so "has been ripped k times" carries almost no signal
beyond "was ripped once". There is no knee and therefore no principled N:
deferring at N=3 would defer 86 of 114 nets (86% of all torn copper), which is
not "hold back the churners", it is reordering the whole run.

Net COST does discriminate. Gating on it, FOUR nets carry 52% of all the copper
destroyed on this board:

    +3V3  cost 101.0  67 rips  17115 segments torn
    +1V8  cost  41.3  37 rips   2819
    +1V0  cost  37.5  36 rips   1074
    +5V   cost  36.4  10 rips   1048

and once cost-gated the rip threshold barely matters (N=1 moves 52%, N=5 moves
50%), so N is a mild guard -- one free inline re-route -- not the mechanism.

RELEASED EXACTLY ONCE (`deferred_released`), so a deferred net cannot be deferred
again by a rip during the final round -- that would be a livelock where the net
is never routed at all.

KICAD_RIP_DEFER_AFTER=0 disables (default 2). The cost gate uses
net_cost.big_net_threshold (KICAD_BIG_NET_FACTOR); the push-back distance is
KICAD_RIP_DEFER_FRACTION (default 0.2 of the run).
"""
import os

# Rips after which a BIG net waits for the final round. 2 = one free inline
# re-route, then defer. The measured curve says this number is not load-bearing
# (N=1 and N=5 differ by 2% of moved copper); the cost gate is.
# DEFAULT 0 = OFF (measured: no effect on the shipped board; see below).
DEFER_AFTER = int(os.environ.get('KICAD_RIP_DEFER_AFTER', '0'))

# How far back a churning big net is pushed, as a fraction of the run.
# NOT to the end: routing a big rail last means routing it into a full
# board, converting churn into an unrouted net.
DEFER_FRACTION = float(os.environ.get('KICAD_RIP_DEFER_FRACTION', '0.2'))


def _is_big(state, net_id) -> bool:
    """Is this net expensive enough to be worth deferring? Threshold computed
    ONCE per run and cached on the state (it is a whole-board median)."""
    try:
        from net_cost import net_cost, big_net_threshold
    except ImportError:
        return False
    th = getattr(state, '_big_net_threshold', None)
    if th is None:
        pcb = getattr(state, 'pcb_data', None)
        if pcb is None:
            return False
        th = big_net_threshold(pcb, list((pcb.nets or {}).keys()))
        try:
            state._big_net_threshold = th
        except AttributeError:
            return False
    return th > 0 and net_cost(state.pcb_data, net_id) > th


def queue_reroute(state, item, net_id) -> bool:
    """Queue a just-ripped net for re-route, pushed BACK if it is big and churning.

    Returns True if queued normally, False if pushed back. Counting happens here
    because every single-ended reroute enqueue follows a rip of that net.

    Pushed back by a BOUNDED distance, not to the very end. Deferring a big power
    rail to last means re-routing it into a board with no room left, which trades
    churn for an UNROUTED net -- the same bad trade as a rip budget. A bounded
    push gives the neighbourhood time to settle while space still exists.
    DEFER_FRACTION of the run is the settling distance: measured, +3V3's median
    gap between a re-route and its next rip is 4 events out of 1901 (0.2% of the
    run), so 20% is ~95x more settling without going all the way to the end.
    """
    counts = state.rip_counts
    counts[net_id] = counts.get(net_id, 0) + 1
    if not _is_big(state, net_id):
        state.reroute_queue.append(item)      # cheap net: re-route inline
        return True
    if DEFER_AFTER <= 0 or counts[net_id] < DEFER_AFTER:
        state.reroute_queue.append(item)
        return True
    q = state.reroute_queue
    base = getattr(state, 'total_routes', 0) or len(q) or 1
    here = getattr(state, 'reroute_index', 0)
    dist = max(1, int(DEFER_FRACTION * base))
    pos = here + dist
    if pos >= len(q):
        q.append(item)                        # push-back exceeds the queue: last
        where = f"end (queue {len(q)})"
    else:
        q.insert(pos, item)
        where = f"+{dist} (index {pos} of {len(q)})"
    # Report the DECISION. A silent push-back is indistinguishable from "the
    # feature never ran", which is how three earlier checks in this investigation
    # passed vacuously.
    print(f"      churn: pushing back {item[1]} (rip #{counts[net_id]}) -> {where}",
          flush=True)
    return False


def release_deferred(state) -> int:
    """No-op retained for the reroute loop's drain condition.

    The design moved from "hold every churning net and release them all at the
    end" to a BOUNDED PUSH-BACK inside the single queue (see queue_reroute):
    end-deferral routes a big rail into a board with no room left, which turns
    churn into an unrouted net. Nothing is held back any more, so there is
    nothing to release -- but the loop still calls this when the queue drains,
    and a net pushed back past the end simply sits at the end of that same queue.
    """
    return 0
