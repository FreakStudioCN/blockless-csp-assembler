"""Placement blocks: which footprints should move as one rigid body (#459).

The quench fixes what it can reach — individual parts within a few mm. The
failures that sink a board are block-scale: a magnetics block 80mm from its
endpoints, unroutable because of the floorplan rather than the routing. Before
anything can *move* a block, something has to know what a block IS, and nothing
in this repo did.

Four sources, in precedence order, first match wins per footprint. Each is
reported with its source so a user can see what was inferred before trusting it.

    kicad      KiCad (group ...) blocks -- the designer's own statement that
               these parts belong together, so it outranks everything inferred.
    sheet      Schematic sheet path. A sheet IS the functional block a floorplan
               thinks in, and it is declarative rather than guessed.
    netprefix  Net-name prefix (AUDIO_*, GPDI_*), gated on spatial coherence.
    decap      Two-pad capacitors tethered to the IC they decouple.

CORPUS EVIDENCE (measured across kicad_files/, not assumed):

  kicad      0 of 27 boards carry a single (group ...) block. Exact when present,
             but no in-repo board exercises it end to end.
  sheet      12 of 22 boards with (path ...) have more than one sheet. ulx3s: 11
             sheets sized 83/34/23/20/20/12 footprints. glasgow_revC: 4, nested.
             This is the workhorse.
  decap      Median distance from a 2-pad cap to the nearest IC bbox is 0.0-2.6mm
             across 8 boards, and the cap shares a net with that IC in 93-100% of
             cases (e.g. glasgow 91/92, orangecrab 75/75, ulx3s 57/70). The
             net-sharing check is what makes this safe: it is what separates "a
             cap that decouples this IC" from "a cap that happens to sit nearby",
             and on ulx3s it correctly rejects 13 of 70.
  netprefix  The WEAKEST source, and it is worth saying so. Raw prefixes are
             dominated by KiCad's auto-generated Net-(U1-Pad) names -- 21 to 92
             refs per board under a bogus "NET" prefix, spread over 16-75mm.
             With auto-generated and power names excluded and a 20mm coherence
             gate, what remains is small but genuine: ulx3s 9 blocks / 63 refs
             (AUDIO, GPDI, JTAG, LED, FPGA), orangecrab 13/80, glasgow 3/20,
             rp2350 1 block of 3, tigard 0. Enable it expecting little.

All output is sorted. Anything whose order can reach the optimizer must be
deterministic, or quench output stops being reproducible across processes (#457).
"""
from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Set, Tuple

# --- decap tethering -------------------------------------------------------
# 5mm from the IC's bounding box. The corpus median is 0.0-2.6mm and <5mm
# captures essentially every real decoupling cap; kit-dev (a large, loosely
# packed board) is the only one needing the full 5mm for 41 of its 53.
DECAP_RADIUS_MM = 5.0
DECAP_MIN_IC_PADS = 4          # what chip_boundary already calls "not a passive"

# --- netprefix -------------------------------------------------------------
# KiCad's auto-generated names carry no functional meaning: Net-(U1-Pad3),
# unconnected-(U2-Pad7). Bucketing on them produces one enormous "NET" pseudo
# block per board (up to 92 refs spread over 75mm), which is exactly the kind of
# wrong group that is worse than no group at all -- it would rigidly drag half
# the board.
_AUTO_NET = re.compile(r'^(net-\(|unconnected-\(|net\d)', re.I)
# Power and ground reach everywhere by design, so they say nothing about blocks.
_POWER_NET = re.compile(r'^(a?gnd|dgnd|pgnd|vcc|vdd|vss|vee|vbus|vin|vout|vref|'
                        r'pwr|\+|-|\d)', re.I)
_PREFIX = re.compile(r'^([A-Za-z][A-Za-z0-9]+)[_\-]')
NETPREFIX_MIN_REFS = 3
# Max distance from the cluster centroid to any member. A prefix scattered wider
# than this is a naming convention, not a physical block.
NETPREFIX_MAX_SPREAD_MM = 20.0

MIN_GROUP_SIZE = 2             # a block of one is not a block

SOURCES = ('kicad', 'sheet', 'netprefix', 'decap')
AUTO_SOURCES = ('kicad', 'sheet')   # the declarative ones


class GroupError(ValueError):
    """An unknown --group-by source."""


def parse_sources(spec: Optional[str]) -> Tuple[str, ...]:
    """'none' | 'auto' | comma list -> the ordered source tuple."""
    if not spec or spec.strip().lower() in ('', 'none'):
        return ()
    out: List[str] = []
    for tok in spec.split(','):
        tok = tok.strip().lower()
        if not tok:
            continue
        if tok == 'auto':
            out.extend(s for s in AUTO_SOURCES if s not in out)
            continue
        if tok not in SOURCES:
            raise GroupError(
                f"unknown group source {tok!r}; choose from "
                f"{', '.join(SOURCES)}, or 'auto' ({'+'.join(AUTO_SOURCES)}), "
                f"or 'none'")
        if tok not in out:
            out.append(tok)
    return tuple(out)


def _centroid(fp) -> Tuple[float, float]:
    pts = [(p.global_x, p.global_y) for p in fp.pads]
    if not pts:
        return (fp.x, fp.y)
    return (sum(p[0] for p in pts) / len(pts),
            sum(p[1] for p in pts) / len(pts))


def _sheet_of(fp) -> str:
    """The sheet prefix of a footprint's (path ...): everything but the LAST
    uuid, which is the symbol itself. A bare single-uuid path is top level and
    yields '' -- those must not all collapse into one bogus block."""
    parts = [q for q in (fp.sheet_path or '').split('/') if q]
    return '/'.join(parts[:-1])


def _from_kicad(pcb_data, movable) -> Dict[str, List[str]]:
    return {f"kicad:{name}": [r for r in refs if r in movable]
            for name, refs in (pcb_data.groups or {}).items()}


def _from_sheet(pcb_data, movable) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for ref in movable:
        fp = pcb_data.footprints.get(ref)
        if fp is None:
            continue
        sheet = _sheet_of(fp)
        if not sheet:          # top level: not a block
            continue
        out.setdefault(f"sheet:{sheet}", []).append(ref)
    return out


def _from_netprefix(pcb_data, movable) -> Dict[str, List[str]]:
    buckets: Dict[str, Set[str]] = {}
    for net_id, net in (pcb_data.nets or {}).items():
        if net_id <= 0:
            continue
        leaf = (net.name or '').split('/')[-1]
        if not leaf or _AUTO_NET.match(leaf) or _POWER_NET.match(leaf):
            continue
        m = _PREFIX.match(leaf)
        if not m:
            continue
        for pad in net.pads:
            if pad.component_ref in movable:
                buckets.setdefault(m.group(1).upper(), set()).add(pad.component_ref)

    out: Dict[str, List[str]] = {}
    for prefix, refs in buckets.items():
        if len(refs) < NETPREFIX_MIN_REFS:
            continue
        pts = [_centroid(pcb_data.footprints[r]) for r in refs
               if r in pcb_data.footprints]
        if len(pts) < NETPREFIX_MIN_REFS:
            continue
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        # Spatial coherence: a prefix whose parts are scattered across the board
        # is a naming convention, not something that can move as one body.
        if max(math.hypot(p[0] - cx, p[1] - cy) for p in pts) > NETPREFIX_MAX_SPREAD_MM:
            continue
        out[f"net:{prefix}"] = sorted(refs)
    return out


def _from_decap(pcb_data, movable) -> Dict[str, List[str]]:
    """Two-pad caps tethered to the IC they decouple.

    Nearest IC by bounding-box distance, but ONLY when the cap shares a net with
    it. Proximity alone is not enough -- on ulx3s 13 of 70 caps sit near an IC
    they have no electrical relationship with, and dragging those along would be
    a wrong group.
    """
    from chip_boundary import build_chip_list
    chips = build_chip_list(pcb_data, min_pads=DECAP_MIN_IC_PADS)
    if not chips:
        return {}
    ic_nets = {}
    for c in chips:
        fp = pcb_data.footprints.get(c.reference)
        if fp is not None:
            ic_nets[c.reference] = {p.net_id for p in fp.pads if p.net_id > 0}

    out: Dict[str, List[str]] = {}
    for ref in movable:
        fp = pcb_data.footprints.get(ref)
        if fp is None or not ref[:1].upper() == 'C':
            continue
        nets = {p.net_id for p in fp.pads if p.net_id > 0}
        if len(nets) != 2:            # a decoupling cap bridges exactly two nets
            continue
        cx, cy = _centroid(fp)
        best, best_d = None, None
        for c in chips:
            if c.reference == ref:
                continue
            x0, y0, x1, y1 = c.bounds
            d = math.hypot(max(x0 - cx, cx - x1, 0.0), max(y0 - cy, cy - y1, 0.0))
            if best_d is None or d < best_d:
                best, best_d = c.reference, d
        if best is None or best_d > DECAP_RADIUS_MM:
            continue
        if not (nets & ic_nets.get(best, set())):
            continue              # near, but not electrically its cap
        out.setdefault(f"decap:{best}", []).append(ref)
    # The IC itself anchors the tether, so it belongs to the block.
    for key, refs in out.items():
        ic = key.split(':', 1)[1]
        if ic in movable:
            refs.append(ic)
    return out


_BUILDERS = {'kicad': _from_kicad, 'sheet': _from_sheet,
             'netprefix': _from_netprefix, 'decap': _from_decap}


def derive_groups(pcb_data, sources, movable=None) -> Dict[str, List[str]]:
    """{group name: sorted [reference]} for the requested sources.

    `movable` restricts membership to refs the caller is willing to move (locked
    parts, or the loop's target set); None means every footprint.

    Precedence is the order of `sources`, and a ref lands in at most ONE group:
    an explicit KiCad group must not be torn apart by a later sheet or prefix
    rule. Groups smaller than MIN_GROUP_SIZE after that are dropped.
    """
    if not sources:
        return {}
    if movable is None:
        movable = set(pcb_data.footprints)
    else:
        movable = set(movable)

    claimed: Set[str] = set()
    out: Dict[str, List[str]] = {}
    for src in sources:
        builder = _BUILDERS.get(src)
        if builder is None:
            raise GroupError(f"unknown group source {src!r}")
        for name, refs in sorted(builder(pcb_data, movable).items()):
            members = sorted({r for r in refs if r not in claimed})
            if len(members) < MIN_GROUP_SIZE:
                continue
            out[name] = members
            claimed.update(members)
    return out


def _short(name: str) -> str:
    """Readable form of a group key. Sheet keys are uuid paths -- KiCad's
    Sheetname property is absent from every corpus board, so the uuid is all
    there is; show its tail rather than 36 meaningless characters."""
    src, _, rest = name.partition(':')
    if src != 'sheet':
        return name
    return 'sheet:' + '/'.join(p[-8:] for p in rest.split('/') if p)


def describe(groups: Dict[str, List[str]], limit: int = 8) -> str:
    """One-line-per-block summary, for the run banner."""
    if not groups:
        return "  placement groups: none"
    lines = [f"  placement groups: {len(groups)} block(s), "
             f"{sum(len(v) for v in groups.values())} part(s)"]
    for name, refs in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:limit]:
        shown = ', '.join(refs[:6]) + (', ...' if len(refs) > 6 else '')
        lines.append(f"    {_short(name)}  ({len(refs)}): {shown}")
    if len(groups) > limit:
        lines.append(f"    ... and {len(groups) - limit} more")
    return '\n'.join(lines)
