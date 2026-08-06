#!/usr/bin/env python3
"""Regression test for #493 item 1: net filter patterns are sheet-path aware.

KiCad qualifies a net with the sheet it was declared on ('/GND',
'/Management Interface/VSMPS'), but everyone writes the unqualified spelling on
the command line: `--nets '*' '!GND'`. Plain fnmatch against the FULL name makes
that exclusion match nothing, so the net is routed anyway -- silently.

That is issue #292 (core1106_cam routed its plane nets as traces), which was
closed with a "did you mean" WARNING rather than a fix, and it recurred on
eth_tap: its step-1 BGA fanout shipped 267 '/GND' + 171 '/3V3' segments out of
750 from a command that asked to exclude both.

The fix: a pattern naming NO path is matched against the net's trailing
component as well; a pattern that DOES carry a path stays exact. Both the CLI
selector (expand_net_patterns), the engine filter (matches_net_filter) and the
GUI plan selector go through the one helper, so all three agree.

Run:  python3 tests/test_493_net_pattern_sheet_path.py
"""
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT_DIR)

from net_queries import (net_pattern_matches, matches_net_filter,
                         expand_net_patterns)

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok   {label}")


def test_unqualified_pattern_matches_sheet_qualified_net():
    print("1. unqualified pattern reaches the sheet-qualified net")
    check("'/GND' ~ 'GND'", net_pattern_matches('/GND', 'GND'), True)
    check("'/Analog/GND' ~ 'GND'", net_pattern_matches('/Analog/GND', 'GND'), True)
    check("'/Management Interface/VSMPS' ~ 'VSMPS'",
          net_pattern_matches('/Management Interface/VSMPS', 'VSMPS'), True)
    check("'/3V3' ~ '3V3'", net_pattern_matches('/3V3', '3V3'), True)
    # ...without becoming a substring match: only the WHOLE trailing component.
    check("'/GND_A' ~ 'GND' (no partial)", net_pattern_matches('/GND_A', 'GND'), False)
    check("'/VCC' ~ 'GND'", net_pattern_matches('/VCC', 'GND'), False)


def test_explicit_path_stays_exact():
    print("2. a pattern that names a path is taken literally")
    check("'/GND' ~ '/GND'", net_pattern_matches('/GND', '/GND'), True)
    # '/GND' must NOT reach a DIFFERENT sheet's GND -- the user spelled a path.
    check("'/Analog/GND' ~ '/GND'", net_pattern_matches('/Analog/GND', '/GND'), False)
    check("'/A/B' ~ '/A/*'", net_pattern_matches('/A/B', '/A/*'), True)


def test_wildcards_still_work():
    print("3. wildcards keep working on both forms")
    check("'/GND' ~ '*'", net_pattern_matches('/GND', '*'), True)
    check("'/A/B' ~ 'B*'", net_pattern_matches('/A/B', 'B*'), True)
    check("'/Net-(U1-Pad8)' ~ 'Net-(U1-*'",
          net_pattern_matches('/Net-(U1-Pad8)', 'Net-(U1-*'), True)


def test_filter_excludes_plane_nets():
    print("4. matches_net_filter: the eth_tap fanout filter")
    f = ['*', '!GND', '!3V3']
    check("/GND excluded", matches_net_filter('/GND', f), False)
    check("/3V3 excluded", matches_net_filter('/3V3', f), False)
    check("signal kept", matches_net_filter('/Management Interface/VSMPS', f), True)


def test_active_low_literal_preserved():
    """#177 must survive: '!RESET' can be a real net name, not an exclusion."""
    print("5. active-low net names still selectable (#177)")
    check("'!RESET' selected by ['!RESET']",
          matches_net_filter('!RESET', ['!RESET']), True)
    check("'/RESET' excluded by ['*','!RESET']",
          matches_net_filter('/RESET', ['*', '!RESET']), False)


def test_expand_net_patterns_resolves_and_excludes():
    print("6. expand_net_patterns (the route.py --nets path)")

    class _Net:
        def __init__(self, nid, name):
            self.net_id, self.name = nid, name

    class _PCB:
        def __init__(self, names):
            self.nets = {i + 1: _Net(i + 1, n) for i, n in enumerate(names)}
            self.pads_by_net = {}

    pcb = _PCB(['/GND', '/3V3', '/Analog/GND', '/SIG1', '/SIG2'])

    sel = expand_net_patterns(pcb, ['*', '!GND', '!3V3'])
    check("'/GND' excluded", '/GND' in sel, False)
    check("'/Analog/GND' excluded too", '/Analog/GND' in sel, False)
    check("'/3V3' excluded", '/3V3' in sel, False)
    check("signals kept", sorted(sel), ['/SIG1', '/SIG2'])

    # An unqualified literal INCLUDE resolves to the real net rather than
    # passing a name the board does not carry down to the router.
    sel2 = expand_net_patterns(pcb, ['GND'])
    check("literal 'GND' resolves", sorted(sel2), ['/Analog/GND', '/GND'])

    # An explicit path stays exact.
    sel3 = expand_net_patterns(pcb, ['/GND'])
    check("literal '/GND' stays exact", sel3, ['/GND'])

    # A literal naming nothing is still passed through (long-standing behavior).
    sel4 = expand_net_patterns(pcb, ['NOSUCHNET'])
    check("unknown literal passed through", sel4, ['NOSUCHNET'])


def main():
    for t in (test_unqualified_pattern_matches_sheet_qualified_net,
              test_explicit_path_stays_exact,
              test_wildcards_still_work,
              test_filter_excludes_plane_nets,
              test_active_low_literal_preserved,
              test_expand_net_patterns_resolves_and_excludes):
        t()
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print("  " + f)
        return 1
    print("PASS: net filter patterns are sheet-path aware (#493 item 1)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
