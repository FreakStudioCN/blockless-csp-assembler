#!/usr/bin/env python3
"""Unit tests for kicad_dru (#498): the parsable subset of .kicad_dru
layer-clearance rules, precedence, scope forms, units, and skip notes."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kicad_dru import (parse_dru_layer_clearances, read_board_layer_clearances)

L4 = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
passed = failed = 0


def check(name, got, want):
    global passed, failed
    ok = got == want
    passed += ok
    failed += not ok
    print(f"  {'OK  ' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" (want {want!r})"))


# 1. layer clause forms: named / outer / inner
m, n = parse_dru_layer_clearances("""
(version 1)
(rule named (layer "In1.Cu") (constraint clearance (min 0.2mm)))
(rule outer_r (layer outer) (constraint clearance (min 0.3mm)))
(rule inner_r (layer inner) (constraint clearance (min 0.15mm)))
""", L4)
check("named+outer+inner map", m,
      {"F.Cu": 0.3, "B.Cu": 0.3, "In1.Cu": 0.15, "In2.Cu": 0.15})
check("no notes", n, [])

# 2. later rule wins (KiCad last-to-first, first match)
m, _ = parse_dru_layer_clearances("""
(rule a (layer "F.Cu") (constraint clearance (min 0.2mm)))
(rule b (layer "F.Cu") (constraint clearance (min 0.4mm)))
""", L4)
check("later rule wins", m, {"F.Cu": 0.4})

# 3. condition onLayer forms, incl. || join and single quotes
m, n = parse_dru_layer_clearances("""
(rule c (condition "A.onLayer('F.Cu') || B.onLayer('B.Cu')")
        (constraint clearance (min 12mil)))
""", L4)
check("onLayer || condition", m, {"F.Cu": 12 * 0.0254, "B.Cu": 12 * 0.0254})
check("mil units", m.get("F.Cu"), 12 * 0.0254)

# 4. unscoped rule = every copper layer
m, _ = parse_dru_layer_clearances(
    "(rule g (constraint clearance (min 0.25mm)))", L4)
check("unscoped -> all layers", m, {l: 0.25 for l in L4})

# 5. out-of-subset conditions are SKIPPED with a note, not guessed at
m, n = parse_dru_layer_clearances("""
(rule cls (condition "A.NetClass == 'HV'") (constraint clearance (min 1mm)))
(rule mix (condition "A.onLayer('F.Cu') && A.NetClass == 'HV'")
          (constraint clearance (min 1mm)))
(rule ok (layer "B.Cu") (constraint clearance (min 0.2mm)))
""", L4)
check("non-layer conditions skipped", m, {"B.Cu": 0.2})
check("two skip notes", len(n), 2)

# 6. severity ignore skipped; non-clearance constraints ignored silently
m, n = parse_dru_layer_clearances("""
(rule off (layer "F.Cu") (severity ignore) (constraint clearance (min 9mm)))
(rule width (layer "F.Cu") (constraint track_width (min 0.1mm)))
""", L4)
check("severity-ignore + non-clearance", m, {})

# 7. layer clause AND condition = intersection
m, _ = parse_dru_layer_clearances("""
(rule both (layer outer) (condition "A.onLayer('F.Cu')")
           (constraint clearance (min 0.5mm)))
""", L4)
check("clause AND condition intersect", m, {"F.Cu": 0.5})

# 8. sibling discovery + fab floor pin
with tempfile.TemporaryDirectory() as d:
    board = os.path.join(d, "x.kicad_pcb")
    open(board, "w").write("(kicad_pcb)")
    open(os.path.join(d, "x.kicad_dru"), "w").write(
        "(rule r (layer \"B.Cu\") (constraint clearance (min 0.05mm)))")
    m, n = read_board_layer_clearances(board, L4, fab_clearance_floor=0.127)
    check("fab floor pins up", m, {"B.Cu": 0.127})
    check("pin note emitted", len(n), 1)
    m2, n2 = read_board_layer_clearances(
        os.path.join(d, "nodru.kicad_pcb"), L4)
    check("no dru -> empty no-op", (m2, n2), ({}, []))

print(f"\n{passed}/{passed + failed} checks passed")
if __name__ == '__main__':
    sys.exit(1 if failed else 0)
elif failed:
    # Imported (pytest collection). A module-scope sys.exit -- even sys.exit(0)
    # -- escapes collection as an INTERNALERROR and takes the whole session with
    # it, which is the same failure mode #457 item 3 fixes in startup_checks.
    # Raise instead, so a real failure still surfaces, attributed to this file.
    raise AssertionError(f"{failed} kicad_dru check(s) failed")
