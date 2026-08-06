#!/usr/bin/env python3
"""PR #432 follow-up: pad-pair routability tallies + open outcome in JSON_SUMMARY.

Scenario A (emit): the #409 walled-room board, max_rip_up_count=0. BLOCKER
routes (its one pad pair connects), X cannot cross the wall and fails with no
result. Asserts pad_pairs_connected/total == 1/2, exactly one pad_pairs_open
entry (X, 0/1, outcome 'open', subtype 'unrouted' -- scope net with no
result), the frozen entry key set, co-emission with 'blockers' (same net in
both, joinable on 'net'), and return_results parity.

Scenario B (subtype + omission): same board with default rip-up flips the
roles -- BLOCKER is ripped for X, its reroute fails, and the #134 restore
refusal (putting the track back would collide with X's fresh copper) makes
its subtype 'collision_refused', the "left open to avoid creating a short"
signal. A separate clean one-net board pins the omit-when-empty rule:
tallies present and equal, no pad_pairs_open key.

Scenario C (multipoint pair math): 4-pad net M with a pre-existing segment
joining its right pad pair, a full-height static WALL at x=5, one routing
layer. The component-MST has 2 edges (existing copper joins the right pair);
the left edge routes, the wall-crossing edge cannot. Pins the documented
non-reconciliations on one run: pairs 2/3 != pads 2/4 != edges, and the open
entry (M, 2/3, 'open', 'partial').

Scenario D (multi-outline, #479 semantics): two outer Edge.Cuts outlines
with a WALL sealing the corridor between them; 3-pad net Z has a routable
pair in outline A and a lone pad in outline B. Cross-outline gaps are joined
by a connector at assembly, never by copper, so Z grades 1/1 (the in-outline
pair) with NO pad_pairs_open entry -- the raw union-find math (P=3, k=2)
would have invented a phantom 'partial' 1/2 that no router can ever close.

    python3 tests/test_432_pad_pairs.py
"""
import io
import json
import os
import re
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kicad_parser import BoardInfo
from synth import make_net, make_pad, make_pcb, make_seg

BLOCKER, X, WALL = 1, 2, 3
M = 1
Y = 1
Z = 1

PAD_PAIRS_OPEN_KEYS = {'net', 'pairs_connected', 'pairs_total',
                       'outcome', 'open_subtype'}

CHECKS = []


def check(name, ok):
    CHECKS.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'}: {name}")


def _board():
    """The #409 walled-room board (copied, not imported: tests are
    standalone scripts). One routing layer; WALL splits the board at x=5
    except a gap y in [4.2, 5.8] enclosed by a room whose only openings are
    narrow slots at y~5; BLOCKER's fresh track through the gap seals X out."""
    bi = BoardInfo(layers={0: 'F.Cu', 31: 'B.Cu'},
                   copper_layers=['F.Cu', 'B.Cu'],
                   board_bounds=(0.0, 0.0, 10.0, 10.0))
    pads = {
        BLOCKER: [make_pad(BLOCKER, 5.0, 4.5, ref='U3', num='1',
                           net_name='BLOCKER', size_x=0.3, size_y=0.3),
                  make_pad(BLOCKER, 5.0, 5.5, ref='U3', num='2',
                           net_name='BLOCKER', size_x=0.3, size_y=0.3)],
        X: [make_pad(X, 1.0, 5.0, ref='U1', num='1', net_name='X',
                     size_x=0.3, size_y=0.3),
            make_pad(X, 9.0, 5.0, ref='U2', num='1', net_name='X',
                     size_x=0.3, size_y=0.3)],
        WALL: [],
    }
    segs = [
        make_seg(5.0, 0.0, 5.0, 4.2, net_id=WALL, width=0.3),
        make_seg(5.0, 5.8, 5.0, 10.0, net_id=WALL, width=0.3),
        make_seg(4.0, 4.0, 6.0, 4.0, net_id=WALL, width=0.3),   # bottom
        make_seg(4.0, 6.0, 6.0, 6.0, net_id=WALL, width=0.3),   # top
        make_seg(4.0, 4.0, 4.0, 4.6, net_id=WALL, width=0.3),   # left lower
        make_seg(4.0, 5.4, 4.0, 6.0, net_id=WALL, width=0.3),   # left upper
        make_seg(6.0, 4.0, 6.0, 4.6, net_id=WALL, width=0.3),   # right lower
        make_seg(6.0, 5.4, 6.0, 6.0, net_id=WALL, width=0.3),   # right upper
    ]
    return make_pcb(
        nets={BLOCKER: make_net(BLOCKER, 'BLOCKER'), X: make_net(X, 'X'),
              WALL: make_net(WALL, 'WALL')},
        segments=segs, pads_by_net=pads, board_info=bi)


def _clean_board():
    """One 2-pad net, nothing in the way: the fully-clean summary case."""
    bi = BoardInfo(layers={0: 'F.Cu', 31: 'B.Cu'},
                   copper_layers=['F.Cu', 'B.Cu'],
                   board_bounds=(0.0, 0.0, 10.0, 10.0))
    pads = {Y: [make_pad(Y, 2.0, 5.0, ref='U1', num='1', net_name='Y',
                         size_x=0.3, size_y=0.3),
                make_pad(Y, 8.0, 5.0, ref='U2', num='1', net_name='Y',
                         size_x=0.3, size_y=0.3)]}
    return make_pcb(nets={Y: make_net(Y, 'Y')}, segments=[],
                    pads_by_net=pads, board_info=bi)


def _multipoint_board():
    """4-pad net M; its right pad pair is pre-joined by existing copper
    (endpoints on the pads, so the dead-end strip keeps it); a full-height
    static WALL at x=5 blocks the crossing on the only routing layer. The
    component-MST spans 3 components with 2 edges; the wall edge cannot
    route, the left edge can."""
    bi = BoardInfo(layers={0: 'F.Cu', 31: 'B.Cu'},
                   copper_layers=['F.Cu', 'B.Cu'],
                   board_bounds=(0.0, 0.0, 10.0, 10.0))
    pads = {
        M: [make_pad(M, 1.0, 3.0, ref='U1', num='1', net_name='M',
                     size_x=0.3, size_y=0.3),
            make_pad(M, 1.0, 7.0, ref='U1', num='2', net_name='M',
                     size_x=0.3, size_y=0.3),
            make_pad(M, 8.0, 3.0, ref='U2', num='1', net_name='M',
                     size_x=0.3, size_y=0.3),
            make_pad(M, 8.0, 7.0, ref='U2', num='2', net_name='M',
                     size_x=0.3, size_y=0.3)],
        WALL: [],
    }
    segs = [
        make_seg(5.0, 0.0, 5.0, 10.0, net_id=WALL, width=0.3),
        # Pre-existing M copper joining the right pad pair pad-to-pad.
        make_seg(8.0, 3.0, 8.0, 7.0, net_id=M, width=0.15),
    ]
    return make_pcb(
        nets={M: make_net(M, 'M'), WALL: make_net(WALL, 'WALL')},
        segments=segs, pads_by_net=pads, board_info=bi)


def _multi_outline_board():
    """Two outer outlines (a split-board file, #479): A spans x in [0,10],
    B spans x in [20,30]; a full-height static WALL at x=15 seals the only
    routing layer between them. Net Z: a routable pad pair inside A plus a
    lone pad inside B. The B pad can only ever be reached by the assembly
    connector, so outline-aware pair credit is total=1 (A's pair), and once
    that pair routes, connected=1 with no open entry."""
    bi = BoardInfo(layers={0: 'F.Cu', 31: 'B.Cu'},
                   copper_layers=['F.Cu', 'B.Cu'],
                   board_bounds=(0.0, 0.0, 30.0, 10.0),
                   board_outlines=[
                       [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)],
                       [(20.0, 0.0), (30.0, 0.0), (30.0, 10.0), (20.0, 10.0)],
                   ])
    pads = {
        Z: [make_pad(Z, 2.0, 5.0, ref='U1', num='1', net_name='Z',
                     size_x=0.3, size_y=0.3),
            make_pad(Z, 8.0, 5.0, ref='U2', num='1', net_name='Z',
                     size_x=0.3, size_y=0.3),
            make_pad(Z, 25.0, 5.0, ref='U3', num='1', net_name='Z',
                     size_x=0.3, size_y=0.3)],
        WALL: [],
    }
    segs = [make_seg(15.0, 0.0, 15.0, 10.0, net_id=WALL, width=0.3)]
    return make_pcb(
        nets={Z: make_net(Z, 'Z'), WALL: make_net(WALL, 'WALL')},
        segments=segs, pads_by_net=pads, board_info=bi)


def _route(pcb, net_names, **kwargs):
    from route import batch_route
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok, fail, _t, results_data = batch_route(
            'synthetic', '', net_names,
            layers=['F.Cu'],
            clearance=0.1, track_width=0.15,
            via_size=0.5, via_drill=0.3, grid_step=0.1,
            ordering_strategy='original',
            final_reconcile=False,
            return_results=True, pcb_data=pcb, **kwargs)
    out = buf.getvalue()
    sys.stdout.write(out)
    m = re.findall(r'JSON_SUMMARY: (\{.*\})', out)
    check("JSON_SUMMARY present", bool(m))
    return (json.loads(m[-1]) if m else {}), results_data, out


def scenario_a():
    print("-" * 60)
    print("Scenario A: emit (max_rip_up_count=0)")
    summary, results_data, out = _route(_board(), ['BLOCKER', 'X'],
                                        max_rip_up_count=0)

    check("BLOCKER routed", 'BLOCKER' in summary.get('routed_single', []))
    check("X in failed_single", 'X' in summary.get('failed_single', []))
    check("pad_pairs_connected == 1", summary.get('pad_pairs_connected') == 1)
    check("pad_pairs_total == 2", summary.get('pad_pairs_total') == 2)

    entries = summary.get('pad_pairs_open')
    check("'pad_pairs_open' key present", isinstance(entries, list))
    entries = entries or []
    check("exactly one open entry", len(entries) == 1)
    e0 = entries[0] if entries else {}
    check("entry is X 0/1 open/unrouted",
          e0 == {'net': 'X', 'pairs_connected': 0, 'pairs_total': 1,
                 'outcome': 'open', 'open_subtype': 'unrouted'})
    check("entry has exactly the documented keys",
          set(e0.keys()) == PAD_PAIRS_OPEN_KEYS)

    blocker_nets = [b.get('net') for b in summary.get('blockers', [])]
    check("co-emitted with blockers, joinable on net X",
          'X' in blocker_nets and any(x.get('net') == 'X' for x in entries))
    check("return_results carries the same pad_pairs_open (GUI parity)",
          results_data.get('pad_pairs_open') == summary.get('pad_pairs_open'))


def scenario_b():
    print("-" * 60)
    print("Scenario B: role flip + omit-when-empty")
    summary, results_data, out = _route(_board(), ['BLOCKER', 'X'])

    check("X routed after ripping BLOCKER",
          'X' in summary.get('routed_single', []))
    check("pad_pairs 1/2 after the flip",
          summary.get('pad_pairs_connected') == 1
          and summary.get('pad_pairs_total') == 2)
    entries = summary.get('pad_pairs_open') or []
    # BLOCKER was ripped so X could route; its reroute failed and restoring
    # the original track would collide with X's fresh copper, so the restore
    # was refused (#134) -- the subtype that means "left open to avoid
    # creating a short", the richest signal in the enum.
    check("open entry is BLOCKER, collision_refused",
          [(e.get('net'), e.get('open_subtype')) for e in entries]
          == [('BLOCKER', 'collision_refused')])
    check("routed net X never appears in pad_pairs_open",
          all(e.get('net') != 'X' for e in entries))

    print("  -- clean board --")
    summary, results_data, out = _route(_clean_board(), ['Y'])
    check("Y routed", 'Y' in summary.get('routed_single', []))
    check("clean run: tallies present and equal (1/1)",
          summary.get('pad_pairs_connected') == 1
          and summary.get('pad_pairs_total') == 1)
    check("clean run: no pad_pairs_open key (omit-when-empty)",
          'pad_pairs_open' not in summary)
    check("clean run: results_data pad_pairs_open empty list",
          results_data.get('pad_pairs_open') == [])


def scenario_c():
    print("-" * 60)
    print("Scenario C: multipoint pair math (component-MST non-reconciliation)")
    summary, results_data, out = _route(_multipoint_board(), ['M'])

    check("one multipoint net", summary.get('multipoint_nets') == 1)
    # Summary values come from the #184 re-derivation over ALL of the net's
    # pads (route.py sweep), not the raw routing result.
    check("multipoint pads 2/4",
          summary.get('multipoint_pads_connected') == 2
          and summary.get('multipoint_pads_total') == 4)
    # Exact edge counts are retry-path-fragile; the split is what matters.
    check("at least one edge routed and one failed",
          summary.get('multipoint_edges_routed', 0) >= 1
          and summary.get('multipoint_edges_failed', 0) >= 1)
    # The deliberate pin of the documented non-reconciliation: pairs (2/3)
    # measure pad connectivity, pads (2/4) count pads, edges count the
    # component-MST (pre-existing copper joined the right pair first).
    check("pad_pairs 2/3", summary.get('pad_pairs_connected') == 2
          and summary.get('pad_pairs_total') == 3)
    entries = summary.get('pad_pairs_open') or []
    check("single open entry M 2/3 open/partial",
          entries == [{'net': 'M', 'pairs_connected': 2, 'pairs_total': 3,
                       'outcome': 'open', 'open_subtype': 'partial'}])
    fm = [d.get('net_name') if isinstance(d, dict) else d
          for d in summary.get('failed_multipoint', [])]
    check("M in failed_multipoint", 'M' in fm)
    check("return_results parity (scenario C)",
          results_data.get('pad_pairs_open') == summary.get('pad_pairs_open'))


def scenario_d():
    print("-" * 60)
    print("Scenario D: multi-outline boards (#479 cross-outline semantics)")
    summary, results_data, out = _route(_multi_outline_board(), ['Z'])

    # The in-outline pair is the whole universe: 1/1, fully connected.
    check("pad_pairs 1/1 (cross-outline gap is not a pair)",
          summary.get('pad_pairs_connected') == 1
          and summary.get('pad_pairs_total') == 1)
    # The raw union-find view (P=3, k=2 -> 1/2 'partial') must NOT surface:
    # no router can ever close a cross-outline gap, so an open entry here
    # would be a permanent phantom.
    check("no pad_pairs_open key (no phantom cross-outline 'partial')",
          'pad_pairs_open' not in summary)
    check("results_data pad_pairs_open empty (parity)",
          results_data.get('pad_pairs_open') == [])
    # And the failure lists agree: the sweep's net_break_within_outlines
    # scrubbed the cross-outline split, so Z is not reported failed.
    fm = [d.get('net_name') if isinstance(d, dict) else d
          for d in summary.get('failed_multipoint', [])]
    check("Z not in failed_multipoint", 'Z' not in fm)
    check("Z not in failed_single", 'Z' not in summary.get('failed_single', []))


def main():
    print("=" * 60)
    print("PR #432 follow-up: pad-pair tallies in JSON_SUMMARY")
    print("=" * 60)
    scenario_a()
    scenario_b()
    scenario_c()
    scenario_d()
    failed = [n for n, okk in CHECKS if not okk]
    print("-" * 60)
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        sys.exit(1)
    print("ALL PASS")


if __name__ == '__main__':
    main()
