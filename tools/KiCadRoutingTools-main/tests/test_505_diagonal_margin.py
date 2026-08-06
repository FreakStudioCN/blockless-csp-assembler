#!/usr/bin/env python3
"""Regression test for issue #505 -- the #156 A* track margin was derived from
AXIS-ALIGNED grid rows only, so it under-rejected on the router's other movement
axis (45 degrees).

Obstacle stamps reserve the NOMINAL track width; a net routing WIDER (a power
override, an impedance layer width) covers its extra half-width with a
fractional A* `track_margin` measured from the outermost BLOCKED CELL. Rows on
an axis sit 1 cell apart, but parallel 45-degree tracks sit on rows 1/sqrt(2)
cells apart -- so the step from the stamp shell to the next diagonal row is
sqrt(2) = 1.414 cells. A margin below that rejects NOTHING diagonally, and the
next diagonal row lands inside the true keep-out radius.

Measured on the corpus (wave ab_main_0726a, re-graded with kicad-cli):

  polykit_x      grid 0.1  clr 0.2  tw 0.25  power 0.5 -> margin 1.25
                 diagonal opened at 0.565685mm vs a 0.575 requirement
                 (kicad-cli: "Clearance violation, actual 0.1907 vs 0.2"), x39
  caravel_nucleo grid 0.05 clr 0.1  tw 0.13  peers 0.3 -> margin 2.0
                 diagonal opened at 0.388909mm vs a 0.400 requirement
                 (kicad-cli: "actual 0.0889 vs 0.1"), x7

Both are invisible to check_drc, whose clearance_margin defaults to 5% of
clearance (10um at a 0.2 floor) -- the shortfalls are 9.3um and 11.1um.

The fix snaps the margin up to the next DIAGONAL row. That is free on the
orthogonal axis (the axis corridor stays in the same integer row), unlike the
blunt +1 cell which opens the axis corridor a whole cell late.

    python3 tests/test_505_diagonal_margin.py
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routing_config import GridRouteConfig  # noqa: E402

SQRT2 = math.sqrt(2.0)
fails = []


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        fails.append(name)


def stamp_radius(cfg, w_obs, reserve, clr):
    """Stamp keep-out radius in grid cells (obstacle_map's expansion_mm)."""
    return (w_obs / 2.0 + clr + reserve / 2.0) / cfg.grid_step


def diagonal_open_cells(r_stamp, margin):
    """First 45-degree row (in cells from the obstacle centreline) the A* still
    accepts: diagonal rows sit at k/sqrt(2), blocked while k/sqrt(2) < r_stamp,
    and a move is rejected while its distance to the shell is <= margin."""
    k_shell = math.ceil(r_stamp * SQRT2 - 1e-9) - 1
    for k in range(k_shell + 1, 4000):
        if (k - k_shell) / SQRT2 > margin + 1e-12:
            return k / SQRT2
    raise AssertionError("no open diagonal row")


def orthogonal_open_cells(r_stamp, margin):
    """First axis-aligned row the A* still accepts."""
    shell = math.ceil(r_stamp - 1e-9) - 1
    for n in range(shell + 1, 4000):
        if (n - shell) > margin + 1e-12:
            return float(n)
    raise AssertionError("no open orthogonal row")


# --------------------------------------------------------------------------
# 1. The two boards that actually shipped the violation
# --------------------------------------------------------------------------
BOARDS = [
    # label, grid, clearance, track_width, net_width, obstacle_width, required mm
    ("polykit_x", 0.1, 0.2, 0.25, 0.5, 0.25, 0.575),
    ("caravel_nucleo", 0.05, 0.1, 0.13, 0.3, 0.3, 0.4),
]

for label, grid, clr, tw, net_w, obs_w, req_mm in BOARDS:
    cfg = GridRouteConfig(layers=["F.Cu", "B.Cu"], grid_step=grid,
                          clearance=clr, track_width=tw)
    margin = cfg._phase_exact_margin("F.Cu", net_w)
    reserve = cfg.route_reserve_width("F.Cu")
    r_stamp = stamp_radius(cfg, obs_w, reserve, clr)
    got_mm = diagonal_open_cells(r_stamp, margin) * grid
    check(f"{label}: diagonal clears the requirement "
          f"({got_mm:.6f}mm >= {req_mm}mm)", got_mm >= req_mm - 1e-9)

# The exact pre-fix values, pinned so a regression is unambiguous.
_cfg = GridRouteConfig(layers=["F.Cu", "B.Cu"], grid_step=0.1,
                       clearance=0.2, track_width=0.25)
check("polykit_x: margin raised off the slipping 1.25",
      _cfg._phase_exact_margin("F.Cu", 0.5) > 1.25 + 1e-9)
_cfg2 = GridRouteConfig(layers=["F.Cu", "B.Cu"], grid_step=0.05,
                        clearance=0.1, track_width=0.13)
check("caravel_nucleo: margin raised off the slipping 2.0",
      _cfg2._phase_exact_margin("F.Cu", 0.3) > 2.0 + 1e-9)


# --------------------------------------------------------------------------
# 2. The margin must land ON a lattice distance. Anything strictly between two
#    of them rejects exactly what the lower one rejects, so a "nearly big
#    enough" margin is worth nothing -- the original 1.25 on polykit_x behaved
#    identically to 1.0 and the board re-routed byte-for-byte unchanged.
# --------------------------------------------------------------------------
from routing_config import _LATTICE_REACH  # noqa: E402

for label, grid, clr, tw, net_w, obs_w, _req in BOARDS:
    cfg = GridRouteConfig(layers=["F.Cu", "B.Cu"], grid_step=grid,
                          clearance=clr, track_width=tw)
    m = cfg._phase_exact_margin("F.Cu", net_w)
    on_lattice = any(abs(m - r) <= 1e-6 * max(1.0, r) for r in _LATTICE_REACH)
    check(f"{label}: margin sits on a lattice distance ({m:.6f})", on_lattice)

# A margin that lands one ULP BELOW the distance it must reject is inert: the
# router blocks on d <= margin with d computed from integer offsets, so
# 2/sqrt(2) -- which floats one ULP under sqrt(2) -- fails to reject the very
# sqrt(2) row it targets. tw 0.25 + net 0.40 at grid 0.1 gives e=0.75, whose
# worst-case reach e*sqrt(2)=1.06 snaps to exactly the sqrt(2) lattice row.
_m = GridRouteConfig(layers=["F.Cu"], grid_step=0.1, clearance=0.2,
                     track_width=0.25)._phase_exact_margin("F.Cu", 0.40)
check(f"lattice snap clears sqrt(2) rather than landing under it ({_m!r})",
      _m >= SQRT2)


# --------------------------------------------------------------------------
# 3. Invariants across a broad parameter sweep: the margin still covers the
#    bare extra half-width `e`, reaches the worst-case e*sqrt(2) needed on the
#    45-degree axis, and never lets a diagonal row land inside the requirement.
# --------------------------------------------------------------------------
bad_lower = bad_reach = bad_diag = 0
sweeps = 0
for grid in (0.05, 0.1, 0.127):
    for clr in (0.1, 0.15, 0.2):
        for tw in (0.13, 0.2, 0.25):
            for net_w in (tw, tw + 0.05, tw + 0.15, tw + 0.25, tw + 0.4):
                cfg = GridRouteConfig(layers=["F.Cu", "B.Cu"], grid_step=grid,
                                      clearance=clr, track_width=tw)
                reserve = cfg.route_reserve_width("F.Cu")
                e = (net_w - reserve) / 2.0 / grid
                m = cfg._phase_exact_margin("F.Cu", net_w)
                if e <= 1e-9:
                    if m != 0.0:
                        bad_lower += 1
                    continue
                sweeps += 1
                if m < e - 1e-9:
                    bad_lower += 1
                if m < e * SQRT2 - 1e-9:
                    bad_reach += 1
                # every obstacle class the margin is supposed to cover
                for w_obs in (reserve, cfg.get_track_width("F.Cu"), net_w):
                    r_s = stamp_radius(cfg, w_obs, reserve, clr)
                    r_need = r_s + e
                    if diagonal_open_cells(r_s, m) < r_need - 1e-9:
                        bad_diag += 1

check(f"sweep ({sweeps} configs): margin always >= e", bad_lower == 0)
check(f"sweep ({sweeps} configs): margin reaches the 45-degree worst case "
      f"e*sqrt(2)", bad_reach == 0)
check(f"sweep ({sweeps} configs): no diagonal row inside the requirement",
      bad_diag == 0)


# --------------------------------------------------------------------------
# 4. Ground truth: the minimal safe margin brute-forced over the lattice
#    (obstacle orientation x sub-cell phase x line/capsule-cap), sampled at the
#    breakpoints where it steps. The snap must never fall BELOW these -- an
#    undershoot is a real clearance violation, whereas overshoot only costs
#    some routability. Regenerate with the sweep in this file's history if the
#    reach model changes.
# --------------------------------------------------------------------------
GROUND_TRUTH = [
    (0.0625, 1.0),
    (0.75, math.sqrt(2.0)),
    (1.0, 2.0),
    (1.4375, math.sqrt(5.0)),
    (1.9375, 3.0),
    (2.6875, math.sqrt(10.0)),
    (2.875, 4.0),
]
from routing_config import _snap_to_lattice_reach  # noqa: E402

under = [(e, need, _snap_to_lattice_reach(e, e))
         for e, need in GROUND_TRUTH
         if _snap_to_lattice_reach(e, e) < need - 1e-9]
check(f"brute-forced ground truth ({len(GROUND_TRUTH)} breakpoints): "
      f"snap never undershoots", not under)
if under:
    for e, need, got in under:
        print(f"        e={e}: need {need:.6f}, got {got:.6f}")

# The reach set must exclude lattice distances that can never be the NEAREST
# blocked cell (2*sqrt(2), sqrt(13)); including them lets the snap stop one
# family member short -- that is exactly what undershot at e=1.9375.
from routing_config import _LATTICE_REACH  # noqa: E402
check("reach set excludes non-nearest lattice distances (2sqrt2, sqrt13)",
      not any(abs(r - 2 * SQRT2) < 1e-9 or abs(r - math.sqrt(13.0)) < 1e-9
              for r in _LATTICE_REACH))

print("-" * 60)
if fails:
    print(f"{len(fails)} FAILED: " + "; ".join(fails))
    sys.exit(1)
print("ALL PASS")
