#!/usr/bin/env python3
"""#489 §6: plane ampacity -- which standard, what copper weight, what range.

The module had NO test at all. These pin the four filed problems:

  1. Mis-attribution: the formula is the IPC-2221 chart fit (k=0.024 internal /
     0.048 external), not IPC-2152. Values are checked against hand-computed
     expectations, and the IPC-2152 path is asserted to drop 2221's 2x internal
     derating (which is what 2152 actually overturned).
  2. The routing_constants values are the ones used -- they were dead (hardcoded
     inline) with their two exponent comments swapped.
  3. Copper weight comes from the board's stackup, not a hardcoded 1 oz.
  4. A pour-sized cross-section is flagged out of chart range instead of being
     reported as a rating.

Run with:  python3 tests/test_489_ampacity.py
"""
import math
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, "rust_router"))

import plane_resistance as pr  # noqa: E402
import routing_constants as rc  # noqa: E402
from kicad_parser import PCBData, BoardInfo, StackupLayer  # noqa: E402


def _hand_area(width_mm, oz):
    """Cross-section in mils², computed independently of the module."""
    return (width_mm * oz * 0.035) * (1000 / 25.4) ** 2


def _board(stackup):
    info = BoardInfo(layers={0: 'F.Cu', 1: 'In1.Cu', 2: 'B.Cu'},
                     copper_layers=['F.Cu', 'In1.Cu', 'B.Cu'])
    info.stackup = stackup
    return PCBData(board_info=info, nets={}, footprints={}, vias=[],
                   segments=[], pads_by_net={})


def run():
    fails = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    def close(a, b, tol=1e-9):
        return abs(a - b) <= tol * max(1.0, abs(b))

    # ------------------------------------------------- 1. the constants are IPC-2221
    check(rc.IPC_2221_EXPONENT_DT == 0.44,
          "temperature-rise exponent must be 0.44 (it was commented as the AREA exponent)")
    check(rc.IPC_2221_EXPONENT_AREA == 0.725,
          "area exponent must be 0.725 (it was commented as the TEMPERATURE exponent)")
    check(rc.IPC_2221_K_INTERNAL == 0.024 and rc.IPC_2221_K_EXTERNAL == 0.048,
          "k values must be 0.024 internal / 0.048 external")
    check(not hasattr(rc, 'IPC_2152_K_INTERNAL'),
          "the IPC_2152_* constant names must be gone -- the formula is 2221's")

    # The constants must actually be USED, not shadowed by inline literals: bump
    # them and the function must follow.
    saved = rc.IPC_2221_K_EXTERNAL
    try:
        rc.IPC_2221_K_EXTERNAL = saved * 2
        import importlib
        importlib.reload(pr)
        doubled = pr.calculate_max_current_ipc2221(1.0, 1.0, 10.0, is_internal=False)
    finally:
        rc.IPC_2221_K_EXTERNAL = saved
        importlib.reload(pr)
    base = pr.calculate_max_current_ipc2221(1.0, 1.0, 10.0, is_internal=False)
    check(close(doubled, base * 2, 1e-6),
          "calculate_max_current_ipc2221 must READ routing_constants, not hardcode "
          f"k inline (doubling k gave {doubled} vs {base})")

    # ------------------------------------------------ 2. values vs hand computation
    for width, oz, dt in [(1.0, 1.0, 10.0), (0.5, 2.0, 20.0)]:
        area = _hand_area(width, oz)
        check(close(pr.cross_section_mils2(width, oz), area, 1e-9),
              f"cross_section_mils2({width}, {oz}) should be {area}")
        for internal, k in ((True, 0.024), (False, 0.048)):
            want = k * dt ** 0.44 * area ** 0.725
            got = pr.calculate_max_current_ipc2221(width, oz, dt, internal)
            check(close(got, want, 1e-9),
                  f"ipc2221({width}mm, {oz}oz, {dt}C, internal={internal}): "
                  f"expected {want:.6f} A, got {got:.6f} A")

    # ----------------------------------- 3. IPC-2152 drops the 2x internal derating
    ext = pr.calculate_max_current_ipc2221(1.0, 1.0, 10.0, is_internal=False)
    internal_2221 = pr.calculate_max_current_ipc2221(1.0, 1.0, 10.0, is_internal=True)
    internal_2152 = pr.calculate_max_current_ipc2152(1.0, 1.0, 10.0, is_internal=True)
    check(close(internal_2221, ext / 2, 1e-9),
          "2221 must derate internal by 2x (that is the behaviour 2152 reversed)")
    check(close(internal_2152, ext, 1e-9),
          f"2152 must NOT derate internal: expected {ext:.6f} A, got {internal_2152:.6f} A")
    check(pr.calculate_max_current_ipc is pr.calculate_max_current_ipc2221,
          "the old name must remain a working alias for the 2221 function")

    # ---------------------------------------------------- 4. chart-range guard
    check(pr.ipc2221_area_in_range(1.0, 1.0),
          "a 1mm 1oz trace (54 mils²) is inside the chart range")
    check(not pr.ipc2221_area_in_range(52.2, 1.0),
          "a 52.2mm pour (2832 mils²) is NOT inside the IPC-2221 chart range")
    check(rc.IPC_2221_MAX_AREA_MILS2 == 700.0,
          "chart-range bound should be 700 mils²")

    # The pour case that produced the bogus documented figure.
    pour = pr._plane_metrics(117.0, 52.2, 'In4.Cu', 1.0, 10.0)
    check(pour['in_chart_range'] is False,
          "the 52.2mm pour result must be flagged out of chart range")
    check(abs(pour['max_current'] - 21.04) < 0.05,
          f"sanity: the extrapolated value is still reported for reference, "
          f"got {pour['max_current']}")
    check(pour['current_standard'] == 'IPC-2221',
          "results must say which standard produced max_current")
    check(pour['copper_oz'] == 1.0 and pour['temp_rise_c'] == 10.0,
          "results must carry the conditions they were computed at")

    # ------------------------------------------- 5. copper weight from the stackup
    two_oz = _board([
        StackupLayer(name='F.Cu', layer_type='copper', thickness=0.070),
        StackupLayer(name='dielectric 1', layer_type='core', thickness=1.0),
        StackupLayer(name='In1.Cu', layer_type='copper', thickness=0.035),
    ])
    got = pr.stackup_copper_oz(two_oz, 'F.Cu')
    check(close(got, 2.0, 1e-6), f"70um F.Cu should read as 2 oz, got {got}")
    got = pr.stackup_copper_oz(two_oz, 'In1.Cu')
    check(close(got, 1.0, 1e-6), f"35um In1.Cu should read as 1 oz, got {got}")
    check(pr.stackup_copper_oz(two_oz, 'B.Cu') == 1.0,
          "a layer absent from the stackup falls back to 1 oz")
    check(pr.stackup_copper_oz(_board([]), 'F.Cu') == 1.0,
          "a board with no stackup falls back to 1 oz")

    # 2 oz must actually change the answer -- the bug was that it never reached
    # the formula.
    one = pr._plane_metrics(50.0, 1.0, 'F.Cu', 1.0, 10.0)
    two = pr._plane_metrics(50.0, 1.0, 'F.Cu', 2.0, 10.0)
    check(two['max_current'] > one['max_current'] * 1.5,
          f"2 oz copper must raise Imax well above 1 oz "
          f"({two['max_current']:.3f} vs {one['max_current']:.3f} A)")
    check(two['resistance'] < one['resistance'],
          "2 oz copper must lower plane resistance")

    if fails:
        for f in fails:
            print(f"  FAIL  {f}")
        print(f"\n{len(fails)} check(s) FAILED")
        return False
    print("PASS  #489 §6 IPC-2221/2152 ampacity, stackup copper weight, chart range")
    return True


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
