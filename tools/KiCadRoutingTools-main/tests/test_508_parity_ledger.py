#!/usr/bin/env python3
"""#508: pcb_data / write-list divergence -- parity-ledger wiring + reconcile units.

The #508 audit found that the one engine calling the board/file ledger
(route.py) was the one engine clean on the shorting direction, while every
serious finding sat on an unledgered path. These tests pin:

1. LEDGER WIRING (source-level): all four engines call
   verify_written_file_parity, so a pass that changes one representation
   without the other is catchable with KICAD_BOARD_LEDGER=1 everywhere.
2. LIVE LEDGER (end-to-end): route.py / route_planes / repair_planes
   runs on a tiny board under KICAD_BOARD_LEDGER=1 come back [FILE_LEDGER] OK.
   Skipped (not failed) when the Rust router is unavailable.
3. RECONCILE UNITS (the real functions, never mirrored copies):
   - plane_write_reconcile.consume_inner_strips drops matching write-list
     emissions, forwards input strips, and mirrors pcb_data (finding 1).
   - bga_fanout route-track custody: _rebuild_track_for_route replaces ONLY
     its own route's tracks -- a same-net sibling ball keeps its escape
     (finding 6, the cparti_fpga dead via-in-pad class).
   - merge_close_same_net_vias mirrors the merge into pcb_data (finding 12).

    python3 tests/test_508_parity_ledger.py
"""
import io
import os
import sys
import contextlib
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import env_knobs

fails = []


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        fails.append(name)


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------
# 1. Ledger wiring: every engine must carry a verify_written_file_parity call.
# --------------------------------------------------------------------------
def test_ledger_wiring():
    for engine in ('route.py', 'route_diff.py', 'route_planes.py',
                   'repair_planes.py'):
        src = open(os.path.join(ROOT, engine), encoding='utf-8').read()
        n = src.count('verify_written_file_parity(')
        # >= 1 CALL beyond a bare import (the import line has no open paren
        # immediately after the name in this codebase's import style).
        check(f"{engine} calls verify_written_file_parity", n >= 1)
    src = open(os.path.join(ROOT, 'route.py'), encoding='utf-8').read()
    check("route.py keeps the in-memory verify_board_file_parity call",
          'verify_board_file_parity(' in src)


# --------------------------------------------------------------------------
# 2. Live ledger: tiny board, all three newly-wired plane/route paths, with
#    KICAD_BOARD_LEDGER=1 -- must print OK and never FAILED.
# --------------------------------------------------------------------------
_BOARD = '''(kicad_pcb (version 20260206) (generator test)
 (layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal) (2 "B.Cu" signal)
  (44 "Edge.Cuts" user))
 (gr_rect (start 0 0) (end 40 30) (layer "Edge.Cuts") (width 0.1))
 (footprint "test:r" (layer "F.Cu") (at 6 15)
  (property "Reference" "U1" (at 0 0) (layer "F.SilkS"))
  (pad "1" smd rect (at 0 0) (size 1 0.6) (layers "F.Cu") (net 1 "GND"))
  (pad "2" smd rect (at 2 0) (size 1 0.6) (layers "F.Cu") (net 2 "SIG")))
 (footprint "test:r" (layer "F.Cu") (at 34 15)
  (property "Reference" "U2" (at 0 0) (layer "F.SilkS"))
  (pad "1" smd rect (at 0 0) (size 1 0.6) (layers "F.Cu") (net 1 "GND"))
  (pad "2" smd rect (at 2 0) (size 1 0.6) (layers "F.Cu") (net 2 "SIG")))
 (net 0 "") (net 1 "GND") (net 2 "SIG")
)'''


def _rust_available():
    try:
        sys.path.insert(0, os.path.join(ROOT, 'rust_router'))
        import grid_router  # noqa: F401
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  SKIP live-ledger tests (grid_router unavailable: "
              f"{type(e).__name__})")
        return False


def test_live_ledger():
    if not _rust_available():
        return
    os.environ['KICAD_BOARD_LEDGER'] = '1'
    env_knobs.refresh()
    try:
        import route
        import route_planes
        import repair_planes as rdp
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, 'b.kicad_pcb')
            routed = os.path.join(d, 'b_routed.kicad_pcb')
            planed = os.path.join(d, 'b_planed.kicad_pcb')
            repaired = os.path.join(d, 'b_repaired.kicad_pcb')
            with open(src, 'w') as f:
                f.write(_BOARD)

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                route.batch_route(src, routed, ['SIG'],
                                  layers=['F.Cu', 'B.Cu'],
                                  track_width=0.15, clearance=0.1,
                                  grid_step=0.1)
            out = buf.getvalue()
            check("route.py live ledger: [FILE_LEDGER] route printed",
                  '[FILE_LEDGER] route' in out)
            check("route.py live ledger: no FILE_LEDGER failure",
                  'FILE_LEDGER] route FAILED' not in out
                  and 'diverged' not in out.split('[FILE_LEDGER]')[-1][:200])

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                route_planes.create_plane(
                    input_file=routed, output_file=planed,
                    net_names=['GND'], plane_layers=['In1.Cu'],
                    via_size=0.45, via_drill=0.2, track_width=0.15,
                    clearance=0.1, zone_clearance=0.1, grid_step=0.1)
            out = buf.getvalue()
            check("route_planes live ledger: [FILE_LEDGER] planes printed",
                  '[FILE_LEDGER] planes' in out)
            check("route_planes live ledger: no FILE_LEDGER failure",
                  'FILE_LEDGER] planes FAILED' not in out)

            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    rdp.route_planes(
                        input_file=planed, output_file=repaired,
                        net_names=['GND'], plane_layers=['In1.Cu'],
                        track_width=0.15, clearance=0.1, zone_clearance=0.1,
                        via_size=0.45, via_drill=0.2, grid_step=0.1)
                out = buf.getvalue()
                # The repair writes only when it added/changed something; on
                # this tiny healthy board it may pass through -- either the
                # ledger line printed, or no write happened (both fine).
                wrote = 'Output written to' in out
                if wrote:
                    check("repair live ledger: [FILE_LEDGER] planes-repair printed",
                          '[FILE_LEDGER] planes-repair' in out)
                    check("repair live ledger: no FILE_LEDGER failure",
                          'FILE_LEDGER] planes-repair FAILED' not in out)
                else:
                    check("repair live ledger: pass-through (no write, no audit needed)",
                          True)
            except Exception as e:  # noqa: BLE001
                print(buf.getvalue()[-2000:])
                check(f"repair live ledger: engine ran ({type(e).__name__}: "
                      f"{str(e)[:80]})", False)
    finally:
        os.environ.pop('KICAD_BOARD_LEDGER', None)
        env_knobs.refresh()


# --------------------------------------------------------------------------
# 3a. consume_inner_strips (finding 1): drops matching write-list emissions,
#     forwards strips, mirrors pcb_data.
# --------------------------------------------------------------------------
def test_consume_inner_strips():
    from kicad_parser import Segment, Via
    from plane_write_reconcile import consume_inner_strips

    NET = 7
    removed_seg = Segment(start_x=1.0, start_y=2.0, end_x=3.0, end_y=2.0,
                          width=0.15, layer='In1.Cu', net_id=NET)
    removed_via = Via(x=5.0, y=5.0, size=0.45, drill=0.2,
                      layers=['F.Cu', 'B.Cu'], net_id=NET)
    rdata = {'segments_to_remove': [removed_seg],
             'vias_to_remove': [removed_via]}

    # Write-list: one dict matching the removed segment (reversed ends, to
    # exercise the both-orientations key), one unrelated dict that must stay.
    match = {'start': (3.0, 2.0), 'end': (1.0, 2.0), 'width': 0.15,
             'layer': 'In1.Cu', 'net_id': NET}
    keep = {'start': (9.0, 9.0), 'end': (9.5, 9.0), 'width': 0.15,
            'layer': 'In1.Cu', 'net_id': NET}
    vmatch = {'x': 5.0, 'y': 5.0, 'size': 0.45, 'drill': 0.2,
              'layers': ['F.Cu', 'B.Cu'], 'net_id': NET}
    all_segs = [match, keep]
    all_vias = [vmatch]

    # pcb_data still carrying duplicate kept-piece copies at the removed spots
    dup_seg = Segment(start_x=1.0, start_y=2.0, end_x=3.0, end_y=2.0,
                      width=0.15, layer='In1.Cu', net_id=NET)
    live_seg = Segment(start_x=9.0, start_y=9.0, end_x=9.5, end_y=9.0,
                       width=0.15, layer='In1.Cu', net_id=NET)
    pcb = SimpleNamespace(segments=[dup_seg, live_seg], vias=[])

    fs, fv = [], []
    n = consume_inner_strips(rdata, all_segs, all_vias, pcb, fs, fv, 'test')

    check("consume_inner_strips: matching write-list segment dropped",
          match not in all_segs and keep in all_segs)
    check("consume_inner_strips: matching write-list via dropped",
          vmatch not in all_vias)
    check("consume_inner_strips: strips forwarded to the file channels",
          fs == [removed_seg] and fv == [removed_via])
    check("consume_inner_strips: pcb_data mirror removed the duplicate",
          dup_seg not in pcb.segments and live_seg in pcb.segments)
    check("consume_inner_strips: reports dropped count", n == 2)


# --------------------------------------------------------------------------
# 3b. bga_fanout route-track custody (finding 6): rebuilding one route must
#     not delete a same-net sibling ball's escape.
# --------------------------------------------------------------------------
def test_route_track_custody():
    from bga_fanout.reroute import (_rebuild_track_for_route,
                                    _remove_route_tracks)
    from bga_fanout.types import create_track, route_uid

    def mk_route(net_id, x):
        # Duck-typed FanoutRoute: route_segments reads these fields.
        return SimpleNamespace(
            net_id=net_id, pair_id=None, layer='In1.Cu',
            pad_pos=(x, 10.0), stub_end=(x + 0.5, 10.5),
            exit_pos=(x + 2.0, 10.5), jog_end=None, jog_extension=None,
            channel_point=None, channel_point2=None, pre_channel_jog=None,
            neighbor_connection=False)

    NET = 42
    ball_a = mk_route(NET, 10.0)
    ball_b = mk_route(NET, 20.0)
    tracks = []
    for r in (ball_a, ball_b):
        tracks.append(create_track(r.pad_pos, r.stub_end, 0.1, r.layer,
                                   r.net_id, None, route_uid=route_uid(r)))
        tracks.append(create_track(r.stub_end, r.exit_pos, 0.1, r.layer,
                                   r.net_id, None, route_uid=route_uid(r)))
    # Also an unstamped same-net track (an under-pad escape): must survive.
    underpad = {'start': (30.0, 10.0), 'end': (30.0, 11.0), 'width': 0.1,
                'layer': 'B.Cu', 'net_id': NET}
    tracks.append(underpad)

    ball_a.layer = 'F.Cu'  # the repair moved it
    _rebuild_track_for_route(tracks, ball_a, 0.1)

    b_tracks = [t for t in tracks if t.get('route_uid') == route_uid(ball_b)]
    a_tracks = [t for t in tracks if t.get('route_uid') == route_uid(ball_a)]
    check("custody: sibling ball keeps its escape tracks", len(b_tracks) == 2)
    check("custody: rebuilt route's tracks on the new layer",
          a_tracks and all(t['layer'] == 'F.Cu' for t in a_tracks))
    check("custody: unstamped (under-pad) same-net track untouched",
          underpad in tracks)

    n = _remove_route_tracks(tracks, ball_b)
    check("custody: removal scoped to the one route", n == 2
          and underpad in tracks and len(a_tracks) == len(
              [t for t in tracks if t.get('route_uid') == route_uid(ball_a)]))

    # uid must not alias after an object is dropped (the id()-recycling trap)
    check("custody: route_uid stable and unique",
          route_uid(ball_a) == route_uid(ball_a)
          and route_uid(ball_a) != route_uid(ball_b))


# --------------------------------------------------------------------------
# 3c. merge_close_same_net_vias (finding 12): the merge must reach pcb_data.
# --------------------------------------------------------------------------
def test_merge_close_vias_pcb_mirror():
    from kicad_parser import Segment, Via
    from pcb_modification import merge_close_same_net_vias

    NET = 3
    survivor = Via(x=10.0, y=10.0, size=0.45, drill=0.3,
                   layers=['F.Cu', 'B.Cu'], net_id=NET)
    # The new via, 0.05mm away -- inside drill/2+drill/2+h2h. Stamped into
    # pcb_data during routing (object) AND in the write list (dict).
    new_obj = Via(x=10.05, y=10.0, size=0.45, drill=0.3,
                  layers=['F.Cu', 'B.Cu'], net_id=NET)
    new_dict = {'x': 10.05, 'y': 10.0, 'size': 0.45, 'drill': 0.3,
                'layers': ['F.Cu', 'B.Cu'], 'net_id': NET}
    tap_obj = Segment(start_x=12.0, start_y=10.0, end_x=10.05, end_y=10.0,
                      width=0.2, layer='B.Cu', net_id=NET)
    tap_dict = {'start': (12.0, 10.0), 'end': (10.05, 10.0), 'width': 0.2,
                'layer': 'B.Cu', 'net_id': NET}

    pcb = SimpleNamespace(segments=[tap_obj], vias=[survivor, new_obj],
                          pads_by_net={})
    all_vias = [new_dict]
    all_segs = [tap_dict]
    merged = merge_close_same_net_vias(all_vias, all_segs, pcb, 0.2,
                                       verbose=False)

    check("via merge: write-list via dropped", merged == 1 and not all_vias)
    check("via merge: write-list segment re-anchored",
          tap_dict['end'] == (10.0, 10.0))
    check("via merge: pcb_data via removed (board == write model)",
          new_obj not in pcb.vias and survivor in pcb.vias)
    check("via merge: pcb_data segment re-anchored",
          (tap_obj.end_x, tap_obj.end_y) == (10.0, 10.0))


def run():
    test_ledger_wiring()
    test_consume_inner_strips()
    test_route_track_custody()
    test_merge_close_vias_pcb_mirror()
    test_live_ledger()
    print()
    if fails:
        print("FAILED: %d check(s): %s" % (len(fails), fails))
        return 1
    print("All checks passed")
    return 0


if __name__ == '__main__':
    sys.exit(run())
