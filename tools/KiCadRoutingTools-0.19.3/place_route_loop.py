"""
Router-in-the-loop placement repair.

Routes the board, reads the failure diagnostics (failed nets + the blocker
nets named in the router's frontier analysis), and micro-quenches ONLY the
parts that could help those routes succeed:

  - parts owning pads of the failed nets (move the endpoint out of the
    congested pocket), and
  - parts owning pads of the blocker nets (move the anchor so the blocking
    wall re-routes),

with the failed nets given extra weight in the quench cost: their airwire
length and any crossing they take part in.
High-pin-count parts are excluded from targeting (--max-target-pins) - moving
a resistor that anchors a blocker net is low-risk; dragging a 144-pin QFP to
fix one net is how placements get destroyed.

The new placement is accepted only if a real re-route improves
(failures, router iterations), otherwise it is reverted and the next round
widens the displacement cap.

Usage:
  python place_route_loop.py input.kicad_pcb output.kicad_pcb \
      --route-args '--nets "/*" "Net-*" --track-width 0.2 ...' \
      [quench options]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

from kicad_parser import parse_kicad_pcb
import routing_defaults as defaults
from placement.groups import GroupError, derive_groups, describe, parse_sources
from placement.quench import quench
from placement.writer import write_placed_output

# The loop shells out to the route.py sitting NEXT TO THIS FILE. A bare
# relative 'route.py' only resolved when the caller's cwd happened to be the
# repo root, and the failure surfaced as the misleading "produced no
# JSON_SUMMARY" instead of "no such file" (#458).
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROUTE_PY = os.path.join(_SCRIPT_DIR, 'route.py')

_SUMMARY_RE = re.compile(r'JSON_SUMMARY: (\{.*\})')

# route.py prints this from the except around its reconciliation self-invoke
# (route.py:2294). The sub-run prints its JSON_SUMMARY BEFORE the board is
# written, so a summary followed by this marker advertises recoveries that may
# never have reached the file on disk.
_RECONCILE_ABORTED = 'final reconciliation pass failed:'

# Counters that measure WORK DONE, so they add across passes. Everything else
# in a summary is state, and state is whatever the LAST pass measured.
_EFFORT_KEYS = ('total_iterations', 'total_vias', 'total_time')


def merge_route_summaries(log: str):
    """Reduce every JSON_SUMMARY in a route.py log to one honest tally.

    route.py runs an end-of-run reconciliation pass (route.py:2144, #348)
    exactly when the first pass left failures. It self-invokes batch_route one
    level deep on the written board and prints a SECOND JSON_SUMMARY, scoped
    to the retried nets, whose failure lists route.py itself calls "the honest
    still-open set". Reading only the first summary counted every
    reconciliation recovery as a failure: the loop quenched parts anchoring
    nets that were already solved, and better() compared tallies taken at
    different points in the run.

    Per field class:

    * FAILURE STATE (failed_single / failed_multipoint / multipoint_pads_*)
      comes from the LAST summary, and is exact rather than a delta. Every net
      with a nonzero failure term is in the retry set by construction, and the
      sub-run re-derives each retried net's pad counts over ALL of that net's
      pads from the final-board union-find (route.py:1840), so the last
      summary's numbers are absolute. Summing pad counts would double-count.
    * EFFORT (total_iterations / total_vias / total_time) is SUMMED: both
      passes are work this placement cost the router, and better()'s iteration
      tiebreak should see all of it. Taking effort from the last summary would
      make a badly failing candidate look cheap, since the reconciliation pass
      only re-routes a handful of nets.
    * The #432 keys are REBUILT, because they are whole-board in the first
      summary but reconcile-subset-scoped in the sub-run's: pad_pairs_total
      keeps pass 1's whole-board denominator, pad_pairs_connected = that
      total minus the deficit of every net still open at END of run (the
      last summary's pad_pairs_open -- every pass-1 open net is in the retry
      set by construction, so absence from the sub-run's list means
      recovered). A sub-run that printed no pad-pair keys at all restores
      pass 1's, the newest numbers that exist. 'blockers' is last-wins when
      the sub-run emitted the key; when it did not, pass 1's entries are kept
      filtered to the nets still failed, so a recovered net's attribution
      dies with it and run_route never regresses to the whole-log regex.
    * Anything else is last-wins.

    Degrades to the single-summary case unchanged: no failures, or a sub-run
    that returned before printing, leaves one summary that is both the first
    and the last. Returns None when the log carries no summary at all.
    """
    raw = _SUMMARY_RE.findall(log)
    if not raw:
        return None
    summaries = [json.loads(s) for s in raw]

    # A reconciliation that raised AFTER printing its summary claims
    # recoveries the board write may never have committed; fall back to the
    # first pass, which is what is definitely on disk.
    aborted = log.rfind(_RECONCILE_ABORTED) > log.rfind(raw[-1])
    merged = dict(summaries[0] if aborted else summaries[-1])

    for key in _EFFORT_KEYS:
        merged[key] = sum(s.get(key, 0) for s in summaries)

    # #432 x #458: rebuild the pad-pair tallies and the blockers key, which
    # last-wins would silently narrow to the reconcile subset (a 50/40
    # whole-board reading becomes the sub-run's 3/2, or vanishes entirely).
    # Skipped when aborted: merged is already pass 1 wholesale, which is
    # what is on disk.
    if len(summaries) > 1 and not aborted:
        first, last = summaries[0], summaries[-1]
        if 'pad_pairs_total' in first:
            if 'pad_pairs_total' in last:
                # Denominator: pass 1's whole board. Connected: that total
                # minus what is STILL open at end of run. A net the
                # reconciliation itself broke that pass 1 never graded
                # subtracts its deficit without widening the denominator --
                # conservative, same spirit as the coverage-gate widening
                # below. merged's pad_pairs_open is already the last
                # summary's, which is the correct final open set.
                _deficit = sum(
                    e.get('pairs_total', 0) - e.get('pairs_connected', 0)
                    for e in (last.get('pad_pairs_open') or []))
                _total = first['pad_pairs_total']
                merged['pad_pairs_total'] = _total
                merged['pad_pairs_connected'] = max(0, _total - _deficit)
            else:
                # The sub-run printed a summary without pad-pair keys (its
                # emission is defensively try/except'd): pass 1's numbers are
                # the newest that exist.
                merged['pad_pairs_total'] = first['pad_pairs_total']
                merged['pad_pairs_connected'] = first.get(
                    'pad_pairs_connected', 0)
                if 'pad_pairs_open' in first:
                    merged['pad_pairs_open'] = first['pad_pairs_open']
        if 'blockers' in first and 'blockers' not in merged:
            _failed = set(merged.get('failed_single') or [])
            _failed |= {d.get('net_name') if isinstance(d, dict) else d
                        for d in (merged.get('failed_multipoint') or [])}
            merged['blockers'] = [e for e in first['blockers']
                                  if e.get('net') in _failed]

    # Coverage-gate nets (route.py:1881) have NO routed result, so their pads
    # never reach multipoint_pads_total and the caller's
    # failures = len(failed_single) + pad-deficit weighs them ZERO, though
    # they ship at broken copper. Give each one weight 1, matching what
    # failed_single gives a net that produced no result at all, by widening
    # the pad denominator. They are in neither failed_single nor the pad
    # tallies (route.py:1887 excludes single_ended_nets and routed_results),
    # so this cannot double-count. It matters most on the LAST summary: those
    # are nets the reconciliation pass ITSELF broke through its rip
    # escalation, and without this the loop can read failures=0 on a board
    # shipping disconnected copper and stop.
    gate = merged.get('coverage_gate_nets') or []
    if gate:
        merged['multipoint_pads_total'] = (
            merged.get('multipoint_pads_total', 0) + len(gate))
    return merged


def _log_tail(log: str, lines: int = 15) -> str:
    """Last few log lines, so an error names the real failure instead of
    burying it behind a path the operator has to go open."""
    return ''.join(f"  | {ln}\n" for ln in log.splitlines()[-lines:])


def _run_route_cmd(cmd, log_file):
    """Launch route.py with stdout and stderr captured to log_file; return its
    exit code. Split out from run_route so the whole tally can be driven
    against a canned log without launching a child process."""
    with open(log_file, 'w', encoding='utf-8') as f:
        return subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT).returncode


def run_route(pcb_file: str, routed_file: str, route_args: str, log_file: str):
    """Run route.py, return (metrics dict, log text)."""
    # Absolute path to the sibling route.py, so the loop runs from any cwd.
    # No cwd= override: route.py resolves its own assets from __file__, and
    # relative paths inside --route-args (--net-clearances foo.json) must keep
    # resolving against the OPERATOR's cwd, which a cwd= would silently break.
    # -X utf8 mirrors how the test suite invokes route.py.
    cmd = [sys.executable, '-X', 'utf8', _ROUTE_PY, pcb_file, routed_file] + \
        shlex.split(route_args)
    rc = _run_route_cmd(cmd, log_file)
    # errors='replace': route.py forces its own stream to UTF-8, but a cp1252
    # default locale on the READING side would raise on the first non-ASCII
    # glyph and lose the whole round.
    with open(log_file, encoding='utf-8', errors='replace') as f:
        log = f.read()
    if rc != 0:
        # route.py exits 0 even when nets fail; its only deliberate non-zero
        # exit is "No nets matched the given patterns!". So non-zero means a
        # crash, an unreadable board or a --route-args typo, none of which is
        # a routing result.
        raise RuntimeError(f"route.py exited {rc} (see {log_file})\n"
                           + _log_tail(log))

    summary = merge_route_summaries(log)
    if summary is None:
        raise RuntimeError(f"route.py produced no JSON_SUMMARY (see {log_file})"
                           f"\n" + _log_tail(log))

    failed_nets = list(summary.get('failed_single', []))
    # failed_multipoint entries are dicts {net_name, failed_pads}; keep just the
    # name so failed_nets is uniformly net-name strings (downstream uses them as
    # dict keys -> a dict here raises "unhashable type: 'dict'").
    failed_nets += [d['net_name'] if isinstance(d, dict) else d
                    for d in summary.get('failed_multipoint', [])]
    mp_deficit = (summary.get('multipoint_pads_total', 0)
                  - summary.get('multipoint_pads_connected', 0))
    failures = len(summary.get('failed_single', [])) + mp_deficit

    # Blocker nets from frontier diagnostics. Prefer the structured
    # JSON_SUMMARY 'blockers' key (#409): the last-wins attribution of nets
    # still failed at END of run, capped 10/net -- a narrower, more targeted
    # move-candidate set. Fallback for older logs: scrape every transient
    # "  1. /MD1: 46 (31.7%) ..." line in the whole log (includes blockers of
    # nets that later routed and every N-retry re-analysis).
    jb = summary.get('blockers')
    if jb is not None:
        # An explicit empty list means "structured emitter, nothing left to
        # attribute" (e.g. the reconciliation recovered every failure) -- do
        # NOT regress to the whole-log regex, which would scrape transient
        # blocks of nets that later routed.
        blockers = {b['net'] for e in jb for b in e.get('blocked_by', [])}
    else:
        blockers = set(re.findall(r'^\s+\d+\.\s+(\S+?):\s+\d+\s+\(', log, re.M))

    return {
        'failures': failures,
        'failed_nets': failed_nets,
        'blockers': sorted(blockers),
        'iterations': summary.get('total_iterations', 0),
        'vias': summary.get('total_vias', 0),
        # #409 follow-up: pad-pair routability tallies (PRR = connected/total
        # downstream); 0/0 on logs from routers without the keys. Report-only
        # here -- better() still compares failures/iterations (#458's scope).
        'pad_pairs_connected': summary.get('pad_pairs_connected', 0),
        'pad_pairs_total': summary.get('pad_pairs_total', 0),
    }


def nets_to_refs(pcb_data, net_names, max_pins, locked_patterns):
    """Map net names to the movable component refs that own their pads."""
    import fnmatch
    name_to_id = {net.name: nid for nid, net in pcb_data.nets.items()}
    refs = set()
    for name in net_names:
        nid = name_to_id.get(name)
        if nid is None:
            continue
        for pad in pcb_data.nets[nid].pads:
            refs.add(pad.component_ref)
    out = set()
    for ref in refs:
        fp = pcb_data.footprints.get(ref)
        if fp is None:
            continue
        pins = len([p for p in fp.pads if p.net_id > 0])
        if pins > max_pins:
            continue
        if locked_patterns and any(fnmatch.fnmatch(ref, p)
                                   for p in locked_patterns):
            continue
        out.add(ref)
    return out


def better(a, b):
    """Is metrics a better than b? Failures first, then iterations."""
    if a['failures'] != b['failures']:
        return a['failures'] < b['failures']
    return a['iterations'] < b['iterations'] * 0.95


def _ratsnest_screen(before, after, pct):
    """(skip, note) -- should this candidate skip its routing run? (#504)

    Routing is the honest judge but an expensive one, often minutes per round.
    A candidate whose ratsnest got clearly WORSE than the board it came from is
    very unlikely to route better, so it is not worth paying for.

    Screens on `crossings` and `hpwl` only. Both are unweighted -- crossings is
    a raw count by contract, hpwl is pure pad geometry -- whereas `length` and
    `total` are scaled by the per-round net_weights this loop sets from
    --failed-net-weight. (before/after share the weights within one quench call,
    so length is safe to REPORT; it just is not a stable thing to threshold on.)

    pct <= 0 disables the screen, which is the default: skipping a placement
    that would in fact have won is a real cost, so this is opt-in and every
    decision is logged with its numbers.
    """
    if not pct or pct <= 0 or not before or not after:
        return False, ""
    worse = []
    note = []
    for key, label in (('crossings', 'crossings'), ('hpwl', 'hpwl')):
        b, a = before.get(key), after.get(key)
        if b is None or a is None:
            continue
        delta = (a - b) / b * 100.0 if b else (100.0 if a else 0.0)
        note.append(f"{label} {b:g}->{a:g} ({delta:+.1f}%)")
        if delta > pct:
            worse.append(f"{label} {delta:+.1f}%")
    txt = ", ".join(note)
    if worse:
        return True, f"{txt}  [regressed > {pct:g}%: {', '.join(worse)}]"
    return False, txt


def main():
    parser = argparse.ArgumentParser(
        description="Router-in-the-loop placement repair.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input_file")
    parser.add_argument("output_file")
    parser.add_argument("--route-args", required=True,
                        help="Arguments passed to route.py (quoted string)")
    parser.add_argument("--rounds", type=int, default=5,
                        help="Max repair rounds (default: 5)")
    parser.add_argument("--max-displacement", type=float, default=3.0,
                        help="Initial displacement cap per round in mm "
                             "(default: 3; widened 1.5x after a rejected "
                             "round - nudges only, never swaps)")
    parser.add_argument("--swap-max-displacement", type=float, default=None,
                        help="Displacement cap for same-footprint swaps in mm; "
                             "must not exceed --max-displacement and is NOT "
                             "widened between rounds (default: the initial "
                             "--max-displacement)")
    parser.add_argument("--max-target-pins", type=int, default=40,
                        help="Don't move parts with more connected pins than "
                             "this (default: 40)")
    parser.add_argument("--failed-net-weight", type=float, default=3.0,
                        help="Cost multiplier for failed nets: scales their "
                             "airwire length and any crossing they take part "
                             "in (default: 3.0)")
    parser.add_argument("--group-by", default="none",
                        help="Move blocks of parts as one rigid body. Comma "
                             "list of: kicad, sheet, netprefix, decap; 'auto' = "
                             "kicad,sheet (default: none). A targeted part pulls "
                             "in its whole block, so a block is no longer "
                             "half-frozen because its IC exceeds "
                             "--max-target-pins")
    parser.add_argument("--ratsnest-screen", type=float, default=0.0,
                        help="Skip the routing run when a candidate placement's "
                             "airwire crossings or HPWL regress by more than "
                             "this percent against the board it came from -- a "
                             "cheap pre-route filter (0 = disabled, the default)")
    parser.add_argument("--step", type=float, default=0.5,
                        help="Candidate grid step in mm (default: 0.5)")
    parser.add_argument("--length-weight", type=float, default=0.3)
    parser.add_argument("--crossing-penalty", type=float, default=30.0)
    parser.add_argument("--halo-base", type=float, default=0.5)
    parser.add_argument("--halo-coef", type=float, default=0.15)
    parser.add_argument("--halo-weight", type=float, default=2.0)
    parser.add_argument("--edge-halo", type=float, default=2.0)
    parser.add_argument("--edge-weight", type=float, default=2.0)
    parser.add_argument("--clearance", type=float, default=defaults.CLEARANCE)
    parser.add_argument("--board-edge-clearance", type=float, default=0.55)
    parser.add_argument("--grid-step", type=float, default=defaults.GRID_STEP)
    parser.add_argument("--ignore-nets", nargs="+", default=None)
    parser.add_argument("--lock", nargs="+", default=None)
    parser.add_argument("--no-rotate", action="store_true",
                        help="Disable rotation moves")
    parser.add_argument("--no-swap", action="store_true",
                        help="Disable same-footprint swap moves")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print each accepted quench move and the "
                             "per-pass swap-capped count")
    parser.add_argument("--work-dir", default=None,
                        help="Directory for intermediate files "
                             "(default: alongside output)")
    args = parser.parse_args()

    if args.max_displacement < 0:
        parser.error("--max-displacement must be >= 0")
    if args.ratsnest_screen < 0:
        parser.error("--ratsnest-screen must be >= 0 (0 disables it)")
    try:
        group_sources = parse_sources(args.group_by)
    except GroupError as exc:
        parser.error(str(exc))
    if args.swap_max_displacement is not None:
        if args.swap_max_displacement < 0:
            parser.error("--swap-max-displacement must be >= 0")
        if args.swap_max_displacement > args.max_displacement:
            parser.error("--swap-max-displacement must not exceed "
                         "--max-displacement")

    work = args.work_dir or os.path.dirname(os.path.abspath(args.output_file))
    os.makedirs(work, exist_ok=True)

    cur_file = os.path.join(work, 'loop_round0.kicad_pcb')
    shutil.copy(args.input_file, cur_file)

    screened = 0
    print("Round 0: routing initial placement...")
    best = run_route(cur_file, os.path.join(work, 'loop_round0_routed.kicad_pcb'),
                     args.route_args, os.path.join(work, 'loop_round0_route.log'))
    print(f"  failures={best['failures']} iterations={best['iterations']:,}"
          f" vias={best['vias']}")

    max_disp = args.max_displacement
    # The swap cap is pinned to the BASE displacement and never widened (#458).
    # A rejected round widens the NUDGE radius, a local search around each
    # part's own seed that a real re-route then validates. Widening the swap
    # cap in lockstep turns a 3mm budget into a 15mm teleport after four
    # rejections, which is the #430 stranding failure coming back through the
    # loop. Swaps get one fixed budget for the whole run.
    swap_cap = (args.max_displacement if args.swap_max_displacement is None
                else args.swap_max_displacement)

    for rnd in range(1, args.rounds + 1):
        if best['failures'] == 0:
            print("No failures left - stopping.")
            break

        pcb_data = parse_kicad_pcb(cur_file)
        targets = nets_to_refs(pcb_data,
                               best['failed_nets'] + best['blockers'],
                               args.max_target_pins, args.lock)
        if not targets:
            print("No movable target parts - stopping.")
            break

        # #459: a targeted part pulls in its whole block, so the block moves as
        # one body instead of its members fighting each other -- and a block is
        # no longer half-frozen because its IC exceeds --max-target-pins.
        blocks = {}
        if group_sources:
            blocks = derive_groups(pcb_data, group_sources)
            pulled = {r for refs in blocks.values() for r in refs
                      if any(m in targets for m in refs)}
            if pulled - targets:
                print(f"  blocks pull in {len(pulled - targets)} more part(s)")
            targets |= pulled
            blocks = {n: r for n, r in blocks.items()
                      if any(m in targets for m in r)}

        name_to_id = {net.name: nid for nid, net in pcb_data.nets.items()}
        net_weights = {name_to_id[n]: args.failed_net_weight
                       for n in best['failed_nets'] if n in name_to_id}

        print(f"Round {rnd}: failed={best['failed_nets']}")
        print(f"  blockers={best['blockers'][:8]}"
              f"{'...' if len(best['blockers']) > 8 else ''}")
        print(f"  targeting {len(targets)} parts"
              f" (max_disp={max_disp:.1f}mm, swap cap={swap_cap:.1f}mm):"
              f" {', '.join(sorted(targets))}")

        ratsnest = {}
        placements = quench(
            pcb_data, pcb_file=cur_file,
            max_displacement=max_disp,
            swap_max_displacement=swap_cap,
            step=args.step,
            grid_step=args.grid_step, clearance=args.clearance,
            board_edge_clearance=args.board_edge_clearance,
            crossing_penalty=args.crossing_penalty,
            length_weight=args.length_weight,
            halo_base=args.halo_base, halo_coef=args.halo_coef,
            halo_weight=args.halo_weight,
            edge_halo=args.edge_halo, edge_weight=args.edge_weight,
            allow_rotations=not args.no_rotate,
            allow_swaps=not args.no_swap,
            ignore_nets=args.ignore_nets, lock_refs=args.lock,
            move_refs=targets, net_weights=net_weights,
            metrics_out=ratsnest,
            groups=blocks,
            verbose=args.verbose,
        )

        if not placements:
            print(f"  Quench found no improving moves - widening the nudge cap"
                  f" (swap cap stays {swap_cap:.1f}mm).")
            max_disp *= 1.5
            continue

        # #504: quench was handed the CURRENT best board, so its own 'before' IS
        # that board's ratsnest and 'after' is the candidate's -- the screen gets
        # its baseline for free, with no second QuenchState (which would re-read
        # the board for courtyards and locked refs) and no round-0 bootstrap.
        rn_before = ratsnest.get('before', {})
        rn_after = ratsnest.get('after', {})
        skip, why = _ratsnest_screen(rn_before, rn_after, args.ratsnest_screen)
        if why:
            print(f"  ratsnest: {why}")
        if skip:
            print(f"  SCREENED - skipping the routing run, widening the nudge cap"
                  f" (swap cap stays {swap_cap:.1f}mm).")
            screened += 1
            max_disp *= 1.5
            continue

        cand_file = os.path.join(work, f'loop_round{rnd}.kicad_pcb')
        write_placed_output(cur_file, cand_file, placements)

        metrics = run_route(
            cand_file, os.path.join(work, f'loop_round{rnd}_routed.kicad_pcb'),
            args.route_args, os.path.join(work, f'loop_round{rnd}_route.log'))
        # Report-only, exactly like the pad_pairs_* keys above: every round now
        # records placement quality next to the routing result. better() is
        # deliberately untouched -- reworking the comparator is #458's scope.
        metrics['ratsnest_crossings'] = rn_after.get('crossings')
        metrics['ratsnest_hpwl'] = rn_after.get('hpwl')
        metrics['ratsnest_length'] = rn_after.get('length')
        print(f"  -> failures={metrics['failures']}"
              f" iterations={metrics['iterations']:,} vias={metrics['vias']}")

        if better(metrics, best):
            print(f"  ACCEPTED (was failures={best['failures']},"
                  f" iterations={best['iterations']:,})")
            best = metrics
            cur_file = cand_file
            max_disp = args.max_displacement
        else:
            print(f"  REJECTED - reverting, widening the nudge cap"
                  f" (swap cap stays {swap_cap:.1f}mm).")
            max_disp *= 1.5

    shutil.copy(cur_file, args.output_file)
    if args.ratsnest_screen > 0:
        print(f"Ratsnest screen: {screened} round(s) skipped the routing run"
              f" at {args.ratsnest_screen:g}% regression.")
    print(f"Final: failures={best['failures']} iterations={best['iterations']:,}"
          f" vias={best['vias']}")
    print(f"Wrote {args.output_file}")


if __name__ == "__main__":
    main()
