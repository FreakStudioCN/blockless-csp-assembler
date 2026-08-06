"""Plane-fragility soft routing costs (#424, planes-first chains).

A signal track only BISECTS a plane where the plane is NARROW: a crossing
through a wide pour region costs nothing (copper flows around it at fill
time), while a crossing at a neck severs the pour into islands the repair
step then has to stitch. Bisection is topological, but local narrowness is
a computable proxy: this module rasterizes the pour's copper at the routing
grid, measures every fill cell's distance to the copper boundary (iterative
erosion, no scipy), and stamps a soft per-cell cost on the zone's layer
that is negligible mid-pour and steep at necks.

The copper rasterized is the EXACT KiCad fill when available (a ZONE_FILLER
refill of the batch's input board via kicad_exact_fill -- so voids cut by
fanout barrels and earlier copper make the surviving straits expensive,
which is the "penalize what would break the plane" semantics), falling back
to the drawn zone outline when no board file / KiCad python exists (live GUI
boards). For a full-board outline the fallback prices only a board-edge
band -- near-useless; the exact fill is the real lever.

Delivery reuses the congestion-field transport (#424 Phase D): the field is
computed once at batch start into (N, 4) [layer, gx, gy, cost] rows under a
reserved track_proximity_cache key; merge_track_proximity_costs re-applies
it on every per-net prepare in every path. Vias in fragile cells pay
via_proximity_cost x the cell cost through the Rust via branch (the C6
coupling), which is exactly right: a via is a permanent hole in the plane.

Static v1: the field reflects the pours as they exist at batch start (in a
planes-first chain, the just-poured planes). Necks created later by in-run
copper are invisible; a dynamic refresh is the #466 extension.

DEFAULT ON at 2.0 mm-equivalent (2026-08-01, Andy): with plane drops (#424
D2) and a planes-first chain this measured the winning composition on
ottercast (4 vs 5 conn issues, GND+power fully connected, +3V3 pour a
single intact island, GND weld segments 3075 -> 1063, 63/70 balls served
by the pour alone). Inert on boards with no zones (signals-then-planes
chains feel it only after the pour exists). Corpus A/B pending -- revert
per-run with KICAD_PLANE_FRAGILITY_COST=0.

Knobs via environment (no CLI flags; both fronts inherit the default
through batch_route, so CLI/GUI parity needs no wiring):
  KICAD_PLANE_FRAGILITY_COST   mm-equivalent per-cell cost at a zone
                               boundary cell (default 2.0; 0 = off)
  KICAD_PLANE_FRAGILITY_WIDTH  half-width in mm at which the ramp reaches
                               zero (default 2.0: cells deeper than this
                               into the pour cost nothing)

GUI caveat: a live pcbnew board has no source file, so the field falls
back to the drawn OUTLINE there (near-inert for full-board pours) -- a
known CLI/GUI fidelity gap of the same class as the other
source_path-gated exact-fill features.
"""
from __future__ import annotations

import os
from typing import Dict

import numpy as np

from kicad_parser import PCBData
from routing_config import GridCoord, GridRouteConfig

# Reserved track_proximity_cache key (B1 uses -1, congestion -2).
PLANE_FRAGILITY_CACHE_KEY = -3


def fragility_cost_mm() -> float:
    try:
        return float(os.environ.get('KICAD_PLANE_FRAGILITY_COST', '2.0') or 0)
    except ValueError:
        return 2.0


def _rasterize_polygon(poly, coord, gx0, gy0, W, H) -> np.ndarray:
    """Boolean (H, W) fill mask of a polygon via even-odd crossing test,
    vectorized one scanline row at a time (grid rows are few thousand)."""
    xs = np.array([p[0] for p in poly])
    ys = np.array([p[1] for p in poly])
    mask = np.zeros((H, W), dtype=bool)
    col_x = np.array([coord.to_float(gx0 + i, 0)[0] for i in range(W)])
    x1s, y1s = xs, ys
    x2s, y2s = np.roll(xs, -1), np.roll(ys, -1)
    for j in range(H):
        y = coord.to_float(0, gy0 + j)[1]
        # edges straddling this scanline
        straddle = (y1s <= y) != (y2s <= y)
        if not straddle.any():
            continue
        xi = x1s[straddle] + (y - y1s[straddle]) / (y2s[straddle] - y1s[straddle]) \
            * (x2s[straddle] - x1s[straddle])
        # parity of crossings to the left of each column x
        mask[j] = (col_x[None, :] > xi[:, None]).sum(axis=0) % 2 == 1
    return mask


def compute_plane_fragility_cells(pcb_data: PCBData,
                                  config: GridRouteConfig) -> np.ndarray:
    """(N, 4) [layer, gx, gy, cost] rows for every filled-zone cell within
    the fragility half-width of its zone's boundary. Empty when disabled or
    the board has no filled zones on routing layers."""
    cost_mm = fragility_cost_mm()
    if cost_mm <= 0 or not pcb_data.zones:
        return np.empty((0, 4), dtype=np.int32)
    try:
        width_mm = float(os.environ.get('KICAD_PLANE_FRAGILITY_WIDTH', '2.0') or 2.0)
    except ValueError:
        width_mm = 2.0

    coord = GridCoord(config.grid_step)
    layer_index = {l: i for i, l in enumerate(config.layers)}
    depth = max(1, int(round(width_mm / config.grid_step)))
    rows = []

    # Exact fill polygons (KiCad truth): the GUI's live-board provider first
    # (#424: an in-process ZONE_FILLER refill of the board object itself --
    # no save, no staleness), then a refill of the source file (CLI), then
    # the drawn zone outlines as the last resort. Each entry: (layer, poly).
    polys = []
    provider = getattr(pcb_data, 'exact_fill_provider', None)
    if provider is not None:
        try:
            fills = provider()
            polys = [(layer, poly) for (_net, layer), pp in fills.items()
                     for poly in pp]
            if polys:
                print(f"Plane fragility: exact-fill geometry "
                      f"({len(polys)} filled island(s) from the LIVE board)")
        except Exception as e:
            print(f"Plane fragility: live-board fill unavailable ({e}); "
                  f"trying the source file")
            polys = []
    src = getattr(pcb_data, 'source_path', None)
    if not polys and src and os.path.isfile(src):
        try:
            from kicad_exact_fill import refill_islands
            fills = refill_islands(src)
            polys = [(layer, poly) for (_net, layer), pp in fills.items()
                     for poly in pp]
            if polys:
                print(f"Plane fragility: exact-fill geometry "
                      f"({len(polys)} filled island(s) from KiCad refill)")
        except Exception as e:
            print(f"Plane fragility: exact refill unavailable ({e}); "
                  f"using zone outlines")
            polys = []
    if not polys:
        polys = [(z.layer, z.polygon) for z in pcb_data.zones]

    for zlayer, zpoly in polys:
        li = layer_index.get(zlayer)
        if li is None or len(zpoly) < 3:
            continue
        xs = [p[0] for p in zpoly]
        ys = [p[1] for p in zpoly]
        gx0, gy0 = coord.to_grid(min(xs), min(ys))
        gx1, gy1 = coord.to_grid(max(xs), max(ys))
        W, H = int(gx1 - gx0 + 1), int(gy1 - gy0 + 1)
        if W <= 2 or H <= 2 or W * H > 40_000_000:
            continue  # degenerate or absurdly large raster
        mask = _rasterize_polygon(zpoly, coord, gx0, gy0, W, H)
        if not mask.any():
            continue
        # distance-to-boundary in grid steps by iterative 4-neighbour erosion,
        # capped at `depth` (cells deeper than the ramp are never emitted)
        dist = np.zeros((H, W), dtype=np.int16)
        cur = mask.copy()
        for d in range(1, depth + 1):
            inner = cur.copy()
            inner[1:, :] &= cur[:-1, :]
            inner[:-1, :] &= cur[1:, :]
            inner[:, 1:] &= cur[:, :-1]
            inner[:, :-1] &= cur[:, 1:]
            # boundary band peeled at this depth
            band = cur & ~inner
            dist[band] = d
            cur = inner
            if not cur.any():
                break
        emitted = mask & (dist > 0)
        if not emitted.any():
            continue
        jj, ii = np.nonzero(emitted)
        frag = 1.0 - (dist[jj, ii].astype(np.float64) - 1) / depth  # 1 at edge -> ~0 deep
        cell_cost = config.cell_cost(cost_mm)
        costs = np.maximum(1, (frag * cell_cost).astype(np.int32))
        zone_rows = np.column_stack([
            np.full(len(jj), li, dtype=np.int32),
            (ii + gx0).astype(np.int32),
            (jj + gy0).astype(np.int32),
            costs,
        ])
        rows.append(zone_rows)

    if not rows:
        return np.empty((0, 4), dtype=np.int32)
    out = np.vstack(rows)
    # overlapping zones on one layer: keep the max cost per cell
    order = np.lexsort((out[:, 3], out[:, 2], out[:, 1], out[:, 0]))
    out = out[order]
    keep = np.ones(len(out), dtype=bool)
    same = (np.diff(out[:, 0]) == 0) & (np.diff(out[:, 1]) == 0) & (np.diff(out[:, 2]) == 0)
    keep[:-1][same] = False  # lexsort put max cost last within a cell group
    return out[keep]


def register_plane_fragility(pcb_data: PCBData, config: GridRouteConfig,
                             track_proximity_cache: Dict) -> None:
    """Compute and register the field under the reserved cache key (no-op
    when disabled or no zones)."""
    cells = compute_plane_fragility_cells(pcb_data, config)
    if len(cells):
        track_proximity_cache[PLANE_FRAGILITY_CACHE_KEY] = cells
        n_zones = len({(z.layer, z.net_id) for z in pcb_data.zones})
        print(f"Plane fragility field: {len(cells)} cells near the edges of "
              f"{n_zones} pour(s) (KICAD_PLANE_FRAGILITY_COST="
              f"{fragility_cost_mm()}mm-equiv)")
