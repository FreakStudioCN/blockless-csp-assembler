"""
Tests for the courtyard extraction rewrite (issue #456 item 3).

The old fp_line/fp_rect patterns used DOTALL with a bare lazy `.*?` gap up to
`(layer "[FB].CrtYd")`, so a match could START on a preceding silk element and
BORROW a later courtyard element's layer tag -- dragging silk coordinates into
the courtyard bbox. With the standard element order (silk, courtyard, fab) a
0402 whose true courtyard is (-0.5,-0.4)-(0.5,0.4) read as (-2.0,-0.9)-(2.0,0.4).
Usually that only INFLATES a part (spurious clearance rejections, phantom halo
cost), but on a non-rectangular courtyard it can lose an extreme and
UNDER-estimate, letting real overlap validate.

Courtyards drawn with fp_poly / fp_arc / fp_circle were not handled at all and
fell through silently to the pad bounding box, which carries no courtyard margin.

The fix mirrors kicad_parser._footprint_edge_points: a string-aware paren scan
(find_matching_paren, issue #113), an element gap that cannot cross into the next
element (_FP_ELEMENT_GAP, the footprint twin of _GR_ELEMENT_GAP from #77), and
every shape kind. The real-board expectations below were read out of the board
files by hand and are the values KiCad itself draws.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from placement.parser import (extract_courtyard_bboxes, extract_courtyard_sides,
                              extract_locked_refs, courtyard_for_side)

KICAD_FILES = os.path.join(os.path.dirname(__file__), '..', 'kicad_files')


def _board(footprints):
    """Write a minimal board carrying `footprints` text; returns the path."""
    body = ('(kicad_pcb\n\t(version 20241229)\n\t(net 0 "")\n'
            '\t(gr_rect\n\t\t(start 0 0)\n\t\t(end 100 100)\n'
            '\t\t(layer "Edge.Cuts")\n\t\t(uuid "e1")\n\t)\n'
            + footprints + ')\n')
    fd, path = tempfile.mkstemp(suffix='.kicad_pcb')
    with os.fdopen(fd, 'w') as f:
        f.write(body)
    return path


def _fp(ref, elements, layer='F.Cu', extra=''):
    return f'''\t(footprint "test:FP"
\t\t(layer "{layer}")
{extra}\t\t(uuid "fp-{ref}")
\t\t(at 50 50)
\t\t(property "Reference" "{ref}"
\t\t\t(at 0 0)
\t\t)
{elements}\t\t(pad "1" smd rect
\t\t\t(at 0 0)
\t\t\t(size 0.6 0.8)
\t\t\t(layers "{layer}")
\t\t\t(uuid "p1-{ref}")
\t\t)
\t)
'''


def _approx(got, want, tol=1e-6):
    return got is not None and all(abs(a - b) <= tol for a, b in zip(got, want))


# --- item 3: the silk-bleed bug ---------------------------------------------

# The issue's own reproduction: a SILK line wider and higher than the courtyard,
# written BEFORE it (KiCad's standard element order), then the real courtyard.
SILK_THEN_CRTYD = '''\t\t(fp_line
\t\t\t(start -2.0 -0.9)
\t\t\t(end 2.0 -0.9)
\t\t\t(stroke
\t\t\t\t(width 0.12)
\t\t\t\t(type solid)
\t\t\t)
\t\t\t(layer "F.SilkS")
\t\t\t(uuid "silk1")
\t\t)
\t\t(fp_line
\t\t\t(start -0.5 -0.4)
\t\t\t(end 0.5 -0.4)
\t\t\t(layer "F.CrtYd")
\t\t\t(uuid "cy1")
\t\t)
\t\t(fp_line
\t\t\t(start -0.5 0.4)
\t\t\t(end 0.5 0.4)
\t\t\t(layer "F.CrtYd")
\t\t\t(uuid "cy2")
\t\t)
'''


def test_silk_before_courtyard_does_not_bleed():
    """The silk element must not lend its coordinates to the courtyard bbox."""
    path = _board(_fp('C1', SILK_THEN_CRTYD))
    cy = extract_courtyard_bboxes(path)
    assert 'C1' in cy, "courtyard not found at all"
    assert _approx(cy['C1'], (-0.5, -0.4, 0.5, 0.4)), (
        f"silk bled into the courtyard: got {cy['C1']}, "
        f"want (-0.5, -0.4, 0.5, 0.4)")
    os.unlink(path)


def test_fab_layer_after_courtyard_does_not_bleed():
    """The gap must not run BACKWARD either: a courtyard line must not borrow a
    later fab line's coordinates by matching across it."""
    elements = SILK_THEN_CRTYD + '''\t\t(fp_line
\t\t\t(start -5.0 -5.0)
\t\t\t(end 5.0 5.0)
\t\t\t(layer "F.Fab")
\t\t\t(uuid "fab1")
\t\t)
'''
    path = _board(_fp('C1', elements))
    cy = extract_courtyard_bboxes(path)
    assert _approx(cy['C1'], (-0.5, -0.4, 0.5, 0.4)), \
        f"fab element leaked into the courtyard: {cy['C1']}"
    os.unlink(path)


# --- item 3: shapes that were silently dropped ------------------------------

def test_fp_circle_courtyard():
    """A circular courtyard (mounting holes, fiducials) must be read as its
    disc bbox, not dropped to the pad box."""
    elements = '''\t\t(fp_circle
\t\t\t(center 0.5 0)
\t\t\t(end 2.5 0)
\t\t\t(layer "F.CrtYd")
\t\t\t(uuid "cc1")
\t\t)
'''
    path = _board(_fp('H1', elements))
    cy = extract_courtyard_bboxes(path)
    assert _approx(cy.get('H1'), (-1.5, -2.0, 2.5, 2.0)), \
        f"fp_circle courtyard: got {cy.get('H1')}, want (-1.5,-2.0,2.5,2.0)"
    os.unlink(path)


def test_fp_poly_courtyard():
    """A polygon courtyard must be read from its nested (pts (xy ...)) block."""
    elements = '''\t\t(fp_poly
\t\t\t(pts
\t\t\t\t(xy -1.75 -1.5)
\t\t\t\t(xy 1.75 -1.5)
\t\t\t\t(xy 1.75 1.5)
\t\t\t\t(xy -1.75 1.5)
\t\t\t)
\t\t\t(layer "F.CrtYd")
\t\t\t(uuid "cp1")
\t\t)
'''
    path = _board(_fp('Y1', elements))
    cy = extract_courtyard_bboxes(path)
    assert _approx(cy.get('Y1'), (-1.75, -1.5, 1.75, 1.5)), \
        f"fp_poly courtyard: got {cy.get('Y1')}"
    os.unlink(path)


def test_fp_arc_courtyard():
    """An arc courtyard is flattened to segments, so its extreme is kept."""
    elements = '''\t\t(fp_arc
\t\t\t(start -1.0 0)
\t\t\t(mid 0 -1.0)
\t\t\t(end 1.0 0)
\t\t\t(layer "F.CrtYd")
\t\t\t(uuid "ca1")
\t\t)
'''
    path = _board(_fp('U1', elements))
    cy = extract_courtyard_bboxes(path)
    got = cy.get('U1')
    assert got is not None, "fp_arc courtyard dropped"
    # The arc bulges to y = -1.0 at its midpoint; a chord-only read would stop
    # at y = 0 and lose the extreme (the under-estimate failure mode).
    assert got[1] <= -0.99, f"arc bulge lost: {got}"
    assert _approx(got, (-1.0, -1.0, 1.0, 0.0), tol=0.02), f"arc bbox: {got}"
    os.unlink(path)


def test_fp_rect_courtyard_all_corners():
    elements = '''\t\t(fp_rect
\t\t\t(start -1.0 -0.6)
\t\t\t(end 1.0 0.6)
\t\t\t(layer "F.CrtYd")
\t\t\t(uuid "cr1")
\t\t)
'''
    path = _board(_fp('R1', elements))
    cy = extract_courtyard_bboxes(path)
    assert _approx(cy.get('R1'), (-1.0, -0.6, 1.0, 0.6)), f"{cy.get('R1')}"
    os.unlink(path)


# --- per-side extraction -----------------------------------------------------

def test_courtyard_sides_are_kept_apart():
    """F and B courtyards must not be unioned into one untagged box; the union
    view stays available for callers that do not model side."""
    elements = '''\t\t(fp_rect
\t\t\t(start -1.0 -1.0)
\t\t\t(end 1.0 1.0)
\t\t\t(layer "F.CrtYd")
\t\t\t(uuid "cf1")
\t\t)
\t\t(fp_rect
\t\t\t(start -3.0 -0.5)
\t\t\t(end 3.0 0.5)
\t\t\t(layer "B.CrtYd")
\t\t\t(uuid "cb1")
\t\t)
'''
    path = _board(_fp('U2', elements))
    sides = extract_courtyard_sides(path)['U2']
    assert set(sides) == {'F', 'B'}, f"sides not separated: {sides}"
    assert _approx(sides['F'], (-1.0, -1.0, 1.0, 1.0)), f"F: {sides['F']}"
    assert _approx(sides['B'], (-3.0, -0.5, 3.0, 0.5)), f"B: {sides['B']}"
    union = extract_courtyard_bboxes(path)['U2']
    assert _approx(union, (-3.0, -1.0, 3.0, 1.0)), f"union: {union}"
    os.unlink(path)


def test_front_only_footprint_has_no_back_entry():
    path = _board(_fp('C1', SILK_THEN_CRTYD))
    assert set(extract_courtyard_sides(path)['C1']) == {'F'}
    os.unlink(path)


def test_courtyard_for_side_falls_back_to_the_other_side():
    """A part mounted on B whose library drew only F.CrtYd still gets a
    courtyard rather than dropping to the pad box."""
    elements = '''\t\t(fp_rect
\t\t\t(start -1.0 -1.0)
\t\t\t(end 1.0 1.0)
\t\t\t(layer "F.CrtYd")
\t\t\t(uuid "cf1")
\t\t)
'''
    path = _board(_fp('C9', elements, layer='B.Cu'))
    sides = extract_courtyard_sides(path)['C9']
    assert _approx(courtyard_for_side(sides, 'B'), (-1.0, -1.0, 1.0, 1.0))
    assert courtyard_for_side(None, 'B') is None
    os.unlink(path)


# --- #113: the paren scan must be string-aware -------------------------------

def test_paren_in_quoted_property_does_not_swallow_next_footprint():
    """A lone '(' inside a property value made the naive depth counter run past
    the block end and consume the following footprints (issue #113); both
    readers here now use the string-aware find_matching_paren."""
    mpn = '''\t\t(property "MPN" "TCR2EF115,LM(CT"
\t\t\t(at 0 0)
\t\t)
'''
    body = _fp('C1', SILK_THEN_CRTYD, extra=mpn) + _fp('C2', SILK_THEN_CRTYD)
    path = _board(body)
    cy = extract_courtyard_bboxes(path)
    assert set(cy) >= {'C1', 'C2'}, \
        f"unbalanced paren swallowed a footprint: found {sorted(cy)}"
    assert _approx(cy['C2'], (-0.5, -0.4, 0.5, 0.4)), f"C2: {cy['C2']}"
    os.unlink(path)


def test_locked_refs_survive_paren_in_property():
    """The unbalanced footprint comes FIRST and is not locked; the naive scan
    then swallows the locked footprint after it, and its (locked yes) sits past
    the first pad of the merged text, so the lock was lost entirely."""
    mpn = '''\t\t(property "MPN" "WEIRD(NAME"
\t\t\t(at 0 0)
\t\t)
'''
    body = (_fp('C1', SILK_THEN_CRTYD, extra=mpn)
            + _fp('J2', SILK_THEN_CRTYD, extra='\t\t(locked yes)\n'))
    path = _board(body)
    locked = extract_locked_refs(path)
    assert locked == {'J2'}, \
        f"lock lost to the unbalanced paren before it: {locked}"
    os.unlink(path)


# --- real-board regressions --------------------------------------------------
# Each of these was WRONG before the fix. The comments give the old value.

REAL_CASES = [
    # board, ref, expected local courtyard bbox, what was wrong before
    ('kit-dev-coldfire-xilinx_5213.kicad_pcb', 'C212',
     (-1.75, -4.25, 6.75, 4.25),
     'was (2.58,-4.08,2.58,4.08): ZERO WIDTH -- silk bled in and the real '
     'fp_circle courtyard was unsupported'),
    ('rp2350_fpga_eensy_prePlane.kicad_pcb', 'Y1',
     (-1.75, -1.5, 1.75, 1.5),
     'was (-1.71,1.46,1.71,1.46): ZERO HEIGHT -- silk bled in and the real '
     'fp_poly courtyard was unsupported'),
    ('tigard.kicad_pcb', 'J3',
     (-1.8, -1.8, 1.8, 22.1),
     'was (-2.794,-2.794,1.8,22.1): ~1mm of phantom courtyard on two sides'),
]


def test_real_board_courtyards():
    for board, ref, want, why in REAL_CASES:
        path = os.path.join(KICAD_FILES, board)
        if not os.path.exists(path):
            continue
        got = extract_courtyard_bboxes(path).get(ref)
        assert _approx(got, want, tol=1e-3), \
            f"{board} {ref}: got {got}, want {want} ({why})"
        print(f"  PASS: {board} {ref} = {want}")


def test_previously_missing_courtyards_are_found():
    """28 of rp2350's 60 footprints drew their courtyard with fp_poly and were
    absent from the dict entirely, falling back to the pad box."""
    path = os.path.join(KICAD_FILES, 'rp2350_fpga_eensy_prePlane.kicad_pcb')
    if not os.path.exists(path):
        return
    cy = extract_courtyard_bboxes(path)
    assert len(cy) >= 60, f"expected >=60 courtyards on rp2350, got {len(cy)}"
    for ref in ('C1', 'C20', 'R1', 'R9', 'R13', 'C22'):
        assert ref in cy, f"{ref}'s fp_poly courtyard still missing"
    print(f"  PASS: rp2350 courtyards found: {len(cy)}")


def test_no_degenerate_courtyards_on_the_corpus():
    """A zero-width/height courtyard means a real part is modelled as a line --
    the sharpest symptom of the bleed. There must be none left."""
    import glob
    bad = []
    for path in sorted(glob.glob(os.path.join(KICAD_FILES, '*.kicad_pcb'))):
        for ref, b in extract_courtyard_bboxes(path).items():
            if b[2] - b[0] <= 1e-9 or b[3] - b[1] <= 1e-9:
                bad.append((os.path.basename(path), ref, b))
    assert not bad, f"degenerate courtyards: {bad}"
    print("  PASS: no degenerate courtyards across kicad_files/")


TESTS = [
    test_silk_before_courtyard_does_not_bleed,
    test_fab_layer_after_courtyard_does_not_bleed,
    test_fp_circle_courtyard,
    test_fp_poly_courtyard,
    test_fp_arc_courtyard,
    test_fp_rect_courtyard_all_corners,
    test_courtyard_sides_are_kept_apart,
    test_front_only_footprint_has_no_back_entry,
    test_courtyard_for_side_falls_back_to_the_other_side,
    test_paren_in_quoted_property_does_not_swallow_next_footprint,
    test_locked_refs_survive_paren_in_property,
    test_real_board_courtyards,
    test_previously_missing_courtyards_are_found,
    test_no_degenerate_courtyards_on_the_corpus,
]


if __name__ == '__main__':
    for t in TESTS:
        print(f"--- {t.__name__}")
        t()
    print("ALL PASS")
