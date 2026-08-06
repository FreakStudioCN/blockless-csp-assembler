#!/usr/bin/env python3
"""Run-7 E3: stacked duplicate vias must be deduped, and what survives must
be DISCLOSED.

Failed/partial attempts could leave a result emitting a via at the exact
position/span of a surviving input via (or another result's via for the same
net): two barrels in one hole. KiCad's DRC permits same-net stacks, so nothing
flagged them -- the stacks shipped and read as parser/net rot when the raw
file was inspected (run-7 spent a scare on exactly that).

Contract under test:
  * check_weird.stacked_copper_over_model reports coincident same-net vias
    and duplicate same-net segments over a per-net write model;
  * route.py runs a same-net via dedup BEFORE total_vias / the authoritative
    sweep (so tally, sweep, writer and GUI results agree) and exports
    summary['stacked_copper'] for what remains;
  * a clean run emits no stacked_copper key.
"""
import inspect
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from kicad_parser import Segment, Via  # noqa: E402


def _via(x, y, net_id):
    return Via(x=x, y=y, size=0.6, drill=0.3, layers=['F.Cu', 'B.Cu'],
               net_id=net_id)


def _seg(x1, y1, x2, y2, net_id):
    return Segment(start_x=x1, start_y=y1, end_x=x2, end_y=y2,
                   width=0.2, layer='F.Cu', net_id=net_id)


def test_model_check_reports_stacked_vias_and_segments():
    from check_weird import stacked_copper_over_model
    segs = {1: [_seg(0, 0, 5, 0, 1), _seg(0, 0, 5, 0, 1)]}   # exact duplicate
    vias = {1: [_via(2, 2, 1), _via(2.005, 2, 1)],           # 5um apart: stacked
            2: [_via(10, 10, 2)]}                            # lone via: clean
    found = stacked_copper_over_model(segs, vias, lambda n: f'N{n}')
    cats = sorted(f['category'] for f in found)
    assert cats == ['stacked-copper', 'stacked-copper'], (
        f"expected one via stack + one segment stack, got {found}")
    nets = {f['net'] for f in found}
    assert nets == {'N1'}, f"the lone via on N2 must not be flagged: {nets}"
    print("  PASS: model check reports stacked vias + duplicate segments")


def test_far_apart_vias_are_not_stacked():
    from check_weird import stacked_copper_over_model
    vias = {1: [_via(2, 2, 1), _via(2.5, 2, 1)]}  # 0.5mm apart: a real pair
    found = stacked_copper_over_model({}, vias, lambda n: f'N{n}')
    assert found == [], f"0.5mm-apart same-net vias are legitimate: {found}"
    print("  PASS: separated same-net vias stay unflagged")


def test_route_py_dedups_before_tally_and_sweep():
    import route
    src = inspect.getsource(route.batch_route)
    i_dedup = src.find('VIA DEDUP')
    i_tally = src.find('total_vias = sum(')
    i_sweep = src.find('_sweep_strip_via_ids =')
    i_stacked = src.find('STACKED-COPPER SURFACING')
    assert -1 not in (i_dedup, i_tally, i_sweep, i_stacked), (
        "dedup / tally / sweep / surfacing blocks not all present")
    assert i_dedup < i_tally < i_sweep < i_stacked, (
        "the dedup must run BEFORE the via tally and the authoritative sweep "
        "so tally, sweep, writer and GUI results all see the deduped copper")
    assert re.search(r"net_id.*layers", src[i_dedup:i_tally], re.S), (
        "the dedup key must be same-net and same-span -- cross-net "
        "coincidence is a real short the DRC sweep owns, never dedup it")
    assert "summary['stacked_copper']" in src
    print("  PASS: route.py dedups before tally/sweep and exports the key")


def test_end_to_end_clean_run_has_no_stacked_key():
    board = os.path.join(ROOT, 'kicad_files', 'splitflap_driver.kicad_pcb')
    if not os.path.isfile(board):
        print("  SKIP: fixture missing")
        return
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, 's.kicad_pcb')
        # Read the summary off the run's own JSON_SUMMARY stdout line: every
        # route.py emits it, while the --json-out file flag does not exist on
        # every branch. Same data, one interface older.
        r = subprocess.run([sys.executable, '-X', 'utf8',
                            os.path.join(ROOT, 'route.py'), board, out,
                            '--nets', 'GND'],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', cwd=ROOT)
        assert r.returncode == 0, r.stdout[-2000:]
        lines = [l for l in r.stdout.splitlines()
                 if l.startswith('JSON_SUMMARY: ')]
        assert lines, f"no JSON_SUMMARY line in the run output: {r.stdout[-2000:]}"
        summary = json.loads(lines[-1][len('JSON_SUMMARY: '):])
        assert 'stacked_copper' not in summary, (
            f"clean run reported stacks: {summary.get('stacked_copper')}")
    print("  PASS: clean run ships no stacked_copper key")


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in fns:
        print(f"--- {fn.__name__}")
        fn()
    print("ALL PASS")
