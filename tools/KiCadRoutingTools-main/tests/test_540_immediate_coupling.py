#!/usr/bin/env python3
"""#540: bus-coupling awareness in the adaptive immediate-reconnect gate.

The #517 rip-pressure count gate cannot see CORRIDOR COUPLING: a short phase
whose ripped nets are bus siblings (allwinner_h3_ddr3's 9-pad DDR phase, all
victims '/DDR3 16x1/S*') passes the <=14 gate, and each immediate reconnect
locks the shared corridor against the siblings the NEXT pad rips (44 complete
vs 53 under order-only). zynq2's phase of independent signals (+1V8, PL_CLK,
'/TX_EN') is a genuine immediate win, so the defer is per-net: only nets
name-coupled to the phase's other rips wait for the end-of-run batch.

Coupling groups per net name:
- NON-ROOT hierarchical sheet prefix ('/DDR3 16x1/SDQ5' -> '/DDR3 16x1';
  '/A5' lives in the root sheet and gets no sheet key), threshold >= 2 nets;
- trailing-index-stripped stem (SDQ5/SDQ14 -> 'SDQ', SDQS0P/SDQS0N -> 'SDQS';
  stem must be >= 2 chars and differ from the base name), threshold >= 3 nets,
  with power-rail names (+1V8, VCC*, GND*, ...) excluded so +1V8/+1V0 never
  couple.

KICAD_PLANE_IMMEDIATE_COUPLING=0 disables (pure #517 count gate).

Run:  python3 tests/test_540_immediate_coupling.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import repair_planes as rdp


class _Net:
    def __init__(self, name):
        self.name = name


class _Pcb:
    def __init__(self, names):
        self.nets = {i: _Net(n) for i, n in enumerate(names)}


def test_bus_group_keys():
    # Hierarchical DDR nets: sheet key + index-stripped stem key.
    assert rdp._bus_group_keys('/DDR3 16x1/SDQ5') == [
        ('sheet', '/DDR3 16x1'), ('stem', 'SDQ')]
    # P/N strobe suffixes strip with the index: SDQS0P/SDQS0N share 'SDQS'.
    assert rdp._bus_group_keys('/DDR3 16x1/SDQS0P') == [
        ('sheet', '/DDR3 16x1'), ('stem', 'SDQS')]
    assert rdp._bus_group_keys('/DDR3 16x1/SDQS0N') == [
        ('sheet', '/DDR3 16x1'), ('stem', 'SDQS')]
    # Root-sheet nets get NO sheet key ('/A5' is not a bus declaration) and
    # 'A' is too short for a stem.
    assert rdp._bus_group_keys('/A5') == []
    # Control signals without a trailing index have no stem key.
    assert rdp._bus_group_keys('SCAS') == []
    assert rdp._bus_group_keys('/DDR3 16x1/SCAS') == [('sheet', '/DDR3 16x1')]
    # Power rails never form stem groups (+1V8 vs +1V0, GND_SENSE2).
    assert rdp._bus_group_keys('+1V8') == []
    assert rdp._bus_group_keys('GND_SENSE2') == []
    assert rdp._bus_group_keys('VCC3') == []


def test_allwinner_ddr_phase_defers():
    # Observed allwinner_h3_ddr3 rip order: pad 1 rips SDQ5+SDQS0N, pad 2
    # rips SDQ7+SDQ9+SA12.  Every one shares the '/DDR3 16x1' sheet ->
    # coupled from the FIRST transaction (threshold 2).
    pcb = _Pcb(['/DDR3 16x1/SDQ5', '/DDR3 16x1/SDQS0N', '/DDR3 16x1/SDQ7',
                '/DDR3 16x1/SDQ9', '/DDR3 16x1/SA12'])
    assert rdp._corridor_coupled_ids([0, 1], [0, 1], pcb) == {0, 1}
    assert rdp._corridor_coupled_ids([2, 3, 4], [0, 1, 2, 3, 4], pcb) \
        == {2, 3, 4}


def test_zynq2_independent_phase_stays_immediate():
    # Observed muzy_zynq2 rip victims: globals, power rails, root-sheet
    # signals.  None couple -- the whole phase keeps immediate reconnects.
    pcb = _Pcb(['+3V3', '+1V8', 'PS_CLK', '+1V0', 'PL_CLK', '/G17',
                '/TX_EN', '/SDRAM_nCS', '/A5', '/TX1'])
    all_ids = list(range(10))
    assert rdp._corridor_coupled_ids([0, 1, 2], [0, 1, 2], pcb) == set()
    assert rdp._corridor_coupled_ids(all_ids, all_ids, pcb) == set()


def test_apple2e_small_stem_groups_stay_immediate():
    # apple2e_mmu (an immediate-reconnect WINNER): A3/A4/A6 stems are 1 char
    # (too short), ORA1/ORA6 is a stem group of 2 (< 3).  Nothing defers.
    pcb = _Pcb(['A3', 'A4', 'A6', 'ORA1', 'ORA6', 'TMS', 'Q3'])
    ids = list(range(7))
    assert rdp._corridor_coupled_ids(ids, ids, pcb) == set()


def test_flat_bus_names_couple_at_three():
    # Flat (root-level) bus naming needs the stem rule: three DDR_DQ* nets
    # couple, the unrelated EN does not.
    pcb = _Pcb(['DDR_DQ5', 'DDR_DQ14', 'DDR_DQ2', 'EN'])
    assert rdp._corridor_coupled_ids([0, 1], [0, 1], pcb) == set()
    assert rdp._corridor_coupled_ids([0, 1, 2, 3], [0, 1, 2, 3], pcb) \
        == {0, 1, 2}


def test_coupling_grows_retroactively():
    # A net ripped alone is immediate; once siblings accumulate in the phase,
    # LATER rips of the same group defer (the group counts include history).
    pcb = _Pcb(['DDR_DQ5', 'DDR_DQ14', 'DDR_DQ2'])
    assert rdp._corridor_coupled_ids([0], [0], pcb) == set()
    assert rdp._corridor_coupled_ids([2], [0, 1, 2], pcb) == {2}


def test_phase_history_couples_after_success():
    # daisho: /ddr2/A1, VREF, DQ11 ripped ONE PAD AT A TIME with a successful
    # immediate reconnect between each.  The pending population alone is a
    # single net at every decision; the phase HISTORY (which keeps nets whose
    # reconnect succeeded) is what makes the second sibling defer.  The
    # caller passes pending | phase_history as the population.
    pcb = _Pcb(['/ddr2/A1', '/ddr2/VREF', '/ddr2/DQ11'])
    # First rip: alone in both histories -> immediate.
    assert rdp._corridor_coupled_ids([0], {0}, pcb) == set()
    # Second rip: A1 already reconnected (not pending) but in phase history.
    assert rdp._corridor_coupled_ids([1], {0, 1}, pcb) == {1}
    assert rdp._corridor_coupled_ids([2], {0, 1, 2}, pcb) == {2}


def test_cross_phase_pending_casualties_couple():
    # The population is ALL pending ripped casualties, not the current
    # phase's rips: allwinner's VCC-DRAM phase ripped SDQ1 alone, but its
    # /DDR3 siblings from the GND phase's deferred batch were still off the
    # board -- reconnecting SDQ1 immediately claims their corridors.
    pcb = _Pcb(['/DDR3 16x1/SDQ1',          # this phase's lone rip
                '/DDR3 16x1/SDQ5',          # pending from the GND phase
                '/DDR3 16x1/SA12', '+3V3'])
    assert rdp._corridor_coupled_ids([0], [0, 1, 2, 3], pcb) == {0}
    # With no pending sibling it stays immediate.
    assert rdp._corridor_coupled_ids([0], [0, 3], pcb) == set()


def test_env_kill_switch(monkeypatch=None):
    # KICAD_PLANE_IMMEDIATE_COUPLING=0 restores the pure count gate.
    pcb = _Pcb(['/DDR3 16x1/SDQ5', '/DDR3 16x1/SDQ7'])
    old = rdp._PLANE_IMMEDIATE_COUPLING
    try:
        rdp._PLANE_IMMEDIATE_COUPLING = False
        assert rdp._corridor_coupled_ids([0, 1], [0, 1], pcb) == set()
    finally:
        rdp._PLANE_IMMEDIATE_COUPLING = old


if __name__ == '__main__':
    test_bus_group_keys()
    test_allwinner_ddr_phase_defers()
    test_zynq2_independent_phase_stays_immediate()
    test_apple2e_small_stem_groups_stay_immediate()
    test_flat_bus_names_couple_at_three()
    test_coupling_grows_retroactively()
    test_phase_history_couples_after_success()
    test_cross_phase_pending_casualties_couple()
    test_env_kill_switch()
    print('All #540 coupling-gate tests passed')
