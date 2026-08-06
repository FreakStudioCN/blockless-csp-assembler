"""
Tests for quench rotation semantics (issue #458).

--no-rotate gated the nudge phase's rotation candidates and nothing else. A
same-footprint swap exchanges FULL poses, rotation included, so a 0-degree
part could still come out at 90 degrees under a flag documented as "disable
rotation moves". Swaps are now restricted to pairs that already share a
rotation when rotations are off, which also keeps the exchange
rotation-neutral: with identical rot-0 courtyards each part's rectangle is
then a pure translate of the other's, so the occupied-space invariance that
lets swaps skip candidate_valid is strengthened rather than weakened.

The discriminating pair is test_no_rotate_blocks_a_rotation_exchanging_swap
and test_no_rotate_still_allows_equal_rotation_swaps: same board, same
knobs, same improving swap, differing only in the two parts' seed angles.
"""

import math
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kicad_parser import parse_kicad_pcb
from placement.quench import (QuenchState, ROTATIONS, _candidate_rotations,
                              _rotate_local_bounds, quench)

INTERF_U = os.path.join(os.path.dirname(__file__), '..', 'kicad_files',
                        'interf_u_unrouted.kicad_pcb')


def _cap_footprint(ref, x, y, net_id, net_name, rot=0.0):
    """Movable 2-pad part at an explicit rotation; pad 1 carries the net.

    Square pads on purpose. For a footprint without a courtyard the local
    bbox is derived from the pads, so oblong pads at different footprint
    rotations can produce different rot-0 bounds, and the swap identity guard
    would then reject the pair before any of this code runs, making every
    assertion below vacuously true."""
    return f'''\t(footprint "test:CAP2P"
\t\t(layer "F.Cu")
\t\t(uuid "fp-{ref}")
\t\t(at {x} {y} {rot})
\t\t(property "Reference" "{ref}"
\t\t\t(at 0 0)
\t\t)
\t\t(pad "1" smd rect
\t\t\t(at -0.5 0 {rot})
\t\t\t(size 0.8 0.8)
\t\t\t(layers "F.Cu")
\t\t\t(net {net_id} "{net_name}")
\t\t\t(uuid "p1-{ref}")
\t\t)
\t\t(pad "2" smd rect
\t\t\t(at 0.5 0 {rot})
\t\t\t(size 0.8 0.8)
\t\t\t(layers "F.Cu")
\t\t\t(uuid "p2-{ref}")
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


def _swap_board(d, c1_rot=0.0, c2_rot=90.0):
    """Two movable same-footprint parts, C1@(150,100) on net NA and
    C2@(150+d,100) on net NB, with locked anchors J1 (net NA) to the right of
    both and J2 (net NB) to the left. C1's net pulls it right and C2's pulls
    it left, so swapping them shortens both airwires by d."""
    body = '(kicad_pcb\n\t(version 20241229)\n'
    body += '\t(net 0 "")\n\t(net 1 "NA")\n\t(net 2 "NB")\n'
    body += '''\t(gr_rect
\t\t(start 100 80)
\t\t(end 200 120)
\t\t(stroke
\t\t\t(width 0.1)
\t\t\t(type solid)
\t\t)
\t\t(layer "Edge.Cuts")
\t\t(uuid "edge1")
\t)
'''
    body += _cap_footprint('C1', 150.0, 100.0, 1, 'NA', c1_rot)
    body += _cap_footprint('C2', 150.0 + d, 100.0, 2, 'NB', c2_rot)
    body += _anchor_footprint('J1', 150.0 + d + 20.0, 100.0, 1, 'NA')
    body += _anchor_footprint('J2', 130.0, 100.0, 2, 'NB')
    body += ')\n'
    fd, path = tempfile.mkstemp(suffix='.kicad_pcb')
    with os.fdopen(fd, 'w') as f:
        f.write(body)
    return path


# step=1000 makes the nudge grid degenerate to the seed point, so any change
# of position comes from the swap block.
SWAP_ONLY = dict(step=1000.0, max_passes=2)


def test_rotation_exchanging_swap_fires_when_rotations_are_allowed():
    """Anti-vacuity for the pair below, and the behaviour --no-rotate has to
    suppress: with rotations allowed the mixed-angle pair does swap, so the
    swap really is improving, in cap, and past the identity guard."""
    path = _swap_board(3, c1_rot=0.0, c2_rot=90.0)
    try:
        result = quench(parse_kicad_pcb(path), path, max_displacement=5.0,
                        allow_rotations=True, **SWAP_ONLY)
        by_ref = {r['reference']: r for r in result}
        assert set(by_ref) == {'C1', 'C2'}, f"fixture is vacuous: {result}"
        assert by_ref['C1']['new_x'] == 153.0, result
        assert by_ref['C2']['new_x'] == 150.0, result
    finally:
        os.unlink(path)


def test_no_rotate_blocks_a_rotation_exchanging_swap():
    """The fix: the same improving, in-cap swap is refused under --no-rotate,
    because accepting it would hand each part the other's rotation."""
    path = _swap_board(3, c1_rot=0.0, c2_rot=90.0)
    try:
        result = quench(parse_kicad_pcb(path), path, max_displacement=5.0,
                        allow_rotations=False, **SWAP_ONLY)
        assert result == [], \
            f"--no-rotate must not accept a swap that rotates both parts: " \
            f"{result}"
    finally:
        os.unlink(path)


def test_no_rotate_still_allows_equal_rotation_swaps():
    """--no-rotate is not a blanket swap kill. A same-angle pair still swaps,
    and the exchange is rotation-neutral, which is why it remains safe to
    skip candidate_valid: the occupied rectangles are unchanged."""
    path = _swap_board(3, c1_rot=90.0, c2_rot=90.0)
    try:
        result = quench(parse_kicad_pcb(path), path, max_displacement=5.0,
                        allow_rotations=False, **SWAP_ONLY)
        by_ref = {r['reference']: r for r in result}
        assert set(by_ref) == {'C1', 'C2'}, result
        assert by_ref['C1']['new_x'] == 153.0, result
        assert by_ref['C2']['new_x'] == 150.0, result
        assert by_ref['C1']['new_rotation'] == 90.0, result
        assert by_ref['C2']['new_rotation'] == 90.0, result
    finally:
        os.unlink(path)


def test_no_rotate_changes_no_rotation_on_a_real_board():
    """Contract check on a real board: a full --no-rotate run, nudges and
    swaps together, returns no part at an angle other than its seed. This one
    is a contract test, not a bug reproduction: whether it would have failed
    before the fix depends on whether a mixed-angle swap happens to be
    improving on this particular board."""
    pcb = parse_kicad_pcb(INTERF_U)
    seed = {ref: fp.rotation % 360 for ref, fp in pcb.footprints.items()}
    result = quench(pcb, INTERF_U, max_displacement=3.0, step=1.0,
                    max_passes=3, allow_rotations=False)
    assert result, "fixture should still produce improving moves"
    for placement in result:
        ref = placement['reference']
        assert placement['new_rotation'] == seed[ref], \
            f"{ref} rotated from {seed[ref]} to " \
            f"{placement['new_rotation']} under --no-rotate"


# --- the rotation lattice (issue #458 item 4b) -------------------------
#
# Rotation candidates used to be the four axis rotations, offered only when
# the part's SEED rotation was a multiple of 90. So a part placed at 45 could
# never rotate at all, and one that inherited 45 from a swap partner could
# only move by snapping back onto a multiple of 90. Candidates are now the
# lattice through the part's own current angle (plus its seed's lattice when
# a swap moved it off), which leaves every orthogonal board untouched.

def _rot_part_footprint(ref, x, y, rot, net_id=0, net_name=''):
    """Movable 2-pad part with square pads at local (+-3, 0); only pad 1
    carries a net, so the airwire endpoint is the part's own angle made
    visible."""
    net = f'\n\t\t\t(net {net_id} "{net_name}")' if net_id else ''
    return f'''\t(footprint "test:ROT2P"
\t\t(layer "F.Cu")
\t\t(uuid "fp-{ref}")
\t\t(at {x} {y} {rot})
\t\t(property "Reference" "{ref}"
\t\t\t(at 0 0)
\t\t)
\t\t(pad "1" smd rect
\t\t\t(at 3 0 {rot})
\t\t\t(size 0.8 0.8)
\t\t\t(layers "F.Cu"){net}
\t\t\t(uuid "p1-{ref}")
\t\t)
\t\t(pad "2" smd rect
\t\t\t(at -3 0 {rot})
\t\t\t(size 0.8 0.8)
\t\t\t(layers "F.Cu")
\t\t\t(uuid "p2-{ref}")
\t\t)
\t)
'''


def _rot45_board(anchor_x, anchor_y, seed_rot=45.0):
    """One movable part seeded at `seed_rot` at (150,100) with its net anchor
    at (anchor_x, anchor_y). With the nudge grid frozen the only move
    available is a rotation."""
    body = '(kicad_pcb\n\t(version 20241229)\n\t(net 0 "")\n\t(net 1 "NA")\n'
    body += '''\t(gr_rect
\t\t(start 100 80)
\t\t(end 200 120)
\t\t(stroke
\t\t\t(width 0.1)
\t\t\t(type solid)
\t\t)
\t\t(layer "Edge.Cuts")
\t\t(uuid "edge1")
\t)
'''
    body += _rot_part_footprint('R1', 150.0, 100.0, seed_rot, 1, 'NA')
    body += _anchor_footprint('J1', anchor_x, anchor_y, 1, 'NA')
    body += ')\n'
    fd, path = tempfile.mkstemp(suffix='.kicad_pcb')
    with os.fdopen(fd, 'w') as f:
        f.write(body)
    return path


def _state(path, grid_step=0.1):
    """QuenchState with every geometry weight zeroed, so the only cost that
    moves is airwire length."""
    return QuenchState(parse_kicad_pcb(path), path, 0.25, 0.55, 10.0,
                       0.0, 0.0, 0.0, 0.0, 0.0, grid_step, 1.0)


def test_candidate_rotations_are_unchanged_for_orthogonal_parts():
    """The byte-identity guard, mechanized. For a part at a multiple of 90
    the candidate list is ROTATIONS itself: same values, and the SAME ORDER,
    because the caller keeps the first strict minimum, so the order decides
    ties."""
    for rot in ROTATIONS:
        part = SimpleNamespace(rot=rot, orig_rot=rot)
        assert _candidate_rotations(part, True) == ROTATIONS, rot
        assert _candidate_rotations(part, False) == [rot], rot


def test_candidate_rotations_on_a_45_degree_lattice():
    """A 45-degree part rotates on its own lattice instead of not at all, and
    a part that a swap moved off its seed lattice keeps both, current first,
    so nothing reachable before becomes unreachable."""
    p = SimpleNamespace(rot=45.0, orig_rot=45.0)
    assert _candidate_rotations(p, True) == [45.0, 135.0, 225.0, 315.0]
    p = SimpleNamespace(rot=45.0, orig_rot=0.0)
    assert _candidate_rotations(p, True) == [45.0, 135.0, 225.0, 315.0,
                                             0.0, 90.0, 180.0, 270.0]
    p = SimpleNamespace(rot=0.0, orig_rot=45.0)
    assert _candidate_rotations(p, True) == [0.0, 90.0, 180.0, 270.0,
                                             45.0, 135.0, 225.0, 315.0]


def test_every_part_on_an_orthogonal_board_keeps_the_old_candidate_list():
    """The claim "no change on a board with only 90-degree rotations",
    checked against a real 25-part board rather than argued."""
    state = QuenchState(parse_kicad_pcb(INTERF_U), INTERF_U, 0.25, 0.55,
                        10.0, 0.5, 0.25, 2.0, 2.0, 2.0, 0.1)
    assert state.parts, "board should have parts"
    for ref, part in state.parts.items():
        assert _candidate_rotations(part, True) == ROTATIONS, ref


def test_part_records_the_whole_lattice_for_a_non_orthogonal_seed():
    """White box: a 45-degree seed brings 135/225/315 too. That is what keeps
    build_neighbor_lists' group closure covering every reachable pose without
    the closure code itself having to change."""
    path = _rot45_board(150.0, 100.0)
    try:
        part = _state(path).parts['R1']
        assert set(part.bounds_by_rot) == {0.0, 45.0, 90.0, 135.0,
                                           180.0, 225.0, 270.0, 315.0}
        lb = part.bounds_by_rot[0.0]
        for r in (135.0, 225.0, 315.0):
            assert part.bounds_by_rot[r] == _rotate_local_bounds(*lb, r), r
    finally:
        os.unlink(path)


def test_45_seeded_part_rotates_on_its_own_lattice():
    """Behaviour: with the nudge grid frozen and swaps off, the only move
    available is a rotation. The part rotates to 135, which is where its
    anchor is. Before the fix it could not rotate at all, and it must never
    be snapped onto a multiple of 90."""
    path = _rot45_board(120.0, 90.0)
    try:
        result = quench(parse_kicad_pcb(path), path, max_displacement=5.0,
                        step=1000.0, grid_step=0.1, length_weight=1.0,
                        crossing_penalty=10.0, halo_base=0.0, halo_coef=0.0,
                        halo_weight=0.0, edge_halo=0.0, edge_weight=0.0,
                        allow_swaps=False, max_passes=2)
        assert [(r['reference'], r['new_rotation']) for r in result] == \
            [('R1', 135.0)], result
    finally:
        os.unlink(path)


def _lattice_board(dy, seed_rot=45.0):
    """A movable part with an OFF-CENTRE courtyard, local (-1,-3,3,3), seeded
    at `seed_rot`, plus a small movable part `dy` below it. Off-centre
    matters: for the origin-symmetric courtyards of ordinary passives the
    45-degree lattice boxes are all congruent, so only an asymmetric one can
    tell the lattice closure apart from the seed-only closure."""
    pads = ''
    for i, (px, py) in enumerate(((-0.5, -2.5), (-0.5, 2.5),
                                  (2.5, -2.5), (2.5, 2.5)), 1):
        net = '\n\t\t\t(net 1 "NA")' if i == 1 else ''
        pads += f'''\t\t(pad "{i}" smd rect
\t\t\t(at {px} {py} {seed_rot})
\t\t\t(size 1 1)
\t\t\t(layers "F.Cu"){net}
\t\t\t(uuid "p{i}-R1")
\t\t)
'''
    body = '(kicad_pcb\n\t(version 20241229)\n\t(net 0 "")\n\t(net 1 "NA")\n'
    body += '''\t(gr_rect
\t\t(start 100 80)
\t\t(end 200 140)
\t\t(stroke
\t\t\t(width 0.1)
\t\t\t(type solid)
\t\t)
\t\t(layer "Edge.Cuts")
\t\t(uuid "edge1")
\t)
'''
    body += (f'\t(footprint "test:BIG4P"\n\t\t(layer "F.Cu")\n'
             f'\t\t(uuid "fp-R1")\n\t\t(at 150 100 {seed_rot})\n'
             f'\t\t(property "Reference" "R1"\n\t\t\t(at 0 0)\n\t\t)\n'
             + pads + '\t)\n')
    body += f'''\t(footprint "test:SMALL2P"
\t\t(layer "F.Cu")
\t\t(uuid "fp-R2")
\t\t(at 150 {100.0 + dy} 0)
\t\t(property "Reference" "R2"
\t\t\t(at 0 0)
\t\t)
\t\t(pad "1" smd rect
\t\t\t(at -0.5 0 0)
\t\t\t(size 0.6 0.8)
\t\t\t(layers "F.Cu")
\t\t\t(net 1 "NA")
\t\t\t(uuid "p1-R2")
\t\t)
\t\t(pad "2" smd rect
\t\t\t(at 0.5 0 0)
\t\t\t(size 0.6 0.8)
\t\t\t(layers "F.Cu")
\t\t\t(uuid "p2-R2")
\t\t)
\t)
)
'''
    fd, path = tempfile.mkstemp(suffix='.kicad_pcb')
    with os.fdopen(fd, 'w') as f:
        f.write(body)
    return path


def test_neighbor_lists_cover_the_relative_rotation_lattice():
    """The pruning boxes have to cover every pose the candidate list offers,
    not just the seed's own box. Since a 45-seeded part can now reach 225,
    and 225's box on an off-centre courtyard sticks out further than the
    seed-only closure, a neighbour between the two reaches must survive
    pruning. This is a new-invariant test, not an old-bug reproduction: it
    exists so _Part's eager lattice and _candidate_rotations cannot drift
    apart later, and the failure mode if they do is silent, because rect()
    falls back to rot-0 bounds instead of raising."""
    budget, clearance = 1.0, 0.25
    dy = 6.6
    path = _lattice_board(dy)
    try:
        state = _state(path)
        r1, r2 = state.parts['R1'], state.parts['R2']
        assert r1.halo == 0.0 and r2.halo == 0.0, "knobs must zero the halos"
        lb = r1.bounds_by_rot[0.0]
        assert lb == (-1.0, -3.0, 3.0, 3.0), lb

        def reach(rots):
            """How far below its seed R1's union box extends, plus the
            interaction margin, minus R2's own upward extent: the largest dy
            at which R2 can still be a neighbour."""
            top = max(_rotate_local_bounds(*lb, r)[3] for r in rots)
            margin = budget + budget + max(clearance, r1.halo + r2.halo)
            return top + margin - min(
                _rotate_local_bounds(*r2.bounds_by_rot[0.0], r)[1]
                for r in ROTATIONS)

        seed_only = reach(list(ROTATIONS) + [45.0])
        lattice = reach(list(ROTATIONS) + [45.0, 135.0, 225.0, 315.0])
        assert seed_only < dy < lattice, (seed_only, dy, lattice)

        state.build_neighbor_lists(budget)
        assert 'R2' in state._neighbors['R1'], \
            f"pruning must keep a neighbour the 225-degree pose can reach: " \
            f"{state._neighbors}"
    finally:
        os.unlink(path)


TESTS = [
    test_rotation_exchanging_swap_fires_when_rotations_are_allowed,
    test_no_rotate_blocks_a_rotation_exchanging_swap,
    test_no_rotate_still_allows_equal_rotation_swaps,
    test_no_rotate_changes_no_rotation_on_a_real_board,
    test_candidate_rotations_are_unchanged_for_orthogonal_parts,
    test_candidate_rotations_on_a_45_degree_lattice,
    test_every_part_on_an_orthogonal_board_keeps_the_old_candidate_list,
    test_part_records_the_whole_lattice_for_a_non_orthogonal_seed,
    test_45_seeded_part_rotates_on_its_own_lattice,
    test_neighbor_lists_cover_the_relative_rotation_lattice,
]


if __name__ == '__main__':
    for fn in TESTS:
        fn()
    print("ALL PASS")
