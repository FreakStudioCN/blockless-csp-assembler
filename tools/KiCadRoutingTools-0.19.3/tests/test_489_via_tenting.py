#!/usr/bin/env python3
"""#489 §8: via tenting/plugging/filling must round-trip, not be re-stamped.

Via had no field for the protection family, the parser explicitly skipped those
tokens, and generate_via_sexpr hardcoded `(tenting (front yes) (back yes))` on
every via it emitted. So any via that was ripped and RE-PLACED (rip-up/reroute,
sub-grid via nudge, tap relocation) silently lost its real spec -- which matters
most for via-in-pad, where IPC-4761 Type VII filled+capped+plated is what keeps
solder out of the barrel.

Covers:
  * the parser reads all five tokens off a real multi-line via block
  * the writer re-emits a via's own spec verbatim (whitespace normalised)
  * a NEW via adopts the board's prevailing convention instead of tenting-yes
  * with no board spec at all, previous behaviour is unchanged (v10 tenting yes,
    v9 numeric-net output silent)
  * prevailing_via_protection is a deterministic majority
  * the via-in-pad fab note fires on a same-net pad hit and not otherwise

Run with:  python3 tests/test_489_via_tenting.py
"""
import os
import re
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, "rust_router"))

import kicad_parser as kp  # noqa: E402
from kicad_parser import Via, Pad  # noqa: E402
from kicad_writer import (generate_via_sexpr, prevailing_via_protection,  # noqa: E402
                          prevailing_via_protection_in_text)
from fab_notes import via_in_pad_sites, via_in_pad_summary  # noqa: E402

# A real hackrf_one via block: full IPC-4761 declaration, multi-line.
REAL_VIA = '''	(via
		(at 154.4118 141.5736)
		(size 0.6858)
		(drill 0.3302)
		(layers "F.Cu" "B.Cu")
		(capping no)
		(covering
			(front no)
			(back no)
		)
		(plugging
			(front no)
			(back no)
		)
		(filling no)
		(net "!MIX_BYPASS")
		(uuid "4d55cf6a-37ab-475e-af81-21fc5c01c815")
	)
'''

TENTED_VIA = '''	(via
		(at 10.0 20.0)
		(size 0.6)
		(drill 0.3)
		(layers "F.Cu" "B.Cu")
		(tenting (front yes) (back yes))
		(net "GND")
		(uuid "11111111-2222-3333-4444-555555555555")
	)
'''

PLAIN_VIA = '''	(via
		(at 30.0 40.0)
		(size 0.6)
		(drill 0.3)
		(layers "F.Cu" "B.Cu")
		(net "GND")
		(uuid "99999999-8888-7777-6666-555555555555")
	)
'''


def run():
    fails = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    # ------------------------------------------------ parser reads all 5 tokens
    specs = kp._extract_via_protection_attrs(REAL_VIA)
    uuid = "4d55cf6a-37ab-475e-af81-21fc5c01c815"
    check(uuid in specs, f"no spec parsed for the real via; got {specs}")
    spec = specs.get(uuid, {})
    check(set(spec) == {'capping', 'covering', 'plugging', 'filling'},
          f"expected capping/covering/plugging/filling, got {sorted(spec)}")
    check(spec.get('capping') == 'no' and spec.get('filling') == 'no',
          f"whole-via tokens should read 'no', got {spec}")
    check('(front no)' in spec.get('covering', '') and '(back no)' in spec.get('covering', ''),
          f"per-side covering should keep both sides, got {spec.get('covering')!r}")
    check(kp._extract_via_protection_attrs(PLAIN_VIA) == {},
          "a via with no protection tokens must yield no spec")

    # ------------------------------------------------- writer re-emits verbatim
    out = generate_via_sexpr(154.4118, 141.5736, 0.6858, 0.3302,
                             ['F.Cu', 'B.Cu'], 3, net_name='!MIX_BYPASS',
                             tenting_attrs=spec)
    check('(covering (front no) (back no))' in out,
          f"per-side covering must round-trip on one line; got:\n{out}")
    check('(capping no)' in out and '(filling no)' in out,
          f"capping/filling must round-trip; got:\n{out}")
    check('(tenting' not in out,
          f"a via that specifies no tenting must NOT gain one; got:\n{out}")
    # Token order is the file's canonical order, not dict order.
    order = [m.group(1) for m in re.finditer(r'\((tenting|covering|plugging|capping|filling)[\s)]', out)]
    check(order == ['covering', 'plugging', 'capping', 'filling'],
          f"tokens should emit in canonical order, got {order}")

    # ----------------------------------- unchanged behaviour with nothing to say
    v10 = generate_via_sexpr(1, 2, 0.6, 0.3, ['F.Cu', 'B.Cu'], 5, net_name='GND')
    check('(tenting (front yes) (back yes))' in v10,
          f"with no spec, KiCad 10 output must keep today's tenting; got:\n{v10}")
    v9 = generate_via_sexpr(1, 2, 0.6, 0.3, ['F.Cu', 'B.Cu'], 5)
    check('tenting' not in v9,
          f"numeric-net (KiCad 9) output must stay silent; got:\n{v9}")

    # ------------------------------------------- prevailing convention, majority
    def _v(spec):
        return Via(x=0, y=0, size=0.6, drill=0.3, layers=['F.Cu', 'B.Cu'],
                   net_id=1, tenting_attrs=spec)

    a = {'capping': 'no'}
    b = {'tenting': '(front yes) (back yes)'}
    check(prevailing_via_protection([_v(a), _v(a), _v(b)]) == a,
          "the majority spec must win")
    check(prevailing_via_protection([_v(b), _v(a), _v(a)]) == a,
          "the majority must not depend on input order")
    check(prevailing_via_protection([]) is None,
          "no vias -> no prevailing spec")
    check(prevailing_via_protection([_v({}), _v({})]) is None,
          "vias with no spec -> no prevailing spec")
    # Deterministic tie-break: same answer whichever order a tie arrives in.
    t1 = prevailing_via_protection([_v(a), _v(b)])
    t2 = prevailing_via_protection([_v(b), _v(a)])
    check(t1 == t2, f"a tie must break deterministically, got {t1} vs {t2}")

    # A new via on a board whose vias all decline protection must follow suit,
    # NOT arrive tented.
    board_text = REAL_VIA + REAL_VIA.replace(uuid, "0000ffff-1111-2222-3333-444444444444")
    prevailing = prevailing_via_protection_in_text(board_text)
    check(prevailing is not None and prevailing.get('capping') == 'no',
          f"text-based prevailing spec should match the board, got {prevailing}")
    new_out = generate_via_sexpr(5, 5, 0.6, 0.3, ['F.Cu', 'B.Cu'], 1,
                                 net_name='GND', tenting_attrs=prevailing)
    check('(tenting (front yes) (back yes))' not in new_out,
          f"a new via must not be stamped tented on a board that declines it; got:\n{new_out}")
    check(prevailing_via_protection_in_text(PLAIN_VIA) is None,
          "a board with no protection tokens has no prevailing spec")

    # ------------------------------------------------- via-in-pad fab note
    pad = Pad(component_ref='U1', pad_number='A1', global_x=10.0, global_y=10.0,
              local_x=0, local_y=0, size_x=0.5, size_y=0.5, shape='rect',
              layers=['F.Cu'], net_id=7, net_name='N', drill=0.0, pad_type='smd')
    far_pad = Pad(component_ref='U1', pad_number='A2', global_x=20.0, global_y=20.0,
                  local_x=0, local_y=0, size_x=0.5, size_y=0.5, shape='rect',
                  layers=['F.Cu'], net_id=7, net_name='N', drill=0.0, pad_type='smd')
    in_pad = {'x': 10.05, 'y': 9.95, 'size': 0.45, 'drill': 0.25, 'net_id': 7}
    outside = {'x': 15.0, 'y': 15.0, 'size': 0.45, 'drill': 0.25, 'net_id': 7}
    foreign = {'x': 10.0, 'y': 10.0, 'size': 0.45, 'drill': 0.25, 'net_id': 99}
    pads_by_net = {7: [pad, far_pad]}
    check(len(via_in_pad_sites([in_pad], pads_by_net)) == 1,
          "a via inside a same-net pad is via-in-pad")
    check(via_in_pad_sites([outside], pads_by_net) == [],
          "a via clear of every pad is not via-in-pad")
    check(via_in_pad_sites([foreign], pads_by_net) == [],
          "a via in a FOREIGN pad is a short, not via-in-pad -- not this report")
    summary = via_in_pad_summary([in_pad, outside], pads_by_net)
    check(summary and summary['count'] == 1 and summary['pads'] == ['U1.A1'],
          f"summary should name the one hit pad, got {summary}")
    check('IPC-4761' in (summary or {}).get('note', ''),
          "the fab note must name the IPC-4761 requirement it creates")
    check(via_in_pad_summary([outside], pads_by_net) is None,
          "no via-in-pad -> no record (so nothing is printed)")

    if fails:
        for f in fails:
            print(f"  FAIL  {f}")
        print(f"\n{len(fails)} check(s) FAILED")
        return False
    print("PASS  #489 §8 via protection round-trip + prevailing convention + fab note")
    return True


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
