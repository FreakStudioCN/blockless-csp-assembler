#!/usr/bin/env python3
"""Pad global coordinates must sit on KiCad's integer-nanometre grid.

Everything else in PCBData is read verbatim from the board file -- segments,
vias, footprint origins -- so the text parser and pcbnew agree on those
bit-for-bit by construction. Pad globals are the ONE value that has to be
COMPUTED (footprint origin + rotated local offset), and the two front-ends
compute it in different number systems:

    pcbnew (GUI path)      composes in integer nm, then converts once
    parse_kicad_pcb (CLI)  composes in float mm

Left alone, the two answers land up to ~0.3 nm apart -- geometrically nil, but
DIFFERENT BIT PATTERNS, and that is enough to flip an A* tie-break. That is how
eth_tap step 11 diverged: 64 of 1699 pads came out a few femtometres off
(44.300000000000004 vs 44.3), the GUI then routed 16 segments differently from
the CLI, and by the end of the chain it was 3569 segments vs 3472.

local_to_global snaps to the nm grid so both fronts land on the same value.
This test pins that, without needing pcbnew: a coordinate on the nm grid
survives a round-trip through mm_to_iu exactly, and an off-grid one does not.

Same family as #493 (math.fsum, mm_to_iu): canonicalise to integer nm rather
than trusting float arithmetic in two implementations to agree.

Uses only checked-in boards.
"""
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT_DIR)

from kicad_parser import (parse_kicad_pcb, local_to_global, mm_to_iu,
                          IU_PER_MM)

failures = []


def on_grid(v):
    """True iff v is exactly representable as an integer number of nm."""
    return mm_to_iu(v) / IU_PER_MM == v


def test_local_to_global_snaps():
    """The transform itself must return grid-exact values, including for
    rotations that produce irrational offsets."""
    print("1. local_to_global returns nm-grid coordinates")
    cases = [
        # (fp_x, fp_y, rot, local_x, local_y)
        (44.0, 41.0, 0.0, 0.3, 0.035),
        (44.0, 41.0, 90.0, 0.3, 0.035),
        (44.0, 41.0, 45.0, 0.3, 0.035),     # irrational sin/cos
        (100.7, 62.35, 33.7, 1.27, -0.635),  # arbitrary angle
        (21.0, 21.0, 180.0, 0.65, 0.65),
        (0.0, 0.0, 270.0, 1.0 / 3.0, 2.0 / 3.0),
    ]
    bad = 0
    for fx, fy, rot, lx, ly in cases:
        gx, gy = local_to_global(fx, fy, rot, lx, ly)
        for axis, v in (('x', gx), ('y', gy)):
            if not on_grid(v):
                failures.append(f"local_to_global({fx},{fy},{rot},{lx},{ly}) "
                                f"{axis}={v!r} is off the nm grid")
                print(f"  FAIL rot={rot} {axis}={v!r} off-grid")
                bad += 1
    if not bad:
        print(f"  ok   all {len(cases)} cases land on the nm grid")


def test_repo_board_pads_on_grid():
    """Every pad of every checked-in board must be grid-exact."""
    print("2. pad globals on checked-in boards are nm-grid exact")
    # All TRACKED, so none of these can silently skip on a fresh clone.
    # interf_u_connected was in this list and is neither tracked nor cheap to
    # produce (it needs a full board route); interf_u_unrouted covers the same
    # footprints. fanout_starting_point IS worth keeping -- a fanned-out board
    # exercises generated copper rather than only hand-placed pads -- so it is
    # built on demand from its tracked root instead of skipped.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from fixture_boards import ensure
    boards = [
        'kicad_files/interf_u_unrouted.kicad_pcb',
        'kicad_files/glasgow_revC.kicad_pcb',
        'kicad_files/tigard.kicad_pcb',
        'kicad_files/splitflap_driver.kicad_pcb',
    ]
    paths = [os.path.join(ROOT_DIR, rel) for rel in boards]
    paths.append(ensure('fanout_starting_point.kicad_pcb'))
    checked = 0
    for path in paths:
        rel = os.path.relpath(path, ROOT_DIR)
        if not os.path.exists(path):
            raise AssertionError(
                f"{rel} is supposed to be tracked in git but is missing")
        pcb = parse_kicad_pcb(path)
        off = []
        npads = 0
        for ref, fp in pcb.footprints.items():
            for p in fp.pads:
                npads += 1
                if not on_grid(p.global_x):
                    off.append((ref, p.pad_number, 'x', p.global_x))
                if not on_grid(p.global_y):
                    off.append((ref, p.pad_number, 'y', p.global_y))
        checked += 1
        if off:
            failures.append(f"{rel}: {len(off)} off-grid pad coords "
                            f"(e.g. {off[0]})")
            print(f"  FAIL {os.path.basename(rel)}: {len(off)}/{npads*2} "
                  f"off-grid, e.g. {off[0]}")
        else:
            print(f"  ok   {os.path.basename(rel)}: {npads} pads all on grid")
    if not checked:
        print("  SKIP no boards available")


def test_rotation_does_not_reintroduce_drift():
    """A rotated footprint is the case that actually broke: assert a pad at a
    rotation whose float math is inexact still lands on the grid."""
    print("3. rotated placements stay on the grid")
    # 0.3 * cos(-33.7deg) etc. is irrational; without the snap these are
    # off-grid with probability ~1.
    bad = 0
    for rot in (5.0, 12.5, 33.7, 47.3, 66.6, 123.4, 271.9, 359.1):
        gx, gy = local_to_global(37.5, 22.25, rot, 0.7, -0.45)
        if not (on_grid(gx) and on_grid(gy)):
            failures.append(f"rot={rot}: ({gx!r}, {gy!r}) off-grid")
            print(f"  FAIL rot={rot}")
            bad += 1
    if not bad:
        print("  ok   8 arbitrary rotations all land on the nm grid")


def main():
    for t in (test_local_to_global_snaps,
              test_repo_board_pads_on_grid,
              test_rotation_does_not_reintroduce_drift):
        t()
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print("  " + f)
        return 1
    print("PASS: pad globals are canonicalised to KiCad's integer-nm grid")
    return 0


if __name__ == '__main__':
    sys.exit(main())
