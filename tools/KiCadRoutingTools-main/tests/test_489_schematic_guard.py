#!/usr/bin/env python3
"""#489 §3: back-annotation must not rewrite a SHARED lib_symbols definition.

schematic_updater.swap_pins_in_schematic resolves a component's lib_id from its
symbol instance, then edits the `lib_symbols` DEFINITION for that lib_id. That
definition is shared by every instance of the same lib_id in the file and there
was no instance guard -- so pin-swapping U1 silently re-pinned an identical U2,
corrupting the user's schematic (an input artifact the tool cannot validate).

Covers:
  * two instances of one lib_id -> REFUSED, file left byte-identical
  * one instance -> still swapped (the fix must not disable the feature)
  * units of one multi-unit part (U2A/U2B) share the definition legitimately
    -> still swapped
  * a same-prefix neighbour (R10 vs R1) is not mistaken for a unit suffix
  * the caller reports the divergence instead of leaving it to be inferred

Run with:  python3 tests/test_489_schematic_guard.py
"""
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT_DIR)

import schematic_updater as su  # noqa: E402

LIB_ID = "Amp:OPA2134"


def _lib_symbols(pins):
    body = "\n".join(
        f'        (pin passive line (at 0 0 0) (length 2.54)\n'
        f'          (name "{name}" (effects (font (size 1.27 1.27))))\n'
        f'          (number "{num}" (effects (font (size 1.27 1.27))))\n'
        f'        )' for name, num in pins)
    return (f'  (lib_symbols\n'
            f'    (symbol "{LIB_ID}"\n'
            f'      (symbol "OPA2134_1_1"\n{body}\n      )\n'
            f'    )\n'
            f'  )\n')


def _instance(ref, unit=1):
    return (f'  (symbol\n'
            f'    (lib_id "{LIB_ID}")\n'
            f'    (at 100 100 0)\n'
            f'    (unit {unit})\n'
            f'    (property "Reference" "{ref}"\n'
            f'      (at 100 95 0)\n'
            f'    )\n'
            f'    (property "Value" "OPA2134"\n'
            f'      (at 100 105 0)\n'
            f'    )\n'
            f'  )\n')


def _schematic(refs, pins=(("IN+", "1"), ("IN-", "2"), ("OUT", "3"))):
    return ('(kicad_sch\n  (version 20230121)\n  (generator eeschema)\n'
            + _lib_symbols(pins)
            + "".join(_instance(r, u) for r, u in refs)
            + ')\n')


def _write(text):
    fd, path = tempfile.mkstemp(suffix=".kicad_sch")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def run():
    fails = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    # ---------------------------------------- shared definition -> must REFUSE
    text = _schematic([("U1", 1), ("U2", 1)])
    path = _write(text)
    try:
        ok = su.swap_pins_in_schematic(path, "U1", "1", "2", verbose=True)
        after = open(path, encoding="utf-8").read()
    finally:
        os.unlink(path)
    check(ok is False,
          "two components sharing one lib_symbol must be REFUSED, got success")
    check(after == text,
          "a refused swap must leave the schematic byte-identical -- it was modified")

    # ------------------------------------------ single instance -> still works
    text = _schematic([("U1", 1)])
    path = _write(text)
    try:
        ok = su.swap_pins_in_schematic(path, "U1", "1", "2", verbose=True)
        after = open(path, encoding="utf-8").read()
    finally:
        os.unlink(path)
    check(ok is True, "a sole instance must still be swapped, got failure")
    check('(number "2"' in after and '(number "1"' in after,
          "both pin numbers must still be present after the swap")
    # IN+ must now carry number 2, IN- number 1.
    in_plus = after.index('"IN+"')
    in_minus = after.index('"IN-"')
    num_after_plus = after.index('(number "', in_plus)
    num_after_minus = after.index('(number "', in_minus)
    check(after.startswith('(number "2"', num_after_plus),
          f"IN+ should now be pin 2, got {after[num_after_plus:num_after_plus+12]!r}")
    check(after.startswith('(number "1"', num_after_minus),
          f"IN- should now be pin 1, got {after[num_after_minus:num_after_minus+12]!r}")

    # ------------------------- multi-unit part: units share the def LEGITIMATELY
    text = _schematic([("U2A", 1), ("U2B", 2)])
    path = _write(text)
    try:
        ok = su.swap_pins_in_schematic(path, "U2A", "1", "2", verbose=True)
    finally:
        os.unlink(path)
    check(ok is True,
          "U2A/U2B are units of ONE package and must still be swapped, got failure")

    # -------------------------------- base-reference parsing must not over-strip
    check(su._base_reference("U2A") == "U2", "U2A -> U2")
    check(su._base_reference("U2") == "U2", "U2 -> U2")
    check(su._base_reference("R10") == "R10",
          "R10 must NOT be read as unit 0 of R1")
    check(su._base_reference("IC1") == "IC1", "IC1 -> IC1")

    # R1 and R10 are DIFFERENT components -> refuse.
    text = _schematic([("R1", 1), ("R10", 1)])
    path = _write(text)
    try:
        ok = su.swap_pins_in_schematic(path, "R1", "1", "2", verbose=True)
    finally:
        os.unlink(path)
    check(ok is False,
          "R1 and R10 are different components sharing a symbol -> must refuse")

    # ------------------------------------- helper reports who shares the symbol
    shared = su.components_sharing_lib_symbol(
        _schematic([("U1", 1), ("U2", 1), ("U3", 1)]), LIB_ID, "U1")
    check(sorted(shared) == ["U2", "U3"],
          f"components_sharing_lib_symbol should list U2, U3; got {shared}")
    shared = su.components_sharing_lib_symbol(
        _schematic([("U2A", 1), ("U2B", 2)]), LIB_ID, "U2A")
    check(shared == [],
          f"a multi-unit part must not report itself as a sharer; got {shared}")

    # ----------------------------- caller surfaces the PCB/schematic divergence
    import io
    import contextlib
    tmpdir = tempfile.mkdtemp()
    try:
        with open(os.path.join(tmpdir, "sheet.kicad_sch"), "w", encoding="utf-8") as f:
            f.write(_schematic([("U1", 1), ("U2", 1)]))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            applied, failed = su.apply_swaps_to_schematics(
                tmpdir, [{"component_ref": "U1", "pad1": "1", "pad2": "2"}])
        out = buf.getvalue()
    finally:
        import shutil
        shutil.rmtree(tmpdir)
    check((applied, failed) == (0, 1),
          f"expected (0 applied, 1 failed), got ({applied}, {failed})")
    check("disagree" in out,
          "the caller must state that board and schematic now disagree; "
          f"output was:\n{out}")
    check("REFUSING" in out,
          f"the refusal reason must be printed, not only under verbose; output was:\n{out}")

    if fails:
        for f in fails:
            print(f"  FAIL  {f}")
        print(f"\n{len(fails)} check(s) FAILED")
        return False
    print("PASS  #489 §3 shared lib_symbols guard (refuse, multi-unit still works)")
    return True


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
