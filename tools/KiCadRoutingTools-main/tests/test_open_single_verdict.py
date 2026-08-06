#!/usr/bin/env python3
"""`open_single` -- routed-but-OPEN nets are COUNTED, not just named.

A net can keep a result while its copper leaves pads disconnected (a near-miss
stub). Before this key it landed in neither `routed_single` nor `failed_single`:
its pads showed up in `failed_multipoint` (names only), but every verdict that
counts failures -- converge.route_verdict, place_route_loop.metrics_from_summary
-- read `len(failed_single) + pad-deficit`, and a NON-multipoint open net
contributes to neither term. Probes read failures=0 on boards shipping open
copper (run-7: failed_single [] while the oracle listed six opens).

The contract under test:
  * route.py emits `open_single` (always present; multipoint nets excluded --
    their shortfall is already the pad deficit, so verdicts may add the terms).
  * the failure count that drives the place/route loop adds it.
  * Summaries WITHOUT the key (older logs) degrade to the old arithmetic.

Note: this branch computes that failure count inline in
`place_route_loop.run_route` rather than in an importable
`metrics_from_summary`/`converge.route_verdict` pair, so the consumer side is
asserted over the source rather than by calling it.
"""
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def test_failure_count_adds_open_single():
    """The place/route loop's failure count must include open_single, and a
    summary WITHOUT the key must degrade to the old arithmetic."""
    import place_route_loop
    src = inspect.getsource(place_route_loop).replace('\n', ' ')
    m = re.search(r"failures\s*=\s*\(?([^\n]{0,240}?mp_deficit)", src)
    assert m, "could not find the failures computation in place_route_loop"
    expr = m.group(1)
    assert 'open_single' in expr, (
        "the failure count ignores open_single, so a non-multipoint open net "
        f"still counts 0: {expr}")
    assert ("get('open_single', [])" in expr
            or 'get("open_single", [])' in expr), (
        "open_single must be read with a default so pre-key summaries degrade "
        f"to the old arithmetic instead of raising: {expr}")
    assert 'failed_single' in expr and 'mp_deficit' in expr, (
        f"the other two terms must survive: {expr}")
    print("  PASS: the loop's failure count adds open_single (and degrades)")


def test_route_py_classification_has_the_third_bucket():
    """Source-level: the scope loop must classify kept-result broken nets."""
    import route
    src = inspect.getsource(route.batch_route)
    m = re.search(r"for net_name, net_id in single_ended_nets:(.*?)\n\n",
                  src, re.S)
    assert m, "the summary classification loop moved -- update this test"
    loop = m.group(0)
    assert 'open_single.append' in loop, (
        "a net with a result but broken pads must land in open_single, "
        "not fall through unclassified")
    assert 'is_multipoint' in loop, (
        "multipoint nets must be excluded from open_single (their shortfall "
        "is already the pad deficit -- counting both double-charges)")
    assert "'open_single': open_single" in src, (
        "the summary dict must carry the key")
    print("  PASS: route.py classifies the routed-but-OPEN bucket")


def test_end_to_end_key_present_and_clean():
    """A real run emits the key (empty on a clean route)."""
    board = os.path.join(ROOT, 'kicad_files', 'splitflap_driver.kicad_pcb')
    if not os.path.isfile(board):
        print("  SKIP: fixture missing")
        return
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, 's.kicad_pcb')
        # The summary is read off the run's own JSON_SUMMARY stdout line --
        # every route.py emits it, whereas the --json-out file flag does not
        # exist on every branch. Same data, one interface older.
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
        assert 'open_single' in summary, (
            "open_single must always be present so verdict consumers can "
            "rely on it (absent = pre-key log, not 'clean')")
        assert summary['open_single'] == [], (
            f"clean GND route reported open nets: {summary['open_single']}")
    print("  PASS: real run emits open_single (empty when clean)")


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in fns:
        print(f"--- {fn.__name__}")
        fn()
    print("ALL PASS")
