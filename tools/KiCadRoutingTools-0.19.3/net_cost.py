#!/usr/bin/env python3
"""How expensive is a net to re-route? -- the shared "big net" metric.

Rip churn on a real board is dominated by a handful of large nets. Measured on
muzy_zynq2 (1126 rips, 42447 segments torn out across the chain):

    net      pads  span    cost   rips  segments torn
    +3V3       87  103.5   94.9     67   17115   <- 40% of ALL torn copper
    +1V8       36   44.6   40.1     37    2819
    +1V0       30   46.6   37.4     36    1074
    +5V        11   99.2   33.0     10    1048
    FPGA_RX     5   76.7   19.6      2      62
    (typical 2-pad net)     2.0-2.5  0-1     0-4

Nets with >=8 pads averaged 37.5 rips and 5514 segments torn; nets with <8 pads
averaged 8.9 rips and 185 segments. Large nets are ripped ~4x as often and lose
~30x the copper -- and each rip is a re-route of the most expensive thing on the
board.

THE METRIC

    cost = sqrt(pads) * sqrt(span_mm)

Both terms matter and both are damped:

  * PADS alone is not enough. +5V has only 11 pads but spans 99mm and still cost
    10 rips / 1048 segments -- a long thin net crosses many other nets' corridors
    regardless of pin count.
  * SPAN alone is not enough either: a 2-pad net across the board is cheap to
    re-route (one path), unlike a 30-pad tree.
  * The SQRTs damp both. A linear penalty would make the biggest net effectively
    un-rippable, which converts churn into unrouted nets -- the wrong trade. The
    aim is to bias the choice toward cheaper victims, not to forbid the expensive
    one.

Span is the max pairwise pad distance, computed off the pad bounding box
(diagonal) rather than an O(n^2) sweep: on a 174-pad net the exact sweep is
15k distance computations for a number used only to rank.

SCALE-FREE THRESHOLDS. Absolute cost values do not port between boards -- span
is in mm, so a small board's worst net may score below a large board's median.
Callers that need a "is this a big net" decision should use
`big_net_threshold()`, which returns a MULTIPLE OF THE BOARD'S MEDIAN net cost.
"""
import math
import os
from typing import Dict, Optional

# Multiple of the board's median net cost above which a net counts as "big".
# Default 4.0 sits in the natural gap measured on muzy_zynq2 (+5V 33.0 vs the
# next net down at 19.6) without being tuned to that board: it is expressed in
# medians, not millimetres.
# DEFAULT 0 = OFF. Measured on muzy_zynq2's live chain, holding big nets out of
# direct-first changed the routing order provably (+3V3 moved from slot 2 to 16,
# +1V0 from 3 to 81) and changed the churn NOT AT ALL. Set 4.0 to re-enable.
BIG_NET_FACTOR = float(os.environ.get('KICAD_BIG_NET_FACTOR', '0'))

def _cache_for(pcb_data) -> Dict[int, float]:
    """Per-BOARD cache, stored on the board object.

    NOT a module-level dict keyed by net_id: a long-running process (the GUI
    routes several boards; tests reuse fixtures) would then serve costs computed
    for a DIFFERENT board, since net ids restart per board. Keying on id(pcb_data)
    is also wrong -- CPython reuses ids after GC, so a freed board's entry can be
    silently inherited by its successor. Hanging the dict off the board ties the
    cache lifetime to exactly the data it describes.
    """
    c = getattr(pcb_data, '_net_cost_cache', None)
    if c is None:
        c = {}
        try:
            pcb_data._net_cost_cache = c
        except AttributeError:
            pass          # exotic/slotted board object: degrade to uncached
    return c


def net_cost(pcb_data, net_id: int) -> float:
    """sqrt(pads) * sqrt(span_mm) for `net_id`; 0.0 for a net with <2 pads.

    O(pads) on first call, O(1) thereafter -- callers hit this inside the rip
    loop, once per candidate blocker per rip.
    """
    _cache = _cache_for(pcb_data)
    if net_id in _cache:
        return _cache[net_id]
    pads = (getattr(pcb_data, 'pads_by_net', None) or {}).get(net_id)
    if pads is None:
        net = (pcb_data.nets or {}).get(net_id)
        pads = getattr(net, 'pads', None) or []
    if len(pads) < 2:
        _cache[net_id] = 0.0
        return 0.0
    xs = [p.global_x for p in pads]
    ys = [p.global_y for p in pads]
    span = math.hypot(max(xs) - min(xs), max(ys) - min(ys))   # bbox diagonal
    cost = math.sqrt(len(pads)) * math.sqrt(max(span, 0.1))
    _cache[net_id] = cost
    return cost


def big_net_threshold(pcb_data, net_ids=None, factor: Optional[float] = None) -> float:
    """Cost above which a net is 'big', as a multiple of the board's MEDIAN.

    Scale-free by construction, so the same factor behaves sensibly on a 20mm
    module and a 200mm backplane. Returns 0.0 when there is nothing to rank
    (every caller then treats no net as big, i.e. the feature is inert).
    """
    if factor is None:
        factor = BIG_NET_FACTOR
    ids = list(net_ids) if net_ids is not None else list((pcb_data.nets or {}).keys())
    costs = sorted(c for c in (net_cost(pcb_data, n) for n in ids) if c > 0)
    if not costs:
        return 0.0
    median = costs[len(costs) // 2]
    return median * factor


def clear_cache(pcb_data=None) -> None:
    """Drop a board's cost cache (pads do not move mid-run; for tests/tools)."""
    if pcb_data is not None and hasattr(pcb_data, '_net_cost_cache'):
        pcb_data._net_cost_cache.clear()
