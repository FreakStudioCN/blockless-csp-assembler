#!/usr/bin/env python3
"""A layer mod must land on the segment whose CURRENT layer it names, and a
mod that matches nothing must be REPORTED (cynthion, #264 class).

stub_layer_switching mutates seg.layer in pcb_data at decision time and asks
the writer to make the file agree. Two ways that contract broke:

  1. mods were resolved as "last mod for this (coords, net) wins", ignoring
     old_layer. One net can own twin segments of identical geometry on
     different layers (a stub and its continuation), so the wrong mod was
     applied -- the #264 ambiguity, which is NOT limited to twins of
     different nets as the old comment assumed.
  2. a mod that matched no file block was discarded silently, so the file
     kept the old layer while pcb_data said the copper had moved.

What that shipped (cynthion, arch sweep 0804): VCCRAM's stub
(138.425,92.1)-(138.5,92.1) stayed on In1.Cu in the written file while
pcb_data had it on In3.Cu, leaving a 0.075mm In1 sliver between two In3
segments with NO via at either end -- a broken track plus stray copper that
KiCad DRC does not flag as a break. Only the FILE_LEDGER audit caught it.

    python3 tests/test_layer_mod_twin_stub.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from kicad_writer import modify_segment_layers  # noqa: E402

_fail = []


def check(label, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}: {label}" + (f"  ({detail})" if detail and not cond else ''))
    if not cond:
        _fail.append(label)


def seg(start, end, layer, net='VCCRAM', width='0.1'):
    return (f'\t(segment\n\t\t(start {start[0]:.6f} {start[1]:.6f})\n'
            f'\t\t(end {end[0]:.6f} {end[1]:.6f})\n\t\t(width {width})\n'
            f'\t\t(layer "{layer}")\n\t\t(net "{net}")\n'
            f'\t\t(uuid "0000-0000")\n\t)\n')


def main():
    print("1. cynthion topology: the In1 stub moves to In3, its In3 twin stays")
    # Same net, identical geometry, two layers -- exactly what made "last mod
    # wins" pick wrong. Only the In1 copy has a mod.
    content = (seg((138.425, 92.1), (138.5, 92.1), 'In1.Cu')
               + seg((138.425, 92.1), (138.5, 92.1), 'In3.Cu'))
    mods = [{'start': (138.425, 92.1), 'end': (138.5, 92.1),
             'net_id': 218, 'net_name': 'VCCRAM',
             'old_layer': 'In1.Cu', 'new_layer': 'In3.Cu'}]
    un = []
    out, n = modify_segment_layers(content, mods, unapplied_out=un)
    check("the In1 block was rewritten", n == 1, f"n={n}")
    check("no In1.Cu copy survives", '"In1.Cu"' not in out)
    check("both blocks are now In3.Cu", out.count('"In3.Cu"') == 2,
          f"count={out.count(chr(34) + 'In3.Cu' + chr(34))}")
    check("nothing reported unapplied", un == [], f"un={len(un)}")

    print("2. a mod matching NO block is reported, not swallowed")
    content = seg((10.0, 10.0), (11.0, 10.0), 'F.Cu', net='OTHER')
    mods = [{'start': (138.425, 92.1), 'end': (138.5, 92.1),
             'net_id': 218, 'net_name': 'VCCRAM',
             'old_layer': 'In1.Cu', 'new_layer': 'In3.Cu'}]
    un = []
    out, n = modify_segment_layers(content, mods, unapplied_out=un)
    check("nothing was rewritten", n == 0, f"n={n}")
    check("the unmatched mod is reported", len(un) == 1, f"un={len(un)}")

    print("3. wrong-layer mod does NOT steal a twin on another layer")
    # The mod names In2.Cu; neither block is on In2, so it must not apply.
    content = (seg((5.0, 5.0), (6.0, 5.0), 'In1.Cu')
               + seg((5.0, 5.0), (6.0, 5.0), 'In3.Cu'))
    mods = [{'start': (5.0, 5.0), 'end': (6.0, 5.0),
             'net_id': 218, 'net_name': 'VCCRAM',
             'old_layer': 'In2.Cu', 'new_layer': 'B.Cu'}]
    un = []
    out, n = modify_segment_layers(content, mods, unapplied_out=un)
    check("no block was rewritten", n == 0, f"n={n}")
    check("no B.Cu appeared", '"B.Cu"' not in out)
    check("the mod is reported unapplied", len(un) == 1, f"un={len(un)}")

    print("4. chained swaps on ONE segment still land on the final layer")
    content = seg((1.0, 1.0), (2.0, 1.0), 'In1.Cu')
    mods = [{'start': (1.0, 1.0), 'end': (2.0, 1.0), 'net_id': 218,
             'net_name': 'VCCRAM', 'old_layer': 'In1.Cu',
             'new_layer': 'In2.Cu'},
            {'start': (1.0, 1.0), 'end': (2.0, 1.0), 'net_id': 218,
             'net_name': 'VCCRAM', 'old_layer': 'In2.Cu',
             'new_layer': 'B.Cu'}]
    un = []
    out, n = modify_segment_layers(content, mods, unapplied_out=un)
    # The block starts on In1, so the chain In1->In2->B must be FOLLOWED to
    # its end: the file lands on B.Cu and no link is left over. (An earlier
    # draft of this fix stopped at the first link and broke exactly this --
    # caught by tests/test_segment_layer_mods.py, not by this file.)
    check("the chain resolves to the final layer", '"B.Cu"' in out, out)
    check("no intermediate layer survives", '"In2.Cu"' not in out, out)
    check("nothing reported unapplied", un == [], f"un={len(un)}")

    print("5. mods with no old_layer keep the legacy last-wins behavior")
    content = seg((3.0, 3.0), (4.0, 3.0), 'F.Cu')
    mods = [{'start': (3.0, 3.0), 'end': (4.0, 3.0), 'net_id': 218,
             'net_name': 'VCCRAM', 'new_layer': 'B.Cu'}]
    un = []
    out, n = modify_segment_layers(content, mods, unapplied_out=un)
    check("legacy mod still applies", n == 1 and '"B.Cu"' in out, f"n={n}")
    check("nothing reported unapplied", un == [], f"un={len(un)}")

    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): " + "; ".join(_fail))
        sys.exit(1)
    print("All checks passed")


if __name__ == '__main__':
    main()
