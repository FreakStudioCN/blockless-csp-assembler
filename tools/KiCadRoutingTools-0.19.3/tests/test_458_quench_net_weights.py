"""
Tests for per-net weighting of airwire crossings (issue #458).

net_weights used to scale airwire LENGTH only. At place_route_loop's own
knobs (length_weight 0.3, crossing_penalty 30) the crossing term is about 96%
of the objective, so --failed-net-weight 3.0 raised a length coefficient from
0.3 to 0.9 against a crossing coefficient of 30: on a real board that moved a
typical failed net by about a fifth of one crossing. Each crossing is now
priced at max of the two crossing nets' weights, so a weighted net's whole
contribution scales by its weight, which is what the flag has always claimed.

max() is the rule that keeps an unweighted board untouched, since
max(1, 1) = 1. That backward compatibility is not an accident and is pinned
below: the reported crossing count stays a raw int, and the unweighted code
path is the same integer arithmetic it always was.

The two-pose fixture is built so the weighting flips the optimizer's CHOICE,
not just its arithmetic: with the nudge grid at step=10 inside a 20x21mm
outline, exactly two candidate poses survive board containment, and they
cross different nets.
"""

import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from kicad_parser import parse_kicad_pcb
from placement.quench import (QuenchState, quench, _aw_array,
                              _count_crossings_np, _count_crossings_within)

INTERF_U = os.path.join(os.path.dirname(__file__), '..', 'kicad_files',
                        'interf_u_unrouted.kicad_pcb')


def _cap_footprint(ref, x, y, net_id, net_name):
    """Movable 2-pad part; pad 1 (at -0.5,0) carries the net."""
    return f'''\t(footprint "test:CAP2P"
\t\t(layer "F.Cu")
\t\t(uuid "fp-{ref}")
\t\t(at {x} {y})
\t\t(property "Reference" "{ref}"
\t\t\t(at 0 0)
\t\t)
\t\t(pad "1" smd rect
\t\t\t(at -0.5 0)
\t\t\t(size 0.8 0.8)
\t\t\t(layers "F.Cu")
\t\t\t(net {net_id} "{net_name}")
\t\t\t(uuid "p1-{ref}")
\t\t)
\t)
'''


def _anchor_footprint(ref, x, y, net_id, net_name):
    """Locked single-pad net anchor (a connector pin the airwire pulls at)."""
    return f'''\t(footprint "test:PIN"
\t\t(layer "F.Cu")
\t\t(locked yes)
\t\t(uuid "fp-{ref}")
\t\t(at {x} {y})
\t\t(property "Reference" "{ref}"
\t\t\t(at 0 0)
\t\t)
\t\t(pad "1" smd rect
\t\t\t(at 0 0)
\t\t\t(size 1 1)
\t\t\t(layers "F.Cu")
\t\t\t(net {net_id} "{net_name}")
\t\t\t(uuid "p1-{ref}")
\t\t)
\t)
'''


def _crossing_board():
    """Movable M on net NX inside a (40,45)-(60,66) outline, with its anchor
    JX at (58,55). With step=10 and max_displacement=10 the candidate grid is
    five points and board containment leaves exactly two: the seed (50,60) and
    (50,50).

    From the seed, M's airwire to JX crosses the NF pair once. From (50,50) it
    crosses the NK1 and NK2 pairs, twice. So the unweighted cost prefers the
    seed (1 crossing < 2) and weighting NF by 3 flips it (3 > 2)."""
    body = '(kicad_pcb\n\t(version 20241229)\n'
    body += ('\t(net 0 "")\n\t(net 1 "NX")\n\t(net 2 "NF")\n'
             '\t(net 3 "NK1")\n\t(net 4 "NK2")\n')
    body += '''\t(gr_rect
\t\t(start 40 45)
\t\t(end 60 66)
\t\t(stroke
\t\t\t(width 0.1)
\t\t\t(type solid)
\t\t)
\t\t(layer "Edge.Cuts")
\t\t(uuid "edge1")
\t)
'''
    body += _cap_footprint('M', 50.0, 60.0, 1, 'NX')
    body += _anchor_footprint('JX', 58.0, 55.0, 1, 'NX')
    body += _anchor_footprint('JF1', 53.75, 56.5, 2, 'NF')
    body += _anchor_footprint('JF2', 53.75, 58.5, 2, 'NF')
    body += _anchor_footprint('JK1a', 52.0, 50.5, 3, 'NK1')
    body += _anchor_footprint('JK1b', 52.0, 52.5, 3, 'NK1')
    body += _anchor_footprint('JK2a', 55.5, 52.5, 4, 'NK2')
    body += _anchor_footprint('JK2b', 55.5, 54.5, 4, 'NK2')
    body += ')\n'
    fd, path = tempfile.mkstemp(suffix='.kicad_pcb')
    with os.fdopen(fd, 'w') as f:
        f.write(body)
    return path


# Pure crossing objective: length and every geometry term zeroed, so each
# number below is exactly crossing_penalty * weighted crossings.
KNOBS = dict(max_displacement=10.0, step=10.0, grid_step=0.1,
             length_weight=0.0, crossing_penalty=30.0,
             halo_base=0.0, halo_coef=0.0, halo_weight=0.0,
             edge_halo=0.0, edge_weight=0.0,
             allow_rotations=False, allow_swaps=False, max_passes=2)

NX, NF = 1, 2


def _state(path, net_weights=None):
    return QuenchState(parse_kicad_pcb(path), path, 0.25, 0.55, 30.0,
                       0.0, 0.0, 0.0, 0.0, 0.0, 0.1, 0.0,
                       net_weights=net_weights)


def _pose_subset(state, x, y):
    part = state.parts['M']
    return {NX: state._build_net_airwires(
        NX, override_ref='M', override_pads=part.pad_globals(x, y, part.rot))}


def _random_airwires(rng, n, n_nets):
    """Pseudo-random (n,5) airwire array over a 50x50 box."""
    pts = rng.uniform(0.0, 50.0, size=(n, 4))
    nets = rng.integers(1, n_nets + 1, size=(n, 1)).astype(float)
    return np.hstack([pts, nets])


def test_unweighted_path_is_bit_identical():
    """All-ones weights must reproduce the integer count exactly, and the
    weighted figure must be exactly float(count). Equality on the floats, not
    approximate equality: this is what keeps every existing quench result
    from shifting by an ulp."""
    rng = np.random.default_rng(458)
    a = _random_airwires(rng, 400, 12)
    b = _random_airwires(rng, 400, 12)
    ones = np.ones(13)

    n_none, w_none = _count_crossings_np(a, b)
    n_ones, w_ones = _count_crossings_np(a, b, ones)
    assert n_none > 0, "fixture must actually produce crossings"
    assert n_ones == n_none
    assert w_none == float(n_none)
    assert w_ones == float(n_none)

    n_none, w_none = _count_crossings_within(a)
    n_ones, w_ones = _count_crossings_within(a, ones)
    assert n_none > 0
    assert n_ones == n_none
    assert w_none == float(n_none)
    assert w_ones == float(n_none)


def test_within_halving_is_exact_under_weights():
    """Two crossing pairs: nets 1x2 (both weight 1) and nets 3x4 (net 4
    weighted 3). The a-vs-a matrix counts every unordered pair twice with the
    same weight, so halving is exact."""
    a = _aw_array([(0.0, 0.0, 10.0, 10.0, 1.0),
                   (0.0, 10.0, 10.0, 0.0, 2.0),
                   (20.0, 0.0, 30.0, 10.0, 3.0),
                   (20.0, 10.0, 30.0, 0.0, 4.0)])
    w = np.ones(5)
    w[4] = 3.0
    total, w_total = _count_crossings_np(a, a, w)
    half, w_half = _count_crossings_within(a, w)
    assert (total, w_total) == (4, 8.0), (total, w_total)
    assert (half, w_half) == (2, 4.0), (half, w_half)
    assert w_half * 2.0 == w_total


def test_crossing_weight_is_max_of_the_two_nets():
    """A crossing is a conflict between two nets, priced by the larger of the
    two weights. Not the sum (which would double every ordinary crossing),
    not the product (which over-prices a weighted-vs-weighted crossing), not
    the mean (which would silently deliver less than the flag says)."""
    w = np.ones(6)
    w[2] = 3.0
    w[3] = 3.0

    def cross(net_a, net_b):
        a = _aw_array([(0.0, 0.0, 10.0, 10.0, float(net_a))])
        b = _aw_array([(0.0, 10.0, 10.0, 0.0, float(net_b))])
        count, weighted = _count_crossings_np(a, b, w)
        assert count == 1
        return weighted

    assert cross(1, 4) == 1.0, "ordinary crossing keeps its unit price"
    assert cross(2, 4) == 3.0, "weighted vs ordinary costs the weight"
    assert cross(4, 2) == 3.0, "and the rule is symmetric"
    assert cross(2, 3) == 3.0, "weighted vs weighted is max, not sum/product"


def test_nets_cost_reports_raw_count_and_weighted_cost():
    """The cost is weighted; the count that comes back with it is not."""
    path = _crossing_board()
    try:
        plain, weighted = _state(path), _state(path, {NF: 3.0})
        other_p = plain.airwires_excluding({NX})
        other_w = weighted.airwires_excluding({NX})
        for (x, y), exp_plain, exp_weighted, exp_count in (
                ((50.0, 60.0), 30.0, 90.0, 1),
                ((50.0, 50.0), 60.0, 60.0, 2)):
            cp, np_count = plain.nets_cost(_pose_subset(plain, x, y), other_p)
            cw, nw_count = weighted.nets_cost(_pose_subset(weighted, x, y),
                                              other_w)
            assert cp == exp_plain, ((x, y), cp)
            assert cw == exp_weighted, ((x, y), cw)
            assert np_count == nw_count == exp_count, ((x, y), np_count,
                                                       nw_count)
            assert isinstance(np_count, int) and isinstance(nw_count, int)
    finally:
        os.unlink(path)


def test_total_cost_crossings_stay_an_unweighted_int():
    """The pass banner prints stats['crossings'] with no format spec, so it
    has to stay an int and stay unweighted; only 'total' takes the weight."""
    path = _crossing_board()
    try:
        plain = _state(path).total_cost()
        weighted = _state(path, {NF: 3.0}).total_cost()
        assert isinstance(plain['crossings'], int)
        assert isinstance(weighted['crossings'], int)
        assert weighted['crossings'] == plain['crossings'] == 1
        assert plain['total'] == 30.0, plain
        assert weighted['total'] == 90.0, weighted
    finally:
        os.unlink(path)


def test_weighting_flips_the_optimizer_choice():
    """The point of the fix. Same board, same knobs, only net_weights
    differs: unweighted the seed wins (1 crossing beats 2) and nothing moves;
    weighting NF by 3 makes the seed's single crossing cost 3 and M moves.
    Before the fix both runs returned []."""
    path = _crossing_board()
    try:
        assert quench(parse_kicad_pcb(path), path, **KNOBS) == [], \
            "unweighted, the seed pose is cheapest and nothing should move"
        moved = quench(parse_kicad_pcb(path), path,
                       net_weights={NF: 3.0}, **KNOBS)
        assert moved == [{'reference': 'M', 'new_x': 50.0, 'new_y': 50.0,
                          'new_rotation': 0.0}], moved
    finally:
        os.unlink(path)


def test_all_ones_weights_are_a_no_op_on_a_real_board():
    """Backward compatibility end to end: weighting every net by 1.0 must
    produce the same placement as passing no weights at all. (Run in one process
    for speed; it no longer has to be — quench output stopped depending on the
    hash seed in #457.)"""
    plain = quench(parse_kicad_pcb(INTERF_U), INTERF_U,
                   max_displacement=2.0, step=1.0, max_passes=2)
    pcb = parse_kicad_pcb(INTERF_U)
    ones = quench(pcb, INTERF_U, max_displacement=2.0, step=1.0, max_passes=2,
                  net_weights={nid: 1.0 for nid in pcb.nets})
    assert plain, "fixture should produce at least one improving move"
    assert plain == ones, "all-ones weights must not change the placement"


TESTS = [
    test_unweighted_path_is_bit_identical,
    test_within_halving_is_exact_under_weights,
    test_crossing_weight_is_max_of_the_two_nets,
    test_nets_cost_reports_raw_count_and_weighted_cost,
    test_total_cost_crossings_stay_an_unweighted_int,
    test_weighting_flips_the_optimizer_choice,
    test_all_ones_weights_are_a_no_op_on_a_real_board,
]


if __name__ == '__main__':
    for fn in TESTS:
        fn()
    print("ALL PASS")
