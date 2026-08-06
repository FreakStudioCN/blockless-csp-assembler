"""KiCad island-link semantics fixtures (the quickfeather U6.29 experiment).

Three minimal two-island boards (a B.Cu GND pour split by a SIG track) pin
down the rule the U6.29 investigation established: **KiCad demands exactly
one unconnected link for a same-zone fill island in every configuration** --
THT pads on both sides, the quickfeather anatomy (SMD pad + stub + via onto
the island), and even a via-only island with no pad at all. There is no
zone-as-one-node special case; links are merely NAMED by the closest member
items (Zone|Zone only when both clusters are pure fill).

Guards two regressions:
  1. Anyone "fixing" the checker or repair by assuming KiCad exempts
     islands (it does not -- the U6.29 confusion was a fill-model
     quantization artifact, see fill-model-fidelity memory).
  2. check_net_connectivity must agree with KiCad on the pad-bearing
     variants (A and B split; the via-only variant has a single pad, which
     the pad-oriented checker rightly grades trivially complete -- KiCad's
     demand there is via-level, asserted against kicad-cli only).

kicad-cli parts skip cleanly when the tool is unavailable.
"""
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from kicad_parser import parse_kicad_pcb  # noqa: E402
from check_connected import check_net_connectivity  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures',
                        'island_semantics')

VARIANTS = {
    'two_islands_tht.kicad_pcb': dict(expect_checker_split=True),
    'two_islands_smd_stub_via.kicad_pcb': dict(expect_checker_split=True),
    'two_islands_via_only.kicad_pcb': dict(expect_checker_split=False),
}


def _find_kicad_cli():
    try:
        from kicad_oracle import find_kicad_cli
        return find_kicad_cli()
    except Exception:
        return None


def _kicad_unconnected_count(board, cli):
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as t:
        out = t.name
    try:
        r = subprocess.run(
            [cli, 'pcb', 'drc', board, '--refill-zones',
             '--format', 'json', '-o', out],
            capture_output=True, timeout=120)
        if r.returncode not in (0, 5):
            return None
        with open(out) as f:
            return len(json.load(f).get('unconnected_items', []))
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def main():
    cli = _find_kicad_cli()
    for name, spec in VARIANTS.items():
        board = os.path.join(FIXTURES, name)
        pcb = parse_kicad_pcb(board)
        gid = next(n for n, net in pcb.nets.items() if net.name == 'GND')
        segs = [s for s in pcb.segments if s.net_id == gid]
        vias = [v for v in pcb.vias if v.net_id == gid]
        pads = pcb.pads_by_net.get(gid, [])
        zones = [z for z in pcb.zones if z.net_id == gid]
        r = check_net_connectivity(gid, segs, vias, pads, zones,
                                   pcb_data=pcb)
        split = not r.get('connected')
        assert split == spec['expect_checker_split'], (
            f"{name}: checker split={split}, "
            f"expected {spec['expect_checker_split']}")

        if cli:
            n = _kicad_unconnected_count(board, cli)
            assert n == 1, (
                f"{name}: KiCad demands {n} link(s), expected exactly 1 -- "
                f"island-link semantics changed?")

    # Reverse grade-reconcile (audit A2): the via-only variant is the class
    # the pad-oriented checker cannot see (single pad -> trivially complete;
    # KiCad's demand is via-level). Full grading must now surface it as a
    # kicad_only issue instead of shipping it silently.
    if cli:
        from check_connected import run_connectivity_check
        issues = run_connectivity_check(
            os.path.join(FIXTURES, 'two_islands_via_only.kicad_pcb'),
            quiet=True)
        assert len(issues) == 1 and issues[0].get('kicad_only'), (
            f"via-only island not reverse-flagged: {issues}")

    suffix = "" if cli else " (kicad-cli unavailable: KiCad half skipped)"
    print(f"PASS: island-link semantics fixtures{suffix}")


if __name__ == '__main__':
    main()
