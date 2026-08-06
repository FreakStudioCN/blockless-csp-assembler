#!/usr/bin/env python3
"""#529 dynamic iterations: tranche-earned budget extension in GridSearch.

route_multi / route_with_frontier grew an optional max_iterations_ceiling
(default 0 = off). When set above max_iterations, hitting the cap grants
+1x base tranches while the search's closest approach (best_h = min f-g over
expanded nodes) improves by a quantum per tranche.

Covers:
  1. Serpentine maze, base cap too small: without a ceiling the search fails
     at the cap; with a ceiling it extends past the base and finds the path.
  2. Determinism: two extended runs return identical paths and iterations.
  3. A/A: omitting the kwarg and passing 0 are byte-identical (feature off).
  4. Sealed target (flood, no progress): the grant rule denies extension --
     iterations stay at the base cap instead of riding to the ceiling.
  5. route_multi stats expose best_h and iteration_tranches.
  6. Python-side gate: probe-scale budgets (<= 10k, the fast-fail retry
     configs cloned from max_probe_iterations) never receive a ceiling;
     with the knob on, bases above 200k clamp to 200k with a flat 1e7
     ceiling; with the knob off, bases pass through untouched.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, 'rust_router'))

import numpy as np

from grid_router import GridObstacleMap, GridRouter


def _block(obs, cells):
    obs.add_blocked_cells_batch(np.asarray(cells, dtype=np.int32))


def build_serpentine(width=60, walls=20):
    """One-layer serpentine corridor: wall rows with alternating end gaps,
    enclosed by a boundary so the only path snakes through every row."""
    obs = GridObstacleMap(1)
    height = walls * 2 + 1  # corridor rows at odd y offsets between walls
    blocked = []
    # boundary box: x=0 / x=width+1 columns, y=0 / y=height+1 rows
    for x in range(0, width + 2):
        blocked.append((x, 0, 0))
        blocked.append((x, height + 1, 0))
    for y in range(0, height + 2):
        blocked.append((0, y, 0))
        blocked.append((width + 1, y, 0))
    # wall rows at even y >= 2 with a gap alternating right / left
    wall_i = 0
    for y in range(2, height + 1, 2):
        gap_x = width if wall_i % 2 == 0 else 1
        for x in range(1, width + 1):
            if x != gap_x:
                blocked.append((x, y, 0))
        wall_i += 1
    _block(obs, blocked)
    source = (1, 1, 0)
    target = (1, height, 0) if wall_i % 2 == 0 else (width, height, 0)
    return obs, [source], [target]


def build_sealed_target(size=200, box=3):
    """One-layer open field with the target sealed inside a solid box: the
    search floods to the box wall, then makes no further approach."""
    obs = GridObstacleMap(1)
    blocked = []
    for x in range(0, size + 1):
        blocked.append((x, 0, 0))
        blocked.append((x, size, 0))
    for y in range(0, size + 1):
        blocked.append((0, y, 0))
        blocked.append((size, y, 0))
    cx = cy = size // 2
    for x in range(cx - box, cx + box + 1):
        for y in range(cy - box, cy + box + 1):
            if abs(x - cx) == box or abs(y - cy) == box:
                blocked.append((x, y, 0))
    _block(obs, blocked)
    return obs, [(3, 3, 0)], [(cx, cy, 0)]


def main():
    failures = []
    router = GridRouter(500, 1.9, layer_costs=[1000])

    # --- 1. serpentine: fails at base cap, succeeds with ceiling ------------
    obs, src, tgt = build_serpentine()
    base = 300
    ceiling = 50 * base

    path0, iters0, _blocked0 = router.route_with_frontier(obs, src, tgt, base)
    if path0 is not None:
        failures.append('maze too easy: base cap %d found a path' % base)
    if iters0 != base:
        failures.append('capped search used %d iterations, expected %d' % (iters0, base))

    path1, iters1, _b1 = router.route_with_frontier(
        obs, src, tgt, base, max_iterations_ceiling=ceiling)
    if path1 is None:
        failures.append('ceiling %d did not rescue the maze search' % ceiling)
    elif iters1 <= base:
        failures.append('extended search finished within base cap (%d)' % iters1)
    if path1 is not None and iters1 > ceiling:
        failures.append('iterations %d exceeded the ceiling %d' % (iters1, ceiling))

    # --- 2. determinism ------------------------------------------------------
    path2, iters2, _b2 = router.route_with_frontier(
        obs, src, tgt, base, max_iterations_ceiling=ceiling)
    if (path1, iters1) != (path2, iters2):
        failures.append('extended search is not deterministic: %s/%s vs %s/%s'
                        % (len(path1 or []), iters1, len(path2 or []), iters2))

    # --- 3. A/A: kwarg omitted == kwarg 0 ------------------------------------
    path3, iters3, blocked3 = router.route_with_frontier(
        obs, src, tgt, base, max_iterations_ceiling=0)
    if (path0, iters0) != (path3, iters3):
        failures.append('max_iterations_ceiling=0 is not a no-op')

    # --- 4. sealed target: no progress -> no tranches ------------------------
    obs4, src4, tgt4 = build_sealed_target()
    base4 = 3000
    path4, iters4, _b4 = router.route_with_frontier(
        obs4, src4, tgt4, base4, max_iterations_ceiling=50 * base4)
    if path4 is not None:
        failures.append('sealed target was somehow reached')
    if iters4 > 2 * base4:
        failures.append('flood earned tranches without approach: %d iterations '
                        '(base %d)' % (iters4, base4))

    # --- 4b. plateau grace: flood earns exactly +grace tranches --------------
    for grace, want in ((2, 3 * base4), (5, 6 * base4)):
        _p, it_g, _b = router.route_with_frontier(
            obs4, src4, tgt4, base4, max_iterations_ceiling=50 * base4,
            grace_tranches=grace)
        if it_g != want:
            failures.append('grace=%d flood ran %d iters, expected %d'
                            % (grace, it_g, want))

    # --- 5. stats: best_h + iteration_tranches -------------------------------
    _p5, _i5, stats = router.route_multi(
        obs, src, tgt, base, max_iterations_ceiling=ceiling)
    if 'best_h' not in stats or 'iteration_tranches' not in stats:
        failures.append('stats missing best_h / iteration_tranches: %s'
                        % sorted(stats))
    else:
        if stats['iteration_tranches'] < 1:
            failures.append('extended success reported no tranches granted')
        if not (0 <= stats['best_h'] <= stats['initial_h']):
            failures.append('best_h %.0f outside [0, initial_h %.0f]'
                            % (stats['best_h'], stats['initial_h']))

    # --- 6. python gate: DEFAULT-ON (no env), probe budgets never extend -----
    for var in ('KICAD_DYNAMIC_ITERATIONS', 'KICAD_DYNAMIC_ITERATIONS_CLAMP'):
        os.environ.pop(var, None)
    import env_knobs
    env_knobs.refresh()
    import single_ended_routing as ser

    class _Cfg:
        max_iterations = 0

    # default config: ON, NO clamp — (base, expected effective, extension?)
    for b, want_base, want_ext in ((5000, 5000, False), (10000, 10000, False),
                                   (100000, 100000, True),
                                   (1000000, 1000000, True)):
        _Cfg.max_iterations = b
        eff, kw = ser._dynamic_iterations(_Cfg())
        if eff != want_base or bool(kw) != want_ext:
            failures.append('default base %d -> (%d, %s), expected (%d, ext=%s)'
                            % (b, eff, kw, want_base, want_ext))
        elif kw and kw.get('max_iterations_ceiling') != 10_000_000:
            failures.append('base %d ceiling %s, expected flat 1e7' % (b, kw))

    # speed dial: CLAMP=200000 caps the base
    os.environ['KICAD_DYNAMIC_ITERATIONS_CLAMP'] = '200000'
    env_knobs.refresh()
    for b, want_base in ((500000, 200000), (1000000, 200000), (100000, 100000)):
        _Cfg.max_iterations = b
        eff, kw = ser._dynamic_iterations(_Cfg())
        if eff != want_base or not kw:
            failures.append('clamped base %d -> (%d, %s), expected %d'
                            % (b, eff, kw, want_base))
    os.environ.pop('KICAD_DYNAMIC_ITERATIONS_CLAMP', None)

    # kill-switch: =0 restores static caps exactly (base untouched, no kwargs)
    os.environ['KICAD_DYNAMIC_ITERATIONS'] = '0'
    env_knobs.refresh()
    _Cfg.max_iterations = 1_000_000
    eff, kw = ser._dynamic_iterations(_Cfg())
    if eff != 1_000_000 or kw:
        failures.append('kill-switch 1e6 must pass through, got (%d, %s)' % (eff, kw))
    os.environ.pop('KICAD_DYNAMIC_ITERATIONS', None)
    env_knobs.refresh()

    if failures:
        for f in failures:
            print('FAIL: %s' % f)
        return 1
    print('PASS: capped fail at %d iters; extended success in %d iters '
          '(%d tranches, best_h %.0f); flood denied at %d iters'
          % (iters0, iters1, stats['iteration_tranches'], stats['best_h'], iters4))
    return 0


if __name__ == '__main__':
    sys.exit(main())
