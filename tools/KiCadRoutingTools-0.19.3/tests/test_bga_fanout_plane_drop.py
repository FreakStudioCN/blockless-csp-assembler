#!/usr/bin/env python3
"""Regression test for the #424 plane-ball drop pass.

  python3 tests/test_bga_fanout_plane_drop.py

After the signal escape (any engine), every SMD ball on a plane net -- a net
the fanout skips as "taps its plane" -- gets a via NOW: a dog-bone gap via
where a gap site is free, else an exact-checked via-in-pad tap. The plane
poured later picks the via up at fill, killing the tap-behind-the-ball-wall
class (#360). This test runs the engine entry point on the dense ulx3s 22x22
BGA and checks:
  - default-on: plane balls get drop vias (gap + in-pad covers every ball),
  - the ledger is honest: drop nets never count as requested/escaped signals,
  - signal-escape copper is IDENTICAL with drops on vs off (the pass is
    strictly additive, after the signal phases),
  - plane_drop='off' and KICAD_FANOUT_PLANE_DROP=0 both disable it; the env
    knob also force-ENABLES over plane_drop='off' (the manifest A/B switch),
  - re-running against a board that already has the drops adds nothing
    (idempotence via the skipped_existing counter),
  - zone auto-detect: an excluded net BELOW the pad-count threshold is
    dropped when the board already owns a zone on that net,
  - drop vias are mutually drill/ring consistent and clear foreign pads.

Uses kicad_files/ulx3s.kicad_pcb; skips cleanly if absent.
"""
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from kicad_parser import parse_kicad_pcb, Segment, Via, Zone
import bga_fanout as bf
from bga_fanout import generate_bga_fanout, generate_plane_drops

BOARD = os.path.join(ROOT, "kicad_files", "ulx3s.kicad_pcb")
LAYERS = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
PARAMS = dict(track_width=0.12, clearance=0.1, via_size=0.35, via_drill=0.2,
              escape_method='underpad')
H2H = 0.2


def run(plane_drop):
    pcb = parse_kicad_pcb(BOARD)
    fp = pcb.footprints["U1"]
    tracks, vias, _vr, failed = generate_bga_fanout(
        fp, pcb, layers=LAYERS, plane_drop=plane_drop, **PARAMS)
    return pcb, fp, tracks, vias, failed, dict(bf.LAST_PLANE_DROP_REPORT)


def sig_key(tracks, plane_ids):
    return {(t['net_id'], t['start'], t['end'], t['layer'])
            for t in tracks if t['net_id'] not in plane_ids}


def main():
    print("=" * 60)
    print("#424 plane-ball drop pass regression test")
    print("=" * 60)
    if not os.path.exists(BOARD):
        print(f"  [SKIP] board not present: {BOARD}")
        return 0

    checks = []

    pcb, fp, tracks_on, vias_on, failed_on, rep = run('auto')
    # Plane nets by the engine's own no-filter classification: >= 6 balls.
    from collections import Counter
    counts = Counter(p.net_name for p in fp.pads if p.net_id)
    plane_names = {n for n, c in counts.items() if c >= 6}
    plane_ids = {p.net_id for p in fp.pads if p.net_name in plane_names}
    ball_of = {}      # plane SMD balls eligible for a drop
    board_counts = Counter()
    for f2 in pcb.footprints.values():
        for p in f2.pads:
            if p.net_id:
                board_counts[p.net_id] += 1
    eligible = [p for p in fp.pads
                if p.net_id in plane_ids and not (p.drill and p.drill > 0)
                and board_counts[p.net_id] >= 2]

    n_drop = rep.get('gap_vias', 0) + rep.get('pad_vias', 0)
    n_covered = n_drop + rep.get('skipped_existing', 0) + rep.get('failed', 0)
    checks.append((f"drops happened ({n_drop} vias for "
                   f"{sorted(rep.get('nets', {}).keys())})", n_drop > 0))
    checks.append((f"every eligible plane ball accounted for "
                   f"({n_covered}/{len(eligible)})",
                   n_covered == len(eligible)))
    drop_vias = [v for v in vias_on if v['net_id'] in plane_ids]
    checks.append((f"report matches emitted vias ({len(drop_vias)})",
                   len(drop_vias) == n_drop))

    # Off arm: no plane-net copper, and the SIGNAL escape is byte-identical.
    _pcb2, _fp2, tracks_off, vias_off, failed_off, rep_off = run('off')
    checks.append(("plane_drop='off' emits no plane vias and no report",
                   not [v for v in vias_off if v['net_id'] in plane_ids]
                   and rep_off == {}))
    checks.append(("signal copper identical on vs off (pass is additive)",
                   sig_key(tracks_on, plane_ids) == sig_key(tracks_off, plane_ids)
                   and failed_on == failed_off))

    # Env knob overrides in both directions (the manifest A/B switch).
    import env_knobs
    os.environ['KICAD_FANOUT_PLANE_DROP'] = '0'
    env_knobs.refresh()
    try:
        *_x, vias_env, _f, rep_env = run('auto')
    finally:
        os.environ['KICAD_FANOUT_PLANE_DROP'] = '1'
        env_knobs.refresh()
    try:
        *_y, vias_env_on, _f2, rep_env_on = run('off')
    finally:
        del os.environ['KICAD_FANOUT_PLANE_DROP']
        env_knobs.refresh()
    checks.append(("KICAD_FANOUT_PLANE_DROP=0 disables over plane_drop='auto'",
                   rep_env == {}))
    checks.append(("KICAD_FANOUT_PLANE_DROP=1 enables over plane_drop='off'",
                   (rep_env_on.get('gap_vias', 0)
                    + rep_env_on.get('pad_vias', 0)) > 0))

    # Idempotence: materialize the ON result into the board, re-run the drop
    # pass alone -- everything must be recognized as already connected.
    for t in tracks_on:
        pcb.segments.append(Segment(
            start_x=t['start'][0], start_y=t['start'][1],
            end_x=t['end'][0], end_y=t['end'][1],
            width=t['width'], layer=t['layer'], net_id=t['net_id']))
    for v in vias_on:
        pcb.vias.append(Via(x=v['x'], y=v['y'], size=v['size'],
                            drill=v['drill'], layers=v['layers'],
                            net_id=v['net_id']))
    _t3, v3, rep3 = generate_plane_drops(
        fp, pcb, LAYERS, track_width=0.12, clearance=0.1,
        via_size=0.35, via_drill=0.2, verbose=False)
    checks.append((f"re-run adds nothing ({len(v3)} new vias, "
                   f"{rep3.get('skipped_existing', 0)} recognized as served)",
                   len(v3) == 0 and rep3.get('skipped_existing', 0) == n_drop))

    # Zone auto-detect: exclude a small (< 6 ball) net; without a zone it is
    # not dropped, with a zone on that net it is.
    small = next((n for n, c in counts.items()
                  if 2 <= c < 6 and board_counts[
                      next(p.net_id for p in fp.pads if p.net_name == n)] >= 2),
                 None)
    if small is None:
        print("  [SKIP] no small multi-ball net for the zone check")
    else:
        small_id = next(p.net_id for p in fp.pads if p.net_name == small)
        pcb4 = parse_kicad_pcb(BOARD)
        fp4 = pcb4.footprints["U1"]
        filt = ['*', f'!{small}']
        _t, v_nozone, _r = generate_plane_drops(
            fp4, pcb4, LAYERS, track_width=0.12, clearance=0.1,
            via_size=0.35, via_drill=0.2, net_filter=filt, verbose=False)
        pcb4.zones.append(Zone(net_id=small_id, net_name=small, layer='In1.Cu',
                               polygon=[(0, 0), (1, 0), (1, 1)]))
        _t, v_zone, _r = generate_plane_drops(
            fp4, pcb4, LAYERS, track_width=0.12, clearance=0.1,
            via_size=0.35, via_drill=0.2, net_filter=filt, verbose=False)
        checks.append((f"zone auto-detect drops the small excluded net "
                       f"'{small}' only when its zone exists "
                       f"({len(v_nozone)} -> {len(v_zone)})",
                       len(v_nozone) == 0 and len(v_zone) > 0))

    # Geometry: drop vias mutually consistent + clear foreign ball pads.
    balls = [p for p in fp.pads if p.net_id]
    viol_pad = viol_via = 0
    for v in drop_vias:
        for p in balls:
            if p.net_id == v['net_id']:
                continue
            d = math.hypot(v['x'] - p.global_x, v['y'] - p.global_y)
            if d < v['size'] / 2 + max(p.size_x, p.size_y) / 2 + 0.1 - 1e-6:
                viol_pad += 1
    for i, a in enumerate(vias_on):
        for b in vias_on[i + 1:]:
            d = math.hypot(a['x'] - b['x'], a['y'] - b['y'])
            if d < a['drill'] / 2 + b['drill'] / 2 + H2H - 1e-6:
                viol_via += 1
            if a['net_id'] != b['net_id'] and \
                    d < a['size'] / 2 + b['size'] / 2 + 0.1 - 1e-6:
                viol_via += 1
    checks.append((f"drop vias clear foreign ball pads ({viol_pad} violations)",
                   viol_pad == 0))
    checks.append((f"all vias mutually consistent ({viol_via} violations)",
                   viol_via == 0))

    print()
    ok = True
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    print()
    print("ALL CHECKS PASSED" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
