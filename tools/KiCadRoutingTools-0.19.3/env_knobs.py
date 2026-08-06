"""Read-once cache of the KICAD_* environment knobs.

The engine's experimental/diagnostic knobs were read with ``os.environ.get``
at their point of use -- some per net, per leg, or per congestion sample --
which both hammers the environ dict from hot paths and scatters the knob
inventory across a dozen modules. Every knob is read ONCE here, at import,
and stored as a plain module attribute; call sites read the attribute.

Semantics: a knob is its value at process start (first import). Harnesses that
pass knobs to CHILD processes via the environment are unaffected -- the child
re-reads at its own import (see memory: env vars must be EXPORTED for
subprocesses, an inline prefix misses children). Code that mutates
``os.environ`` mid-process and needs THIS process to see it calls
``refresh()`` afterwards.

Each attribute preserves the exact parse semantics its call site had (truthy
vs '1' vs default-on off-switch vs opt-in set) -- flipping one to a stricter
parse silently disables someone's recorded workflow. Argparse ``default=os.
environ.get(...)`` sites (route.py --ripup-* ) deliberately stay in place:
they are startup-once and self-document the override in --help.
"""
from __future__ import annotations

import os

_OFF = ('0', 'off', 'false')
_ON = ('1', 'true', 'on')


def _truthy(name: str) -> bool:
    return bool(os.environ.get(name))


def _on_default(name: str, default: str = '1') -> bool:
    return os.environ.get(name, default) not in _OFF


def _opt_in(name: str) -> bool:
    return os.environ.get(name, '') in _ON


def _f(name: str, dflt: float) -> float:
    try:
        return float(os.environ.get(name, str(dflt)) or dflt)
    except ValueError:
        return dflt


def _i(name: str, dflt: int) -> int:
    try:
        return int(os.environ.get(name, str(dflt)) or dflt)
    except ValueError:
        return dflt


def _s(name: str, dflt: str = '') -> str:
    return os.environ.get(name, dflt)


def refresh() -> None:
    """(Re)read every knob from os.environ (tests / in-process mutation)."""
    g = globals()

    # --- routing behavior switches (default ON, env turns OFF) -------------
    g['SEAM_REASK'] = _on_default('KICAD_SEAM_REASK', '')          # '' not in _OFF -> on
    g['ISLAND_LAUNCH'] = _on_default('KICAD_ISLAND_LAUNCH')
    g['MULTIPOINT_STUB_SWITCH'] = _on_default('KICAD_MULTIPOINT_STUB_SWITCH')
    g['HYBRID_COUPLE'] = _on_default('KICAD_HYBRID_COUPLE', '')
    g['BUS_MULTIPOINT_SPAN'] = _on_default('KICAD_BUS_MULTIPOINT_SPAN')
    g['BARE_BALL_ZONE_EXEMPT'] = _on_default('KICAD_BARE_BALL_ZONE_EXEMPT')
    g['DIRECT_FIRST'] = _on_default('KICAD_DIRECT_FIRST')
    g['IMPEDANCE_NECKDOWN'] = (_s('KICAD_IMPEDANCE_NECKDOWN', '1').strip().lower()
                               not in ('0', 'false', 'no', 'off'))
    g['NET_RESCUE'] = _s('KICAD_NET_RESCUE', '1') != '0'
    # #529 dynamic iterations, DEFAULT ON (=0 reverts to static caps): full
    # searches run at min(base, CLAMP) and earn +1x base tranches while the
    # heuristic keeps approaching, up to a flat 1e7 ceiling. CLAMP defaults
    # to 1e7 = no clamping (corpus: -29 incomplete nets over 150 boards);
    # set CLAMP=200000 as the deliberate speed-over-completion dial.
    g['DYNAMIC_ITERATIONS'] = _s('KICAD_DYNAMIC_ITERATIONS', '1') != '0'
    g['DYNAMIC_ITERATIONS_CLAMP'] = _i('KICAD_DYNAMIC_ITERATIONS_CLAMP', 10_000_000)

    # --- opt-in experiments (env turns ON) ----------------------------------
    g['COLLINEAR_VIAS'] = _opt_in('KICAD_COLLINEAR_VIAS')  # #487: on-axis vias
    # per-tranche quantum = max(CELLS grid cells, PCT% of tranche-start best_h)
    g['DYNAMIC_ITERATIONS_QUANTUM_CELLS'] = _f('KICAD_DYNAMIC_ITERATIONS_QUANTUM_CELLS', 2.0)
    g['DYNAMIC_ITERATIONS_QUANTUM_PCT'] = _f('KICAD_DYNAMIC_ITERATIONS_QUANTUM_PCT', 2.0)
    # plateau grace: consecutive quantum-failing tranches tolerated before
    # denial (progress judged cumulatively over the plateau; emulates a wider
    # window without a bigger base)
    g['DYNAMIC_ITERATIONS_GRACE'] = _i('KICAD_DYNAMIC_ITERATIONS_GRACE', 0)
    g['MULTIPOINT_DENSE_FIRST'] = _opt_in('KICAD_MULTIPOINT_DENSE_FIRST')
    g['FANOUT_DIRECT'] = _opt_in('KICAD_FANOUT_DIRECT')
    g['FANOUT_TOWARD_TARGETS'] = _opt_in('KICAD_FANOUT_TOWARD_TARGETS')
    # '' = follow the --plane-drop param (default auto); '0'/'off' forces the
    # plane-ball drop pass OFF, '1'/'auto' forces it ON -- the manifest-replay
    # A/B switch for #424 D2 (recorded chains carry no flag either way).
    g['FANOUT_PLANE_DROP'] = _s('KICAD_FANOUT_PLANE_DROP', '')
    g['STOP_CLEANUP'] = _opt_in('KICAD_STOP_CLEANUP')
    g['TAP_RELOCATION'] = _opt_in('KICAD_TAP_RELOCATION')  # phase-3 tap pocket moves
    g['PLANE_PARTIAL_RESTORE'] = _s('KICAD_PLANE_PARTIAL_RESTORE') == '1'
    g['DUMP_BATCH_KWARGS_CONTINUE'] = _s('KICAD_DUMP_BATCH_KWARGS_CONTINUE') == '1'

    # --- truthy diagnostics / overrides -------------------------------------
    g['UNBLOCK_DEBUG'] = _truthy('KICAD_UNBLOCK_DEBUG')
    g['TAP_CROSS_SCAN'] = _truthy('KICAD_TAP_CROSS_SCAN')
    g['OBSTACLE_AUDIT'] = _truthy('KICAD_OBSTACLE_AUDIT')
    g['PLANE_MAP_PARITY'] = _truthy('KICAD_PLANE_MAP_PARITY')
    g['SETTLE_DEBUG'] = _truthy('KICAD_SETTLE_DEBUG')
    g['LEGACY_GATE_ORACLE'] = _truthy('KICAD_LEGACY_GATE_ORACLE')
    g['NO_GATE_ORACLE'] = _truthy('KICAD_NO_GATE_ORACLE')
    g['GATE_DEBUG'] = _truthy('KICAD_GATE_DEBUG')
    g['NO_SWEEP_PLATED'] = _truthy('KICAD_NO_SWEEP_PLATED')
    g['NO_SOFT_JOINT_BRIDGE'] = _truthy('KICAD_NO_SOFT_JOINT_BRIDGE')
    g['BOARD_LEDGER'] = _truthy('KICAD_BOARD_LEDGER')
    # "x,y" via position string ('' = off; consumers test truthiness + parse)
    g['RESCUE_DEBUG_VIA'] = _s('KICAD_RESCUE_DEBUG_VIA')
    g['MEM_PROBE'] = _truthy('KICAD_MEM_PROBE')
    g['NO_STATIC_BASE'] = _truthy('KICAD_NO_STATIC_BASE')
    g['ALLOW_STAGGERED_BGA'] = _truthy('KICAD_ALLOW_STAGGERED_BGA')
    g['QFN_UNDERPAD_NO_ALT_STAGGER'] = _truthy('QFN_UNDERPAD_NO_ALT_STAGGER')
    g['LEGACY_ORACLE'] = _truthy('KICAD_LEGACY_ORACLE')
    g['NO_EXACT_FILL'] = _truthy('KICAD_NO_EXACT_FILL')
    g['NO_FILL_NETCLASS'] = _truthy('KICAD_NO_FILL_NETCLASS')  # fill-model A/B

    # --- numeric knobs -------------------------------------------------------
    g['BUS_XLAYER_PCT'] = _i('KICAD_BUS_XLAYER_PCT', 35)
    g['BUS_OFFLANE_MULT'] = _f('KICAD_BUS_OFFLANE_MULT', 1.0)   # off-lane surcharge (1.0 = off)
    g['BUS_CORRIDOR_PROBE_VIA_MULT'] = _f('KICAD_BUS_CORRIDOR_PROBE_VIA_MULT', 20.0)
    g['BUS_MAX_CORRIDOR_LAYER_CHANGES'] = _i('KICAD_BUS_MAX_CORRIDOR_LAYER_CHANGES', 1)
    g['TAP_RELOCATION_MAX'] = _i('KICAD_TAP_RELOCATION_MAX', 2)
    g['SEAM_SE_RATIO'] = _f('KICAD_SEAM_SE_RATIO', 1.3)
    g['SEAM_SE_MAX'] = _i('KICAD_SEAM_SE_MAX', 16)
    g['HYBRID_COUPLE_RADIUS'] = _f('KICAD_HYBRID_COUPLE_RADIUS', 1.0)
    g['BUS_RIP_RESISTANCE'] = _s('KICAD_BUS_RIP_RESISTANCE', '')  # float-or-empty; consumer parses
    g['CONGESTION_COST'] = _f('KICAD_CONGESTION_COST', 0.0)
    g['CONGESTION_BIN'] = _f('KICAD_CONGESTION_BIN', 1.0)
    g['CONGESTION_THRESHOLD'] = _f('KICAD_CONGESTION_THRESHOLD', 0.30)
    g['CONGESTION2'] = {
        'cost': _f('KICAD_CONGESTION2_COST', 0.0),
        'thresh': _f('KICAD_CONGESTION2_THRESHOLD', 0.5),
        'bin': _f('KICAD_CONGESTION2_BIN', 1.0),
        'exempt_r': _f('KICAD_CONGESTION2_EXEMPT_R', 1.0),
        'ramp_top': _f('KICAD_CONGESTION2_RAMP_TOP', 2.0),
    }

    # --- strings -------------------------------------------------------------
    g['HYBRID_LEG_DEBUG'] = _s('KICAD_HYBRID_LEG_DEBUG')   # truthy; '2' = verbose
    g['LEDGER_TRACE'] = _s('KICAD_LEDGER_TRACE')
    g['STOP_AFTER'] = _s('KICAD_STOP_AFTER')
    g['STOP_FILE'] = _s('KICAD_STOP_FILE')
    g['DUMP_BATCH_KWARGS'] = _s('KICAD_DUMP_BATCH_KWARGS')  # dump file path
    # raw override for underpad outer-ring count ('' = keep the caller's
    # default, which differs by escape method; consumer parses float/inf)
    g['UNDERPAD_OUTER_RINGS'] = _s('KICAD_UNDERPAD_OUTER_RINGS')


refresh()
