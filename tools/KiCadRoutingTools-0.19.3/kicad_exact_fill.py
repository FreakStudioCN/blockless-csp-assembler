"""Exact zone-fill geometry from KiCad itself (pcbnew ZONE_FILLER).

Why this exists
---------------
ZoneFillModel and the oracle's island tracer are RASTER APPROXIMATIONS of
KiCad's fill. At marginal sites -- a pour pinched to ~the min-thickness
threshold, a corridor carved by clearance the model prices differently --
the approximation grades the fill CONNECTED where KiCad's exact polygon
math splits it. Every repair actor keyed off the model (region joiner,
island tracer) then sees one region where KiCad sees two, and the only
remaining actor is the link-router, which cannot handle zone|zone links
(zero-length: both endpoints are the pinch point) or walled endpoints.

This module asks KiCad for the truth: drive pcbnew's ZONE_FILLER headless
(refill + save, the fill-fidelity ground-truth recipe), then parse the
saved board's (filled_polygon ...) blocks. Each filled_polygon is one
connected island polygon -- island discovery comes free with the geometry,
so one pcbnew run yields both the diagnosis and the strap targets.

The pcbnew script is STRAIGHT-LINE module scope on purpose: pcbnew
scripting segfaults when board work happens inside functions/loops.

Availability: needs KiCad's bundled python (macOS/Windows) or a system
python with pcbnew (Linux distro installs). All entry points degrade to
None when unavailable; callers fall back to the raster model.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Tuple

EXACT_FILL_TIMEOUT = 300

_REFILL_SCRIPT = """\
import sys
import pcbnew
src = sys.argv[1]
dst = sys.argv[2]
board = pcbnew.LoadBoard(src)
filler = pcbnew.ZONE_FILLER(board)
zones = board.Zones()
filler.Fill(zones)
pcbnew.SaveBoard(dst, board)
print("REFILL_OK", len(list(zones)))
"""

_KICAD_PYTHONS = [
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/"
    "Versions/Current/bin/python3",
    "/usr/bin/python3",
    os.path.expandvars(r"C:\Program Files\KiCad\bin\python.exe"),
]


def find_kicad_python() -> Optional[str]:
    """Path of a python that can import pcbnew, or None."""
    for cand in _KICAD_PYTHONS:
        if cand and os.path.isfile(cand):
            return cand
    return None


def live_fill_islands(board):
    """{(net_name, layer_name): [island_polygon, ...]} from an IN-PROCESS
    ZONE_FILLER refill of a live pcbnew BOARD (#424 GUI path).

    Same result shape as refill_islands, with no file round-trip and no
    subprocess: the fill reflects exactly the copper the caller is routing
    against, unsaved edits included -- staleness is impossible by
    construction. Only callable under KiCad's python (pcbnew importable).
    Side effect: the live board's zone fills are recomputed, which is
    derived data KiCad refreshes on any fill action anyway.
    """
    import pcbnew
    zones = board.Zones()
    pcbnew.ZONE_FILLER(board).Fill(zones)
    out: Dict = {}
    for z in zones:
        if z.GetIsRuleArea():
            continue
        net = z.GetNetname()
        for lid in z.GetLayerSet().Seq():
            if not pcbnew.IsCopperLayer(lid):
                continue
            lname = board.GetLayerName(lid)
            sps = z.GetFilledPolysList(lid)
            for i in range(sps.OutlineCount()):
                ol = sps.Outline(i)
                poly = [(pcbnew.ToMM(ol.CPoint(j).x),
                         pcbnew.ToMM(ol.CPoint(j).y))
                        for j in range(ol.PointCount())]
                if len(poly) >= 3:
                    out.setdefault((net, lname), []).append(poly)
    return out


def _balanced_block(text: str, start: int) -> Tuple[str, int]:
    """The parenthesized block starting at text[start] == '(' and the index
    one past its closing paren. Quote-aware (net names may hold parens)."""
    depth = 0
    i = start
    n = len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            if c == '"' and text[i - 1] != '\\':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return text[start:i + 1], i + 1
        i += 1
    return text[start:], n


_XY_RE = re.compile(r'\(xy\s+([-\d.]+)\s+([-\d.]+)\)')
_LAYER_RE = re.compile(r'\(layer\s+"([^"]+)"\)')
_NET_ID_RE = re.compile(r'\(net\s+(\d+)\)')
_NET_NAME_RE = re.compile(r'\(net_name\s+"((?:[^"\\]|\\.)*)"\)')
# KiCad 10 files reference zone nets BY NAME with no net table (#344 class):
# (net "Earth") -- there is no numeric token and no net_name attribute.
_NET_STR_RE = re.compile(r'\(net\s+"((?:[^"\\]|\\.)*)"\)')


def refill_islands(board_file: str, timeout: int = EXACT_FILL_TIMEOUT,
                   verbose: bool = False, project_from: str = None
                   ) -> Optional[Dict[Tuple[str, str],
                                      List[List[Tuple[float, float]]]]]:
    """{(net_name, layer): [island_polygon, ...]} from a KiCad refill of
    `board_file`, or None when pcbnew is unavailable / the refill fails.

    The board is staged into a temp dir WITH its sibling .kicad_pro so the
    refill runs at the project's real netclasses (a bare board refills at
    stock rules and shrinks tight pours -- the phantom-divergence trap).
    Each (filled_polygon ...) block of the saved board is one connected
    island polygon (KiCad's fracture output), so island discovery is free.
    """
    kpy = find_kicad_python()
    if kpy is None:
        return None
    tmpdir = tempfile.mkdtemp(prefix='exact_fill_')
    try:
        stem = os.path.splitext(os.path.basename(board_file))[0]
        staged = os.path.join(tmpdir, stem + '.kicad_pcb')
        shutil.copyfile(board_file, staged)
        sib_pro = os.path.splitext(board_file)[0] + '.kicad_pro'
        if not os.path.isfile(sib_pro) and project_from:
            # Mid-chain boards have no sibling project yet (the writeback
            # runs after the oracle, #338) -- stage the INPUT's project so
            # the refill runs at the real netclasses, not stock rules.
            sib_pro = os.path.splitext(project_from)[0] + '.kicad_pro'
        if os.path.isfile(sib_pro):
            shutil.copyfile(sib_pro, os.path.join(tmpdir,
                                                  stem + '.kicad_pro'))
        script = os.path.join(tmpdir, 'refill.py')
        with open(script, 'w') as f:
            f.write(_REFILL_SCRIPT)
        filled = os.path.join(tmpdir, stem + '_filled.kicad_pcb')
        r = subprocess.run([kpy, script, staged, filled],
                           capture_output=True, text=True, timeout=timeout)
        if 'REFILL_OK' not in (r.stdout or '') or not os.path.isfile(filled):
            if verbose:
                print(f"  (exact-fill refill failed: rc={r.returncode} "
                      f"{(r.stderr or '').strip()[-200:]})")
            return None
        with open(filled, 'r', encoding='utf-8') as f:
            text = f.read()
    except Exception as e:
        if verbose:
            print(f"  (exact-fill unavailable: {e})")
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return parse_filled_islands(text)


def parse_filled_islands(text: str
                         ) -> Dict[Tuple[str, str],
                                   List[List[Tuple[float, float]]]]:
    """Parse (zone ...)/(filled_polygon ...) blocks of saved board text into
    {(net_name, layer): [island_polygon, ...]}. Net resolution: the zone's
    net_name token when present (KiCad <=9 keeps it), else the numeric
    (net N) mapped through the file's net table / item net names."""
    id_to_name: Dict[int, str] = {}
    for m in re.finditer(r'\(net\s+(\d+)\s+"((?:[^"\\]|\\.)*)"\)', text):
        id_to_name[int(m.group(1))] = m.group(2)
    out: Dict[Tuple[str, str], List[List[Tuple[float, float]]]] = {}
    pos = 0
    while True:
        z = text.find('(zone', pos)
        if z < 0:
            break
        block, pos = _balanced_block(text, z)
        head = block[:block.find('(filled_polygon')] \
            if '(filled_polygon' in block else block
        nm = _NET_NAME_RE.search(head) or _NET_STR_RE.search(head)
        net_name = nm.group(1) if nm else None
        if net_name is None:
            ni = _NET_ID_RE.search(head)
            if ni:
                net_name = id_to_name.get(int(ni.group(1)))
        if not net_name:
            continue
        fp_pos = 0
        while True:
            fp = block.find('(filled_polygon', fp_pos)
            if fp < 0:
                break
            fp_block, fp_pos = _balanced_block(block, fp)
            lm = _LAYER_RE.search(fp_block)
            if not lm:
                continue
            poly = [(float(a), float(b))
                    for a, b in _XY_RE.findall(fp_block)]
            if len(poly) >= 3:
                out.setdefault((net_name, lm.group(1)), []).append(poly)
    return out


def point_in_poly(x: float, y: float,
                  poly: List[Tuple[float, float]]) -> bool:
    """Even-odd ray cast."""
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xc = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xc:
                inside = not inside
    return inside


def exact_clusters(pcb_data, net_id: int, islands,
                   tolerance: float = 0.06):
    """Deterministic KiCad-truth connectivity clusters for one net.

    THE ORACLE REPLACEMENT (#490): kicad-cli DRC's connectivity is
    nondeterministic (threaded refill/ratsnest -- rp2040 gave three
    different unconnected-item reports on one unchanged board; orangecrab's
    repair graded 103/65/92 across identical-input runs because the welds
    chased a different report each round). pcbnew's ZONE_FILLER itself is
    measured-deterministic (3x byte-identical island signatures on both
    boards), so KiCad's fill truth + a deterministic union-find gives the
    same verdict every run -- and hands back real cluster geometry for
    strapping instead of ratsnest anchor names.

    `islands` = the net's exact filled_polygon list as [(layer, poly), ...]
    (from refill_islands). Copper components come from kicad_oracle's
    authoritative _net_track_components (no fill credit); an island joins a
    component when any of its segments (on the island's layer) or vias
    (any layer -- barrels pierce every fill) touches the island polygon;
    pads join via _cluster_points at their center, and directly join
    islands whose polygon contains them on a shared layer.

    Returns clusters largest-first, each
      {'islands': [idx...], 'pads': [Pad...], 'points': [(x, y, layer)...],
       'has_pads': bool}. len(clusters) > 1 => KiCad demands links.
    """
    import math as _m
    from kicad_oracle import _net_track_components, _cluster_points
    from kicad_parser import pad_is_plated_through

    comps = _net_track_components(pcb_data, net_id)
    comp_of_seg, comp_of_via, segs, vias, _ = comps
    # _net_track_components returns EMPTY comp lists when the net has no
    # segments (via-only nets) and None entries for vias the graph did not
    # place -- give every such item its own component identity.
    if len(comp_of_seg) != len(segs):
        comp_of_seg = ['s%d' % i for i in range(len(segs))]
    if len(comp_of_via) != len(vias):
        comp_of_via = ['v%d' % j for j in range(len(vias))]
    comp_of_via = [r if r is not None else 'v%d' % j
                   for j, r in enumerate(comp_of_via)]
    pads = list(pcb_data.pads_by_net.get(net_id, []))

    parent: dict = {}

    def find(a):
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    _samples_cache = {}

    def _island_samples(ii):
        if ii not in _samples_cache:
            _samples_cache[ii] = sample_poly_edges(islands[ii][1], cap=2000)
        return _samples_cache[ii]

    # Island <-> copper components.
    for ii, (layer, poly) in enumerate(islands):
        inode = ('i', ii)
        parent.setdefault(inode, inode)
        for i, s in enumerate(segs):
            if s.layer != layer:
                continue
            if point_in_poly(s.start_x, s.start_y, poly) \
                    or point_in_poly(s.end_x, s.end_y, poly):
                union(inode, ('c', comp_of_seg[i]))
        for j, v in enumerate(vias):
            if point_in_poly(v.x, v.y, poly):
                union(inode, ('c', comp_of_via[j]))

    # Pads <-> copper (position lookup through the authoritative comps) and
    # pads <-> islands (containment on a shared layer / plated barrel).
    for p in pads:
        pnode = ('p', id(p))
        parent.setdefault(pnode, pnode)
        cu = [l for l in p.layers if l.endswith('.Cu')]
        _th = pad_is_plated_through(p)
        lookup_layers = (None,) if _th or '*.Cu' in p.layers else tuple(cu)
        for lyr in lookup_layers:
            _, root = _cluster_points(pcb_data, net_id, p.global_x,
                                      p.global_y, lyr, comps, tol=tolerance
                                      + max(p.size_x, p.size_y) / 2.0)
            if root is not None:
                union(pnode, ('c', root))
                break
        # Pad <-> island: NEVER by center containment -- a thermal-relief
        # connection excludes the pad area from the filled polygon (the
        # spokes land on the pad ring), so the center tests OUTSIDE the
        # fill. Touch = any island boundary sample within the pad's copper
        # reach (+tol). zone_connection=none pads stay separate for free:
        # their fill keeps full clearance, beyond the reach test.
        _pr = max(p.size_x, p.size_y) / 2.0 + tolerance
        for ii, (layer, poly) in enumerate(islands):
            if not (_th or '*.Cu' in p.layers or layer in p.layers):
                continue
            if point_in_poly(p.global_x, p.global_y, poly):
                union(pnode, ('i', ii))
                continue
            for sx, sy in _island_samples(ii):
                if _m.hypot(sx - p.global_x, sy - p.global_y) <= _pr:
                    union(pnode, ('i', ii))
                    break

    # Every copper component gets a node even if it touched nothing.
    for r in set(comp_of_seg) | set(comp_of_via):
        parent.setdefault(('c', r), ('c', r))

    clusters: dict = {}

    def bucket(root):
        return clusters.setdefault(root, {'islands': [], 'pads': [],
                                          'points': [], 'has_pads': False})

    for ii, (layer, poly) in enumerate(islands):
        c = bucket(find(('i', ii)))
        c['islands'].append(ii)
        c['points'].extend((x, y, layer)
                           for x, y in sample_poly_edges(poly, cap=800))
    for p in pads:
        c = bucket(find(('p', id(p))))
        c['pads'].append(p)
        c['has_pads'] = True
        cu = [l for l in p.layers if l.endswith('.Cu')]
        c['points'].append((p.global_x, p.global_y, cu[0] if cu else None))
    for i, s in enumerate(segs):
        c = bucket(find(('c', comp_of_seg[i])))
        c['points'].append((s.start_x, s.start_y, s.layer))
        c['points'].append((s.end_x, s.end_y, s.layer))
    for j, v in enumerate(vias):
        c = bucket(find(('c', comp_of_via[j])))
        c['points'].append((v.x, v.y, None))
    return sorted(clusters.values(), key=lambda c: -len(c['points']))


def exact_unconnected(board_file: str, net_names=None, pcb_data=None,
                      verbose: bool = False, project_from: str = None):
    """Drop-in DETERMINISTIC replacement for kicad_oracle.kicad_unconnected:
    [(net, (x, y, layer|None, kind), (x, y, layer|None, kind)), ...] --
    one link per secondary cluster, anchored at the true nearest-approach
    pair between that cluster and the net's primary cluster (real strap
    geometry, not ratsnest item names). None when pcbnew is unavailable.

    Scope: nets in `net_names` get full island+copper clustering; every
    OTHER net with copper gets a cheap pad-less-component sweep so the
    debris pass keeps seeing stranded-fragment links (XTAL_O class).
    """
    import math as _m
    m = refill_islands(board_file, verbose=verbose,
                       project_from=project_from)
    if m is None:
        return None
    if pcb_data is None:
        from kicad_parser import parse_kicad_pcb
        pcb_data = parse_kicad_pcb(board_file)
    names = set(net_names or [])
    by_net: Dict[str, list] = {}
    for (net, layer), polys in m.items():
        by_net.setdefault(net, []).extend((layer, p) for p in polys)

    links = []

    def _nearest(pa_pts, pb_pts):
        best = (float('inf'), None, None)
        for ax, ay, al in pa_pts:
            for bx, by, bl in pb_pts:
                d = (ax - bx) ** 2 + (ay - by) ** 2
                if d < best[0]:
                    best = (d, (ax, ay, al), (bx, by, bl))
        return best[1], best[2]

    name_to_id = {net.name: nid for nid, net in pcb_data.nets.items()}
    for name in sorted(names):
        nid = name_to_id.get(name)
        if nid is None:
            continue
        cl = exact_clusters(pcb_data, nid, by_net.get(name, []))
        if len(cl) <= 1:
            continue
        primary = cl[0]
        # cap point sets for the O(n*m) nearest scan
        ppts = primary['points'][:1500]
        for c in cl[1:]:
            a, b = _nearest(c['points'][:800], ppts)
            if a is None:
                continue
            kind_a = 'zone' if c['islands'] and not c['pads'] else \
                ('pad' if c['pads'] else 'track')
            links.append((name, (a[0], a[1], a[2], kind_a),
                          (b[0], b[1], b[2], 'zone'
                           if primary['islands'] else 'track')))

    # Debris sweep on every other net with copper: pad-less components.
    from kicad_oracle import _net_track_components
    for nid, net in pcb_data.nets.items():
        if not net.name or net.name in names:
            continue
        segs = [s for s in pcb_data.segments if s.net_id == nid]
        vias = [v for v in pcb_data.vias if v.net_id == nid]
        if not segs and not vias:
            continue
        pads = pcb_data.pads_by_net.get(nid, [])
        comps = _net_track_components(pcb_data, nid)
        comp_of_seg, comp_of_via, csegs, cvias, _cl2 = comps
        if not comp_of_seg and not comp_of_via:
            continue
        roots = set(comp_of_seg) | set(r for r in comp_of_via
                                       if r is not None)
        # A single-component net with NO pads demands nothing (one cluster,
        # no ratsnest). Debris semantics: a pad-less component matters when
        # the net ALSO has pads elsewhere (XTAL_O class) or splits into
        # multiple components.
        if len(roots) <= 1 and not pads:
            continue
        # A component with no pad within reach of any of its items is
        # debris-candidate; emit one self-link so the oracle's stranded-
        # fragment deletion can inspect it (it re-derives authoritatively).
        for root in sorted(roots, key=str):
            pts = []
            for i, s in enumerate(csegs):
                if comp_of_seg[i] == root:
                    pts.append((s.start_x, s.start_y, s.layer))
            for j, v in enumerate(cvias):
                if j < len(comp_of_via) and comp_of_via[j] == root:
                    pts.append((v.x, v.y, None))
            if not pts:
                continue
            has_pad = any(
                _m.hypot(p.global_x - x, p.global_y - y)
                <= max(p.size_x, p.size_y) / 2.0 + 0.06
                for p in pads for x, y, _l in pts)
            if not has_pad:
                x, y, lyr = pts[0]
                links.append((net.name, (x, y, lyr, 'track'),
                              (x, y, lyr, 'track')))
    return links


def rasterize_interior(poly: List[Tuple[float, float]],
                       window: Tuple[float, float, float, float],
                       step: float = 0.25, inset: float = 0.0,
                       edge_samples=None) -> List[Tuple[float, float]]:
    """Grid points inside `poly`, clipped to the `window` bbox, at least
    `inset` from the boundary (distance to `edge_samples`). Row-vectorized
    ray cast (numpy) -- pure-python point-in-poly over a window against a
    10k-vertex island polygon is prohibitive."""
    import numpy as np
    x0, y0, x1, y1 = window
    xs = np.arange(x0, x1 + step, step)
    ys = np.arange(y0, y1 + step, step)
    if not len(xs) or not len(ys):
        return []
    px = np.asarray([p[0] for p in poly])
    py = np.asarray([p[1] for p in poly])
    x2 = np.roll(px, -1)
    y2 = np.roll(py, -1)
    inside = np.zeros((len(xs), len(ys)), dtype=bool)
    for j, yv in enumerate(ys):
        mask = (py > yv) != (y2 > yv)
        if not mask.any():
            continue
        xc = px[mask] + (yv - py[mask]) * (x2[mask] - px[mask]) \
            / (y2[mask] - py[mask])
        cnt = (xs[:, None] < xc[None, :]).sum(axis=1)
        inside[:, j] = (cnt % 2) == 1
    ii, jj = np.nonzero(inside)
    pts = np.column_stack([xs[ii], ys[jj]])
    if inset > 0 and edge_samples is not None and len(pts):
        es = np.asarray([(p[0], p[1]) for p in edge_samples])
        keep = np.ones(len(pts), dtype=bool)
        chunk = 4000
        for i0 in range(0, len(pts), chunk):
            d2 = ((pts[i0:i0 + chunk, None, :] - es[None, :, :]) ** 2
                  ).sum(axis=2).min(axis=1)
            keep[i0:i0 + chunk] = d2 >= inset * inset
        pts = pts[keep]
    return [(float(x), float(y)) for x, y in pts]


def sample_poly_edges(poly: List[Tuple[float, float]], step: float = 0.25,
                      cap: int = 4000) -> List[Tuple[float, float]]:
    """Points along the polygon boundary every ~`step` mm (vertices always
    included), decimated to <= cap points."""
    import math
    pts: List[Tuple[float, float]] = []
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        pts.append((x1, y1))
        seg_len = math.hypot(x2 - x1, y2 - y1)
        for k in range(1, int(seg_len / step)):
            t = k * step / seg_len
            pts.append((x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
    if len(pts) > cap:
        stride = len(pts) // cap + 1
        pts = pts[::stride]
    return pts
