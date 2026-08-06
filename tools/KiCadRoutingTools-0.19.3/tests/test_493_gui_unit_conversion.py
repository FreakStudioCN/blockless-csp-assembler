#!/usr/bin/env python3
"""Regression test for #493 item 3: the GUI's nm -> mm conversion must not
lose a ULP.

`kicad_routing_plugin/swig_gui.py` read the board's net class and design
constraints out of pcbnew with

    nm_to_mm = 1e-6
    clearance = netclass.GetClearance() * nm_to_mm

1e-6 has no exact binary representation, so for some magnitudes that multiply
lands one ULP BELOW the true value:

    200000 * 1e-6 -> 0.19999999999999998   (0.2)
    450000 * 1e-6 -> 0.44999999999999996   (0.45)

while 0.3 / 0.25 / 0.127 / 0.09 come out clean -- which is why this only bit
boards whose Default net class used 0.2 / 0.45. Those epsilons flowed straight
into the routing engines as `clearance` / `via_size` (the GUI clamps its entered
value to the board's Default class in `_effective_geometry_floor`), so the GUI
routed, poured and repaired against constraints a hair different from the CLI's
and produced different copper -- 30 segments of it on nano_eeprom_prog's plane
repair, under an otherwise green grade.

`nm / 1e6` is correctly rounded and reproduces the literal exactly.
`pcbnew.ToMM` already divides, so it was never affected.

This test needs neither wx nor pcbnew: it checks the arithmetic property and
statically guards the plugin sources against the lossy form coming back.

Run:  python3 tests/test_493_gui_unit_conversion.py
"""
import ast
import glob
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
PLUGIN_DIR = os.path.join(ROOT_DIR, 'kicad_routing_plugin')

failures = []

# Values a real board's Default net class actually carries.
NM_CASES = [(200000, 0.2), (450000, 0.45), (300000, 0.3), (250000, 0.25),
            (127000, 0.127), (90000, 0.09), (762000, 0.762), (100000, 0.1)]


def test_divide_is_exact_multiply_is_not():
    print("1. nm / 1e6 reproduces the literal; nm * 1e-6 does not")
    lossy = []
    for nm, mm in NM_CASES:
        if nm / 1e6 != mm:
            failures.append(f"nm/1e6 wrong for {nm}: {nm / 1e6!r} != {mm!r}")
            print(f"  FAIL {nm}/1e6 = {nm / 1e6!r}, want {mm!r}")
        else:
            print(f"  ok   {nm}/1e6 == {mm}")
        if nm * 1e-6 != mm:
            lossy.append((nm, mm, nm * 1e-6))
    # Guard the premise: if a future Python made the multiply exact this test
    # would silently stop testing anything.
    if not lossy:
        failures.append("premise broken: nm*1e-6 was exact for every case")
        print("  FAIL the multiply is exact everywhere -- premise no longer holds")
    else:
        print(f"  ok   the multiply IS lossy for {len(lossy)} case(s), e.g. "
              f"{lossy[0][0]}*1e-6 = {lossy[0][2]!r} != {lossy[0][1]!r}")


def test_plugin_sources_do_not_multiply_by_1e_minus_6():
    """Static guard: no plugin module may convert nm->mm by multiplying.

    AST-based, not regex: the prose in this file and in swig_gui's own comments
    names the lossy form on purpose, and a text scan flags those.
    """
    print("2. no kicad_routing_plugin module multiplies by 1e-6")

    def _is_lossy_factor(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            return node.value == 1e-6
        return isinstance(node, ast.Name) and node.id == 'nm_to_mm'

    hits = []
    for path in sorted(glob.glob(os.path.join(PLUGIN_DIR, '*.py'))):
        tree = ast.parse(open(path, encoding='utf-8').read(), filename=path)
        for node in ast.walk(tree):
            if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult)
                    and (_is_lossy_factor(node.left)
                         or _is_lossy_factor(node.right))):
                hits.append(f"{os.path.relpath(path, ROOT_DIR)}:{node.lineno}")
            # `nm_to_mm = 1e-6` itself, even if the multiply moved elsewhere.
            if (isinstance(node, ast.Assign) and _is_lossy_factor(node.value)
                    and any(isinstance(t, ast.Name) and t.id == 'nm_to_mm'
                            for t in node.targets)):
                hits.append(f"{os.path.relpath(path, ROOT_DIR)}:{node.lineno}"
                            f" (nm_to_mm = 1e-6)")
    if hits:
        failures.append(f"lossy nm->mm multiply reintroduced ({len(hits)})")
        print(f"  FAIL {len(hits)} lossy conversion(s):")
        for h in hits:
            print("    " + h)
    else:
        print("  ok   none found")


def test_helper_divides():
    """The shared helper exists and divides (source-level; importing swig_gui
    would require wx + pcbnew, which the plain suite does not have)."""
    print("3. swig_gui._nm_to_mm exists and divides by 1e6")
    src = open(os.path.join(PLUGIN_DIR, 'swig_gui.py'), encoding='utf-8').read()
    if 'def _nm_to_mm(' not in src:
        failures.append("swig_gui._nm_to_mm helper missing")
        print("  FAIL helper not found")
        return
    body = src.split('def _nm_to_mm(', 1)[1].split('\ndef ', 1)[0]
    if '/ 1e6' not in body:
        failures.append("swig_gui._nm_to_mm does not divide by 1e6")
        print("  FAIL helper does not divide by 1e6")
    else:
        print("  ok   helper divides")
    # And it is actually used for the netclass / constraint reads.
    for needle in ("_nm_to_mm(netclass.GetClearance())",
                   "_nm_to_mm(netclass.GetViaDiameter())",
                   "_nm_to_mm(ds.m_MinClearance)"):
        if needle in src:
            print(f"  ok   used: {needle}")
        else:
            failures.append(f"helper not used at: {needle}")
            print(f"  FAIL not used: {needle}")


def test_plugin_sources_do_not_use_FromMM():
    """Static guard: no plugin module may convert mm->IU with pcbnew.FromMM.

    FromMM TRUNCATES. Measured on KiCad 10: FromMM(1.001) = 1000999 where the
    correct value is 1001000 -- 24 of 2010 swept floor values come out 1 nm low,
    all of them >= 1mm. kicad_parser.mm_to_iu rounds and is the project's
    canonical mm->IU conversion.

    Found in gui_utils.update_live_drc_floors, which stamped every live DRC
    floor (including the copper-to-edge pin) through FromMM. Sub-millimetre
    floors are unaffected, so no current corpus board shows it -- but a board
    declaring e.g. a 1.27mm (50 mil) edge clearance would have its floor written
    1 nm BELOW the copper that was routed to it, which is precisely the
    phantom-violation class #493 exists to prevent.

    The nm->mm guard above catches the other direction; this catches this one.
    """
    print("4. no kicad_routing_plugin module converts mm->IU with FromMM")
    bad = []
    for path in sorted(glob.glob(os.path.join(PLUGIN_DIR, '*.py'))):
        try:
            tree = ast.parse(open(path, encoding='utf-8').read())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == 'FromMM'):
                bad.append(f"{os.path.basename(path)}:{node.lineno}")
    if bad:
        for b in bad:
            print(f"  FAIL pcbnew.FromMM at {b} -- use kicad_parser.mm_to_iu")
        failures.append(f"{len(bad)} FromMM call(s) in the plugin: {bad}")
    else:
        print("  ok   none")


def main():
    for t in (test_divide_is_exact_multiply_is_not,
              test_plugin_sources_do_not_multiply_by_1e_minus_6,
              test_helper_divides,
              test_plugin_sources_do_not_use_FromMM):
        t()
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print("  " + f)
        return 1
    print("PASS: GUI nm->mm conversion is exact (#493 item 3)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
