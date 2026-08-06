#!/usr/bin/env python3
"""Terminal rip-restore must not claim success for copper that isn't connected.

Run-7 E2: a rip victim's terminal restore graded the saved payload only for
CLEARANCE conflicts. A payload that was partial (recorded off an already-broken
net, or one whose copper never covered every pad) restored cleanly, printed a
success line, and counted `successful += 1` -- the run reported a routed net
that shipped open.

Contract under test:
  * try_terminal_restore returns 'full' only when the post-restore state also
    GRADES CONNECTED (authoritative union-find), 'full_open' when conflict-free
    but broken;
  * the stub path's message says the net remains UNROUTED (it used to read
    like a success);
  * both reroute_loop call sites count success only on 'full' and record the
    outcome in state.terminal_restores;
  * route.py folds broken outcomes into the coverage-gate disturbed set (a
    full restore unwinds the stale-strip signal the gate otherwise keys on)
    and exports summary['terminal_restores'].
"""
import contextlib
import inspect
import io
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from kicad_parser import BoardInfo, Net, Pad, PCBData, Segment  # noqa: E402
from rip_restore import try_terminal_restore  # noqa: E402


def _pad(x, y, net_id, name, ref='U1', num='1'):
    return Pad(component_ref=ref, pad_number=num, global_x=x, global_y=y,
               local_x=0.0, local_y=0.0, size_x=1.0, size_y=1.0,
               shape='rect', layers=['F.Cu'], net_id=net_id, net_name=name,
               pad_type='smd')


def _board(extra_segments=()):
    pads = [_pad(0.0, 0.0, 1, 'N1', num='1'), _pad(5.0, 0.0, 1, 'N1', num='2')]
    pcb = PCBData(
        board_info=BoardInfo(layers={0: 'F.Cu'}, copper_layers=['F.Cu']),
        nets={1: Net(1, 'N1', pads), 2: Net(2, 'N2')},
        footprints={},
        vias=[],
        segments=list(extra_segments),
        pads_by_net={1: pads})
    return pcb


def _seg(x1, y1, x2, y2, net_id, width=0.2):
    return Segment(start_x=x1, start_y=y1, end_x=x2, end_y=y2,
                   width=width, layer='F.Cu', net_id=net_id)


CONFIG = SimpleNamespace(clearance=0.2)


def test_connected_payload_is_full():
    pcb = _board()
    pcb._rip_saved = {1: ({'new_segments': [_seg(0, 0, 5, 0, 1)],
                           'new_vias': []}, {1}, True)}
    assert try_terminal_restore(pcb, CONFIG, 1) == 'full'
    print("  PASS: conflict-free AND connected payload -> 'full'")


def test_partial_payload_is_full_open_not_full():
    """The E2 lie: conflict-free copper that never reaches pad 2."""
    pcb = _board()
    pcb._rip_saved = {1: ({'new_segments': [_seg(0, 0, 2, 0, 1)],
                           'new_vias': []}, {1}, True)}
    verdict = try_terminal_restore(pcb, CONFIG, 1)
    assert verdict == 'full_open', (
        f"a payload leaving pad 2 disconnected must grade 'full_open', "
        f"got {verdict!r} -- restoring it as 'full' ships an open net "
        f"counted as routed")
    print("  PASS: partial payload -> 'full_open', never 'full'")


def test_stub_message_says_unrouted():
    """Foreign copper blocks the full restore; only the pad-A stub survives.
    The message must read as a failure, not a success."""
    blocker = _seg(2.5, -1.0, 2.5, 1.0, 2)
    pcb = _board(extra_segments=[blocker])
    pcb._rip_saved = {1: ({'new_segments': [_seg(0, 0, 1, 0, 1),
                                            _seg(1, 0, 5, 0, 1)],
                           'new_vias': []}, {1}, True)}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        verdict = try_terminal_restore(pcb, CONFIG, 1)
    assert verdict == 'stub', f"expected 'stub', got {verdict!r}"
    out = buf.getvalue()
    assert 'UNROUTED' in out, (
        f"the stub print must say the net remains UNROUTED, got: {out!r}")
    print("  PASS: stub path inserts the stub and says UNROUTED")


def test_reroute_loop_counts_success_only_on_full():
    import reroute_loop
    src = inspect.getsource(reroute_loop)
    n_sites = src.count("try_terminal_restore(")
    assert n_sites >= 2, "expected both (single + pair) terminal-restore sites"
    assert src.count("_tr in ('full', 'full_open')") >= 2, (
        "both call sites must restore on 'full_open' (more copper than "
        "nothing) while refusing the success claim")
    assert src.count('state.terminal_restores[') >= 4, (
        "both call sites must record outcomes in state.terminal_restores "
        "(single net + both pair members, full and stub paths)")
    print("  PASS: reroute_loop counts success only on 'full' and records outcomes")


def test_route_py_exports_and_gates_terminal_restores():
    import route
    src = inspect.getsource(route.batch_route)
    assert "summary['terminal_restores']" in src, (
        "route.py must export terminal-restore outcomes additively")
    m_cov = src.find('_cov_disturbed = (')
    assert m_cov != -1
    cov_block = src[m_cov:m_cov + 900]
    assert 'terminal_restores' in cov_block, (
        "broken terminal restores must join the coverage-gate disturbed set "
        "-- a full restore unwinds the stale-strip signal the gate keys on")
    print("  PASS: route.py gates and exports terminal restores")


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in fns:
        print(f"--- {fn.__name__}")
        fn()
    print("ALL PASS")
