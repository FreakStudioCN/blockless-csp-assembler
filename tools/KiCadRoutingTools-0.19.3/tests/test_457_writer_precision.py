"""
Tests for placement/writer.py coordinate and angle precision (issue #457 item 2),
plus the string-aware footprint scan.

The writer used to emit footprint positions with `{new_x:.4g}` — FOUR SIGNIFICANT
digits. Past 100mm, which is most of a real board, that quantises placement to
0.1mm: a part moved to x=139.96 was written as "140", a 40um error, and past
1000mm the step becomes 1mm. That silently coarsens the very clearance repair the
writer exists to apply, and can push a part back into the graze it was moved out
of — the placement engine validates a position the file then does not hold.

The code is correct today (`:.6f` coordinates, `:.6g` angles). What was missing is
a gate: nothing asserted the written TEXT, and `_rotate_pad_angles` was called by
no test at all — so its `% 360` wrap, its drop-the-angle-token branch, and its
format were all free to regress. These tests fail if `.4g` ever comes back.

Precision is deliberately asserted on the text and not only on a re-parsed float:
kicad_writer._num_pat matches coordinates by TEXT and has already broken once over
a format change (#369 A9), so the format itself is part of the contract.
"""

import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kicad_parser import parse_kicad_pcb
from placement.writer import _rotate_pad_angles, write_placed_output


def _board(footprints):
    body = ('(kicad_pcb\n\t(version 20241229)\n\t(net 0 "")\n'
            '\t(gr_rect\n\t\t(start 0 0)\n\t\t(end 2000 2000)\n'
            '\t\t(layer "Edge.Cuts")\n\t\t(uuid "e1")\n\t)\n'
            + footprints + ')\n')
    fd, path = tempfile.mkstemp(suffix='.kicad_pcb')
    with os.fdopen(fd, 'w') as f:
        f.write(body)
    return path


def _fp(ref, x, y, rot=None, pad_angle=None, extra=''):
    at = f"(at {x} {y}{'' if rot is None else ' ' + str(rot)})"
    pad_at = f"(at 0.5 0{'' if pad_angle is None else ' ' + str(pad_angle)})"
    return f'''\t(footprint "test:FP"
\t\t(layer "F.Cu")
\t\t(uuid "fp-{ref}")
\t\t{at}
\t\t(property "Reference" "{ref}"
\t\t\t(at 0 0)
\t\t)
{extra}\t\t(pad "1" smd rect
\t\t\t{pad_at}
\t\t\t(size 0.6 0.8)
\t\t\t(layers "F.Cu")
\t\t\t(net 0 "")
\t\t\t(uuid "p1-{ref}")
\t\t)
\t)
'''


def _write(src, placements):
    fd, out = tempfile.mkstemp(suffix='.kicad_pcb')
    os.close(fd)
    write_placed_output(src, out, placements)
    with open(out, encoding='utf-8') as f:
        return out, f.read()


# --- coordinate precision ----------------------------------------------------

def test_coordinate_keeps_six_decimals():
    """The upstream discovery case: x=139.96 must not become '140'."""
    src = _board(_fp('C1', 10.0, 10.0))
    out, text = _write(src, [{'reference': 'C1', 'new_x': 139.96,
                              'new_y': 88.04, 'new_rotation': 0}])
    assert '(at 139.960000 88.040000)' in text, \
        f"coordinate lost precision; wrote: {[l for l in text.splitlines() if '(at ' in l][:4]}"
    assert '(at 140' not in text, "'.4g' rounding is back"
    os.unlink(src)
    os.unlink(out)


def test_large_coordinate_keeps_sub_micron_precision():
    """Past 1000mm, four significant digits quantises to 1mm."""
    src = _board(_fp('C1', 10.0, 10.0))
    out, text = _write(src, [{'reference': 'C1', 'new_x': 1234.5678,
                              'new_y': 1000.0009, 'new_rotation': 0}])
    assert '(at 1234.567800 1000.000900)' in text, \
        "large coordinate lost precision"
    os.unlink(src)
    os.unlink(out)


def test_written_position_round_trips_through_the_parser():
    """The position the placement engine validated is the position on the board."""
    src = _board(_fp('C1', 10.0, 10.0))
    x, y = 139.96, 88.04
    out, _ = _write(src, [{'reference': 'C1', 'new_x': x, 'new_y': y,
                           'new_rotation': 0}])
    fp = parse_kicad_pcb(out).footprints['C1']
    assert abs(fp.x - x) < 1e-6 and abs(fp.y - y) < 1e-6, \
        f"round trip moved the part: ({fp.x}, {fp.y}) vs ({x}, {y})"
    # ... and the error is far below the clearance budget the engine works with.
    assert math.hypot(fp.x - x, fp.y - y) < 1e-6
    os.unlink(src)
    os.unlink(out)


def test_rotation_keeps_fractional_degrees():
    src = _board(_fp('C1', 10.0, 10.0))
    out, text = _write(src, [{'reference': 'C1', 'new_x': 20.0, 'new_y': 20.0,
                              'new_rotation': 137.25}])
    assert '137.25' in text, "fractional rotation lost"
    assert '(at 20.000000 20.000000 137.25)' in text, \
        f"unexpected at-line: {[l for l in text.splitlines() if '(at 20' in l]}"
    os.unlink(src)
    os.unlink(out)


# --- _rotate_pad_angles: previously called by NO test ------------------------

PAD_FP = _fp('U1', 10.0, 10.0, rot=90, pad_angle=90)


def test_rotate_pad_angles_keeps_fractional_degrees():
    """90 + 47.25 = 137.25; '.4g' renders that as 137.2, a 0.05deg error."""
    got = _rotate_pad_angles(PAD_FP, 47.25)
    assert '(at 0.5 0 137.25)' in got, f"pad angle lost precision: {got!r}"


def test_rotate_pad_angles_wraps_at_360():
    got = _rotate_pad_angles(_fp('U1', 0, 0, rot=350, pad_angle=350), 20.0)
    assert '(at 0.5 0 10)' in got, f"expected wrap to 10deg: {got!r}"


def test_rotate_pad_angles_drops_a_zero_angle_token():
    """270 + 90 = 360 -> 0, and KiCad omits a zero pad angle entirely."""
    got = _rotate_pad_angles(_fp('U1', 0, 0, rot=270, pad_angle=270), 90.0)
    assert '(at 0.5 0)' in got, f"zero angle should drop the token: {got!r}"
    assert '(at 0.5 0 0)' not in got and '(at 0.5 0 360)' not in got


def test_rotate_pad_angles_leaves_pad_xy_untouched():
    """x/y pass through as regex text; only the angle is rewritten."""
    got = _rotate_pad_angles(_fp('U1', 0, 0, rot=0, pad_angle=None), 45.0)
    assert '(at 0.5 0 45)' in got, f"{got!r}"


def test_footprint_rotation_reaches_the_pads_end_to_end():
    """KiCad stores pad angle as footprint + pad-local, so rotating a footprint
    must add the delta to every pad."""
    src = _board(_fp('U1', 10.0, 10.0, rot=0, pad_angle=None))
    out, text = _write(src, [{'reference': 'U1', 'new_x': 10.0, 'new_y': 10.0,
                              'new_rotation': 47.25}])
    assert '(at 0.5 0 47.25)' in text, \
        f"pad angle not updated: {[l for l in text.splitlines() if '(at 0.5' in l]}"
    os.unlink(src)
    os.unlink(out)


# --- #113: the footprint scan must be string-aware ---------------------------

def test_paren_in_property_does_not_bleed_a_rotation_into_the_next_footprint():
    """A lone '(' in a property value makes a naive depth counter run past the
    block end and swallow the FOLLOWING footprint (issue #113).

    The footprint's own `(at ...)` still gets rewritten correctly — the regex
    finds it first — so a position-only placement hides the bug. The damage
    shows when the placement carries a ROTATION: `_rotate_pad_angles` then runs
    over the merged text and rewrites the next footprint's pad angles too,
    silently rotating a part nobody asked to move.
    """
    mpn = '\t\t(property "MPN" "TCR2EF115,LM(CT"\n\t\t\t(at 0 0)\n\t\t)\n'
    src = _board(_fp('C1', 10.0, 10.0, rot=0, pad_angle=None, extra=mpn)
                 + _fp('C2', 20.0, 20.0, rot=0, pad_angle=None))
    out, text = _write(src, [{'reference': 'C1', 'new_x': 10.0, 'new_y': 10.0,
                              'new_rotation': 90}])
    # Split on the second footprint block, so each half holds exactly one part.
    split = text.index('(footprint', text.index('"C1"'))
    c1_block, c2_block = text[:split], text[split:]
    assert '"C1"' in c1_block and '"C2"' in c2_block, "fixture split is wrong"
    # C1's own pad must rotate ...
    assert '(at 0.5 0 90)' in c1_block, f"C1's pad not rotated: {c1_block!r}"
    # ... and C2's must NOT.
    assert '(at 0.5 0)' in c2_block and '(at 0.5 0 90)' not in c2_block, \
        f"rotation bled into C2's pads through the unbalanced paren: {c2_block!r}"
    pcb = parse_kicad_pcb(out)
    assert abs(pcb.footprints['C2'].x - 20.0) < 1e-6, "C2 moved"
    os.unlink(src)
    os.unlink(out)


TESTS = [
    test_coordinate_keeps_six_decimals,
    test_large_coordinate_keeps_sub_micron_precision,
    test_written_position_round_trips_through_the_parser,
    test_rotation_keeps_fractional_degrees,
    test_rotate_pad_angles_keeps_fractional_degrees,
    test_rotate_pad_angles_wraps_at_360,
    test_rotate_pad_angles_drops_a_zero_angle_token,
    test_rotate_pad_angles_leaves_pad_xy_untouched,
    test_footprint_rotation_reaches_the_pads_end_to_end,
    test_paren_in_property_does_not_bleed_a_rotation_into_the_next_footprint,
]


if __name__ == '__main__':
    for t in TESTS:
        print(f"--- {t.__name__}")
        t()
    print("ALL PASS")
