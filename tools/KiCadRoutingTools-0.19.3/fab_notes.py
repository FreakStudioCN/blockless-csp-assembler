"""Fabrication notes for geometry this tool creates that the fab must know about.

Right now: via-in-pad (#489 §8). The tool places it from three separate paths
(qfn_fanout `allow_via_in_pad`, `route_planes --same-net-pad-clearance -1`,
bga_fanout/underpad) and said nothing about the requirement it had just created.
Via-in-pad needs IPC-4761 Type VII -- filled, capped and plated -- or solder
wicks out of the joint into the barrel during reflow. The tool cannot enforce
that (it is a fab process, not board geometry), so the least it can do is COUNT
the sites and say so in the run output.

Leaf module: no routing imports, so any front (CLI main, shared engine, GUI tab)
can call it.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# IPC-4761 Type VII is the protection class via-in-pad requires.
VIA_IN_PAD_FAB_NOTE = (
    "via-in-pad requires IPC-4761 Type VII (filled + capped + plated); "
    "state it on the fab drawing or solder wicks into the barrel")


def _get(obj, name: str, default=None):
    """Read `name` from a dict or an attribute-style object."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _pad_holds(pad, x: float, y: float, margin: float = 0.0) -> bool:
    """Is (x, y) within the pad's copper extent? size_x/size_y are already
    resolved to board space by the parser, so this is an axis-aligned test."""
    hx = _get(pad, 'size_x', 0.0) / 2 + margin
    hy = _get(pad, 'size_y', 0.0) / 2 + margin
    return (abs(x - _get(pad, 'global_x', 0.0)) <= hx
            and abs(y - _get(pad, 'global_y', 0.0)) <= hy)


def via_in_pad_sites(vias, pads_by_net: Dict[int, list],
                     margin: float = 0.0) -> List[Tuple[object, object]]:
    """[(via, pad)] for every via whose centre lands inside a SAME-NET pad.

    Same-net only: a via inside a FOREIGN pad is a short, not via-in-pad, and is
    the DRC checkers' business. Accepts vias as dicts or objects.
    """
    sites = []
    for via in vias or ():
        net_id = _get(via, 'net_id')
        if net_id is None:
            continue
        vx, vy = _get(via, 'x'), _get(via, 'y')
        if vx is None or vy is None:
            continue
        for pad in pads_by_net.get(net_id, ()) or ():
            if _get(pad, 'drill', 0.0) or 0.0:
                continue  # a plated TH pad's own barrel is not via-in-pad
            if _pad_holds(pad, vx, vy, margin):
                sites.append((via, pad))
                break
    return sites


def via_in_pad_summary(vias, pads_by_net: Dict[int, list],
                       margin: float = 0.0) -> Optional[Dict]:
    """Machine-readable via-in-pad record, or None when there is none.

    {'count', 'pads' (["U1.A1", ...]), 'note'} -- returned so a caller can put it
    in a run summary instead of only printing it.
    """
    sites = via_in_pad_sites(vias, pads_by_net, margin)
    if not sites:
        return None
    refs = []
    for _via, pad in sites:
        ref = f"{_get(pad, 'component_ref', '?')}.{_get(pad, 'pad_number', '?')}"
        if ref not in refs:
            refs.append(ref)
    return {'count': len(sites), 'pads': refs, 'note': VIA_IN_PAD_FAB_NOTE}


def print_via_in_pad_note(vias, pads_by_net: Dict[int, list],
                          context: str = "", margin: float = 0.0,
                          max_refs: int = 8) -> Optional[Dict]:
    """Print one fab note when this run put vias in pads. Returns the record."""
    record = via_in_pad_summary(vias, pads_by_net, margin)
    if not record:
        return None
    where = f" ({context})" if context else ""
    shown = record['pads'][:max_refs]
    more = len(record['pads']) - len(shown)
    print(f"\n  FAB NOTE{where}: {record['count']} via(s) placed in pad(s) "
          f"[{', '.join(shown)}{f', +{more} more' if more > 0 else ''}] -- "
          f"{VIA_IN_PAD_FAB_NOTE}.")
    return record
