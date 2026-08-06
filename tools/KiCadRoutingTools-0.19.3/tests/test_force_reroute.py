#!/usr/bin/env python3
"""--force-reroute end-to-end (#515's manual recipe automated, PR #533).

Drives the REAL CLI on the in-repo splitflap board:

1. route a net normally (baseline);
2. re-run naming it in --nets alone -> already-connected no-op (unchanged);
3. re-run with --force-reroute -> its copper is stripped and replanned from
   scratch, the output is connected, and the original copper is NOT shipped
   alongside the reroute (no stacked duplicates);
4. a PROTECTED net (recorded in the sibling .kicad_pro, #521) selected by a
   GLOB is skipped -- protection has no glob override;
5. the same net selected by EXACT name IS force-rerouted (the deliberate
   override);
6. --force-reroute without an explicit net scope is an argparse error (the
   default-'*' expansion must not rip the whole board).

    python3 tests/test_force_reroute.py
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BOARD = os.path.join(ROOT, 'kicad_files', 'splitflap_driver.kicad_pcb')
NET = '/LED_A'


def run_route(args, expect_ok=True):
    cmd = [sys.executable, os.path.join(ROOT, 'route.py')] + args
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    if expect_ok and r.returncode != 0:
        sys.stderr.write(r.stdout[-3000:] + r.stderr[-3000:])
        raise AssertionError(f"route.py failed: {' '.join(args)}")
    return r


def net_copper(board_path, net_name):
    from kicad_parser import parse_kicad_pcb
    pcb = parse_kicad_pcb(board_path)
    nid = next(i for i, n in pcb.nets.items() if n.name == net_name)
    segs = [s for s in pcb.segments if s.net_id == nid]
    vias = [v for v in pcb.vias if v.net_id == nid]
    return pcb, nid, segs, vias


def assert_connected(board_path, net_name):
    from check_connected import check_net_connectivity
    pcb, nid, segs, vias = net_copper(board_path, net_name)
    res = check_net_connectivity(nid, segs, vias, pcb.pads_by_net.get(nid, []))
    assert res.get('connected'), f"{net_name} disconnected in {board_path}"
    return segs, vias


def seg_spans(segs):
    def sig(s):
        a = (round(s.start_x, 3), round(s.start_y, 3))
        b = (round(s.end_x, 3), round(s.end_y, 3))
        return (min(a, b), max(a, b), s.layer)
    return [sig(s) for s in segs]


def main():
    tmp = tempfile.mkdtemp(prefix='force_reroute_')
    out1 = os.path.join(tmp, 'step1.kicad_pcb')
    print(f"workdir: {tmp}")

    # 1. Baseline: route the net.
    run_route([BOARD, out1, '--nets', NET])
    base_segs, _ = assert_connected(out1, NET)
    assert base_segs, "baseline routed no copper?"
    print(f"1. baseline routed: {len(base_segs)} segment(s)")

    # 2. Plain re-run: already-connected no-op.
    out2 = os.path.join(tmp, 'step2.kicad_pcb')
    r = run_route([out1, out2, '--nets', NET])
    assert 'already fully connected' in r.stdout, "expected the no-op path"
    segs2, _ = assert_connected(out2, NET)
    assert sorted(seg_spans(segs2)) == sorted(seg_spans(base_segs)), \
        "no-op re-run must not change the net's copper"
    print("2. plain re-run: no-op confirmed")

    # 3. Force re-route: stripped + replanned + connected + no stacked originals.
    out3 = os.path.join(tmp, 'step3.kicad_pcb')
    r = run_route([out1, out3, '--nets', NET, '--force-reroute'])
    assert '--force-reroute: stripped' in r.stdout, \
        "force-reroute did not strip the connected net"
    segs3, _ = assert_connected(out3, NET)
    # The replan must not ship the originals NEXT TO new copper: every original
    # span may appear at most once (a reroute reproducing a span replaces the
    # original, it does not stack on it).
    spans3 = seg_spans(segs3)
    assert len(spans3) == len(set(spans3)), \
        "force-reroute shipped duplicated same-span copper"
    print(f"3. force re-route: replanned to {len(segs3)} segment(s), connected")

    # 4./5. Protection (#521): record the net as protected in out1's project;
    # a GLOB selection must skip it, the EXACT name must override.
    pro1 = os.path.splitext(out1)[0] + '.kicad_pro'
    assert os.path.isfile(pro1), "route.py did not write the sibling .kicad_pro"
    with open(pro1) as f:
        proj = json.load(f)
    proj.setdefault('kicad_routing_tools', {})['protected_nets'] = \
        {NET: 'length-matched group'}
    with open(pro1, 'w') as f:
        json.dump(proj, f, indent=2)

    out4 = os.path.join(tmp, 'step4.kicad_pcb')
    r = run_route([out1, out4, '--nets', '/LED_*', '--force-reroute'])
    # /LED_A is the only /LED_* net with copper on out1, so a glob selection
    # must produce NO strip at all (the other LED nets route normally).
    assert '--force-reroute: stripped' not in r.stdout, \
        "glob must not override protection"
    segs4, _ = assert_connected(out4, NET)
    assert sorted(seg_spans(segs4)) == sorted(seg_spans(base_segs)), \
        "protected net selected by glob must keep its copper"
    print("4. protected + glob selection: skipped (copper unchanged)")

    out5 = os.path.join(tmp, 'step5.kicad_pcb')
    r = run_route([out1, out5, '--nets', NET, '--force-reroute'])
    assert '--force-reroute: stripped' in r.stdout, \
        "exact name must override protection"
    assert_connected(out5, NET)
    print("5. protected + exact name: override rips and re-routes")

    # 6. No explicit scope -> argparse error, never a whole-board rip.
    out6 = os.path.join(tmp, 'step6.kicad_pcb')
    r = run_route([out1, out6, '--force-reroute'], expect_ok=False)
    assert r.returncode != 0 and 'explicit net scope' in (r.stderr + r.stdout), \
        "--force-reroute without --nets must be rejected"
    print("6. missing net scope: rejected")

    print("OK: --force-reroute end-to-end")


if __name__ == '__main__':
    main()
