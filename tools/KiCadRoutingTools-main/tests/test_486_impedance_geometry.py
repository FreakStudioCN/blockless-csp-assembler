#!/usr/bin/env python3
"""#486: impedance geometry models -- CPW-over-ground, the microstrip thickness
correction, the asymmetric-stripline solver, and the post-route checker.

Run directly (no pytest needed, matching the rest of tests/):
    python3 tests/test_486_impedance_geometry.py
"""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import impedance as I                                          # noqa: E402

FAILURES = []


def check(cond, label, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        FAILURES.append(label)


def close(a, b, tol):
    return abs(a - b) <= tol


# =============================================================================
# Complete elliptic integral (the CPW model's engine)
# =============================================================================

def test_elliptic():
    print("complete_elliptic_k")
    # Published values of K(k).
    check(close(I.complete_elliptic_k(0.0), math.pi / 2, 1e-12), "K(0) = pi/2")
    check(close(I.complete_elliptic_k(0.5), 1.6857503548, 1e-9), "K(0.5)")
    check(close(I.complete_elliptic_k(0.9), 2.2805491384, 1e-9), "K(0.9)")

    # Independent cross-check: K(k) = int_0^{pi/2} dtheta/sqrt(1-k^2 sin^2 t),
    # by Simpson. Stronger than hardcoded constants -- it re-derives the value
    # by a completely different route than the AGM the implementation uses.
    def k_quad(k, n=40001):
        h = (math.pi / 2) / (n - 1)
        tot = 0.0
        for i in range(n):
            th = i * h
            f = 1.0 / math.sqrt(1 - k * k * math.sin(th) ** 2)
            tot += f * (1 if i in (0, n - 1) else (4 if i % 2 else 2))
        return tot * h / 3

    worst = max(abs(I.complete_elliptic_k(k) - k_quad(k))
                for k in (0.1, 0.5, 0.9, 0.99))
    check(worst < 1e-9, "AGM matches numerical integration", f"worst={worst:.2e}")
    # Monotone increasing, finite at the endpoints (no ZeroDivision / inf).
    vals = [I.complete_elliptic_k(k / 100.0) for k in range(0, 100)]
    check(all(b >= a for a, b in zip(vals, vals[1:])), "K is monotone in k")
    check(math.isfinite(I.complete_elliptic_k(1.0)), "K(1) is a finite sentinel")


# =============================================================================
# CPW over ground
# =============================================================================

def test_cpwg_limits():
    print("cpwg_epsilon_eff analytic limits")
    er = 4.3
    # h -> infinity: no bottom plane, so this degenerates to ungrounded CPW,
    # whose effective permittivity is exactly (er + 1) / 2.
    check(close(I.cpwg_epsilon_eff(0.3, 0.2, 1e6, 0.0, er), (er + 1) / 2, 1e-6),
          "h -> inf gives ungrounded-CPW (er+1)/2")
    # h -> 0: the plane dominates, all field in the dielectric.
    check(close(I.cpwg_epsilon_eff(0.3, 0.2, 1e-4, 0.0, er), er, 1e-6),
          "h -> 0 gives er")
    # Always bracketed.
    for s in (0.05, 0.1, 0.3, 1.0):
        for h in (0.05, 0.1, 0.2, 0.8):
            ee = I.cpwg_epsilon_eff(0.25, s, h, 0.035, er)
            if not (1.0 <= ee <= er):
                check(False, "er_eff bracketed in [1, er]", f"s={s} h={h} ee={ee}")
                return
    check(True, "er_eff bracketed in [1, er] across the s/h grid")


def test_cpwg_z0_behaviour():
    print("cpwg_z0 behaviour")
    er, h, t, w = 4.3, 0.2, 0.035, 0.36
    # Tightening the side ground must LOWER Z0 -- the whole premise of #486 A.
    gaps = [0.08, 0.1, 0.15, 0.2, 0.3, 0.5, 0.8, 1.2]
    zs = [I.cpwg_z0(w, s, h, t, er) for s in gaps]
    check(all(b > a for a, b in zip(zs, zs[1:])),
          "Z0 increases monotonically with side gap", f"{[round(z,2) for z in zs]}")
    check(all(z > 0 for z in zs), "Z0 positive across the gap sweep")
    # A tight gap is a BIG effect, not a rounding difference.
    ms = I.microstrip_z0(w, h, t, er)
    check(I.cpwg_z0(w, 0.1, h, t, er) < 0.75 * ms,
          "tight coplanar ground drops Z0 >25% below microstrip",
          f"cpwg={I.cpwg_z0(w,0.1,h,t,er):.1f} micro={ms:.1f}")
    # ...and a wide gap converges back toward the microstrip answer.
    check(abs(I.cpwg_z0(w, 1.2, h, t, er) - ms) / ms < 0.05,
          "wide gap approaches the microstrip answer")
    # Degenerate inputs return 0.0 rather than raising.
    for bad in ((0, 0.2, h, t, er), (w, 0, h, t, er), (w, 0.2, 0, t, er),
                (w, 0.2, h, t, 0)):
        if I.cpwg_z0(*bad) != 0.0:
            check(False, "degenerate geometry returns 0.0", str(bad))
            return
    check(True, "degenerate geometry returns 0.0")


def test_cpwg_applies():
    print("cpwg_applies threshold")
    check(I.cpwg_applies(0.2, 0.2) is True, "s == h is coplanar-coupled")
    check(I.cpwg_applies(0.6, 0.2) is True, "s == 3h is at the limit")
    check(I.cpwg_applies(0.7, 0.2) is False, "s > 3h is not")
    check(I.cpwg_applies(0.0, 0.2) is False, "zero gap is not coplanar")
    check(I.cpwg_applies(0.2, 0.0) is False, "zero height is not coplanar")


def test_cpwg_width_solver():
    """The solver must actually reproduce its own target."""
    print("cpwg_width_for_z0 round-trip")
    er, h, t = 4.3, 0.2, 0.035
    for target in (40.0, 50.0, 60.0, 75.0):
        for s in (0.1, 0.2, 0.4):
            w = I.cpwg_width_for_z0(target, s, h, t, er)
            if w <= 0:
                continue        # legitimately unreachable
            got = I.cpwg_z0(w, s, h, t, er)
            if not close(got, target, 0.5):
                check(False, "solved width reproduces target",
                      f"target={target} s={s} w={w:.4f} got={got:.2f}")
                return
    check(True, "solved width reproduces target across targets and gaps")
    # A coplanar trace is always NARROWER than the microstrip one -- if this
    # inverts, routing a declared-coplanar net would come out too wide.
    for s in (0.1, 0.2, 0.3):
        w_cpw = I.cpwg_width_for_z0(50.0, s, h, t, er)
        w_ms = I.microstrip_width_for_z0(50.0, h, t, er)
        if not (0 < w_cpw < w_ms):
            check(False, "coplanar width < microstrip width",
                  f"s={s} cpw={w_cpw:.4f} ms={w_ms:.4f}")
            return
    check(True, "coplanar width < microstrip width for the same target")


def test_differential_cpwg():
    print("differential_cpwg_z0 / solver")
    er, h, t = 4.3, 0.2, 0.035
    zdiff, zodd = I.differential_cpwg_z0(0.2, 0.2, 0.2, h, t, er)
    check(zdiff > 0 and close(zdiff, 2 * zodd, 1e-9), "Zdiff == 2 * Zodd")
    # Tighter coplanar ground lowers Zdiff too.
    tight, _ = I.differential_cpwg_z0(0.2, 0.2, 0.1, h, t, er)
    wide, _ = I.differential_cpwg_z0(0.2, 0.2, 0.6, h, t, er)
    check(tight < wide, "coplanar ground lowers Zdiff", f"{tight:.1f} vs {wide:.1f}")
    w = I.differential_cpwg_width_for_z0(100.0, 0.2, 0.25, h, t, er)
    if w > 0:
        got, _ = I.differential_cpwg_z0(w, 0.2, 0.25, h, t, er)
        check(close(got, 100.0, 1.0), "differential solver round-trips",
              f"w={w:.4f} got={got:.2f}")
    else:
        check(False, "differential solver produced a width")


# =============================================================================
# Microstrip thickness correction (the IMPEDANCE_WIDTH_SCALE resolution)
# =============================================================================

def _hammerstad_jensen(w, h, t, er):
    """Independent reference implementation (H&J 1980), ~0.2% accurate."""
    u = w / h
    if t > 0:
        du1 = (t / h / math.pi) * math.log(
            1.0 + 4 * math.e / ((t / h) / (math.tanh(math.sqrt(6.517 * u)) ** 2)))
        dur = 0.5 * (1.0 + 1.0 / math.cosh(math.sqrt(er - 1))) * du1
        u1, ur = u + du1, u + dur
    else:
        u1 = ur = u

    def z01(uu):
        fu = 6 + (2 * math.pi - 6) * math.exp(-((30.666 / uu) ** 0.7528))
        return 59.9585 * math.log(fu / uu + math.sqrt(1 + (2 / uu) ** 2))

    def ereff(uu, e):
        a = (1 + (1 / 49) * math.log((uu ** 4 + (uu / 52) ** 2) / (uu ** 4 + 0.432))
             + (1 / 18.7) * math.log(1 + (uu / 18.1) ** 3))
        b = 0.564 * ((e - 0.9) / (e + 3)) ** 0.053
        return (e + 1) / 2 + (e - 1) / 2 * (1 + 10 / uu) ** (-a * b)

    ee = ereff(ur, er)
    return z01(ur) / math.sqrt(ee * (z01(u1) / z01(ur)) ** 2)


def test_microstrip_thickness():
    print("microstrip thickness correction (#486)")
    er, t = 4.3, 0.035

    def hj_width(target, h):
        lo, hi = 0.001, 50 * h
        for _ in range(200):
            mid = (lo + hi) / 2
            if _hammerstad_jensen(mid, h, t, er) > target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    worst = 0.0
    for h in (0.1, 0.15, 0.2, 0.3, 0.5, 0.8, 1.6):
        ours = I.microstrip_width_for_z0(50.0, h, t, er) * I.IMPEDANCE_WIDTH_SCALE
        ref = hj_width(50.0, h)
        worst = max(worst, abs(ours - ref) / ref)
    check(worst < 0.03, "solved 50-ohm width within 3% of H&J reference",
          f"worst={100*worst:.2f}% (was ~20% before #486)")

    # The specific regression: on a 0.1mm dielectric the old chain produced
    # 0.144mm where ~0.18mm is right.
    w = I.microstrip_width_for_z0(50.0, 0.1, t, er) * I.IMPEDANCE_WIDTH_SCALE
    check(0.17 <= w <= 0.195, "0.1mm dielectric 50-ohm width is ~0.18mm",
          f"got {w:.4f}mm")

    # t == 0 must be bit-identical to the pre-#486 formula (the fix is confined
    # to the t > 0 branch, and widths become track widths -- a ULP can flip an
    # A* tie-break).
    def old_z0(w_, h_, er_):
        u = w_ / h_
        ee = (er_ + 1) / 2 + (er_ - 1) / 2 * (1 / math.sqrt(1 + 12 / u))
        if u <= 1:
            return max((60 / math.sqrt(ee)) * math.log(8 / u + 0.25 * u), 0.0)
        return max((120 * math.pi / math.sqrt(ee))
                   / (u + 1.393 + 0.667 * math.log(u + 1.444)), 0.0)

    bad = [(w_, h_) for h_ in (0.1, 0.2, 0.5, 1.6)
           for w_ in (0.05, 0.1, 0.2, 0.5, 1.0, 3.0)
           if I.microstrip_z0(w_, h_, 0.0, er) != old_z0(w_, h_, er)]
    check(not bad, "t=0 is bit-identical to the pre-#486 formula", str(bad[:3]))

    # Thickness must still LOWER Z0, just not by 2.5x too much.
    z_t0 = I.microstrip_z0(0.18, 0.1, 0.0, er)
    z_t = I.microstrip_z0(0.18, 0.1, 0.035, er)
    drop = (z_t0 - z_t) / z_t0
    check(0.02 < drop < 0.07, "35um copper drops Z0 by a few percent",
          f"{100*drop:.1f}% (was 10.5%)")


def test_width_scale_is_neutral():
    print("IMPEDANCE_WIDTH_SCALE")
    check(I.IMPEDANCE_WIDTH_SCALE == 1.0,
          "the empirical 0.90 fudge is resolved to 1.0",
          f"got {I.IMPEDANCE_WIDTH_SCALE}")


# =============================================================================
# Asymmetric stripline: solver and verifier must agree
# =============================================================================

def test_asymmetric_stripline():
    print("asymmetric stripline solver (#486)")
    er, t = 4.6, 0.0152
    h1, h2 = 0.2104, 1.0650          # hackrf_one In1.Cu -- a real stackup
    w = I.stripline_asymmetric_width_for_z0(50.0, h1, h2, t, er)
    check(w > 0, "solver produced a width")
    got = I.stripline_z0_asymmetric(w, h1, h2, t, er)
    check(close(got, 50.0, 0.5), "asymmetric width round-trips to its target",
          f"w={w:.4f} got={got:.2f}")
    # The averaged-height symmetric answer is what the old code solved; it must
    # be clearly WIDER, which is the bug this closes.
    w_sym = I.stripline_width_for_z0(50.0, (h1 + h2) / 2, t, er)
    check(w_sym > w * 1.5, "the old symmetric solve was far too wide",
          f"sym={w_sym:.4f} asym={w:.4f}")

    class P:
        is_outer_layer = False
        height_above = h1
        height_below = h2
    check(I.is_asymmetric_stripline(P()) is True, "predicate detects asymmetry")

    class Q:
        is_outer_layer = False
        height_above = 0.2
        height_below = 0.205
    check(I.is_asymmetric_stripline(Q()) is False, "5% difference is symmetric")


# =============================================================================
# Checker plumbing (no board needed)
# =============================================================================

def test_checker_helpers():
    print("check_impedance helpers")
    import check_impedance as C

    class BI:
        stackup = []
        copper_layers = ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu']

    class PCB:
        board_info = BI()

    refs = C.reference_layers_for(PCB())
    check(refs['F.Cu'] == ['In1.Cu'], "outer layer references the one below",
          str(refs.get('F.Cu')))
    check(refs['B.Cu'] == ['In2.Cu'], "bottom layer references the one above",
          str(refs.get('B.Cu')))
    check(refs['In1.Cu'] == ['F.Cu', 'In2.Cu'], "inner layer references both",
          str(refs.get('In1.Cu')))

    # Sampling: n+1 points over n intervals, endpoints exact.
    pts = C._sample_points(0.0, 0.0, 1.0, 0.0, 0.15)
    check(pts[0][:2] == (0.0, 0.0) and close(pts[-1][0], 1.0, 1e-12),
          "sample points span the segment exactly")
    check(close(pts[-1][2], 1.0, 1e-12), "arc length reaches the segment length")

    pct = C._percentiles([1.0, 2.0, 3.0, 4.0, 5.0])
    check(pct['p50'] == 3.0, "median percentile", str(pct))
    check(C._percentiles([]) == {}, "empty percentiles are empty")


def test_calculate_width_selects_model():
    """calculate_width_for_impedance must switch models on coplanar_gap, and
    only on outer layers."""
    print("model selection in calculate_width_for_impedance")

    class SL:
        def __init__(self, name, ltype, thick, eps=0.0):
            self.name, self.layer_type, self.thickness = name, ltype, thick
            self.epsilon_r, self.loss_tangent, self.material = eps, 0.0, ''

    class BI:
        stackup = [SL('F.Cu', 'copper', 0.035), SL('d1', 'prepreg', 0.2, 4.3),
                   SL('In1.Cu', 'copper', 0.0152), SL('d2', 'core', 0.2, 4.3),
                   SL('B.Cu', 'copper', 0.035)]
        copper_layers = ['F.Cu', 'In1.Cu', 'B.Cu']

    class PCB:
        board_info = BI()

    pcb = PCB()
    plain = I.calculate_width_for_impedance(pcb, 'F.Cu', 50.0)
    cop = I.calculate_width_for_impedance(pcb, 'F.Cu', 50.0, coplanar_gap=0.2)
    check(plain.get('is_coplanar') is False, "no gap -> microstrip")
    check(cop.get('is_coplanar') is True, "gap -> coplanar")
    check(cop['calculated_width_mm'] < plain['calculated_width_mm'],
          "coplanar width is narrower",
          f"{cop['calculated_width_mm']:.4f} vs {plain['calculated_width_mm']:.4f}")

    inner = I.calculate_width_for_impedance(pcb, 'In1.Cu', 50.0, coplanar_gap=0.2)
    check(inner.get('is_coplanar') is False,
          "inner layer ignores the coplanar gap (CPWG models ONE plane)")

    # The layer-widths dict used by routing honours the same switch.
    lw_plain = I.calculate_layer_widths_for_impedance(pcb, ['F.Cu', 'In1.Cu'], 50.0)
    lw_cop = I.calculate_layer_widths_for_impedance(pcb, ['F.Cu', 'In1.Cu'], 50.0,
                                                    coplanar_gap=0.2)
    check(lw_cop['F.Cu'] < lw_plain['F.Cu'], "layer dict narrows the outer layer")
    check(close(lw_cop['In1.Cu'], lw_plain['In1.Cu'], 1e-12),
          "layer dict leaves the inner layer untouched")


def test_epsilon_eff_backcompat():
    print("get_layer_epsilon_eff back-compat")

    class SL:
        def __init__(self, name, ltype, thick, eps=0.0):
            self.name, self.layer_type, self.thickness = name, ltype, thick
            self.epsilon_r, self.loss_tangent, self.material = eps, 0.0, ''

    class BI:
        stackup = [SL('F.Cu', 'copper', 0.035), SL('d1', 'prepreg', 0.2, 4.3),
                   SL('B.Cu', 'copper', 0.035)]
        copper_layers = ['F.Cu', 'B.Cu']

    class PCB:
        board_info = BI()

    pcb = PCB()
    # No width -> the historical flat (er+1)/2, so length matching is unchanged.
    check(close(I.get_layer_epsilon_eff(pcb, 'F.Cu'), (4.3 + 1) / 2, 1e-12),
          "width omitted reproduces the pre-#486 flat value")
    # With a width -> the real width-dependent value, always higher.
    ee_w = I.get_layer_epsilon_eff(pcb, 'F.Cu', width=0.36)
    check(ee_w > (4.3 + 1) / 2, "width-aware er_eff is higher than the flat form",
          f"{ee_w:.4f}")
    check(ee_w < 4.3, "width-aware er_eff stays below er")
    # Coplanar ground sits BETWEEN the two: higher than the flat (er+1)/2 the
    # delay model used to assume, but LOWER than the equivalent microstrip,
    # because the coplanar gaps are air-filled and hold part of the field.
    ee_c = I.get_layer_epsilon_eff(pcb, 'F.Cu', width=0.36, side_gap=0.15)
    check(ee_c > (4.3 + 1) / 2, "coplanar er_eff exceeds the flat (er+1)/2",
          f"{ee_c:.4f}")
    check(ee_c < ee_w, "coplanar er_eff is below the microstrip value "
                       "(air-filled side gaps)", f"{ee_c:.4f} vs {ee_w:.4f}")
    # ...and tightening the gap moves it further toward the air-filled limit.
    ee_tight = I.get_layer_epsilon_eff(pcb, 'F.Cu', width=0.36, side_gap=0.08)
    check(ee_tight < ee_c, "tighter coplanar gap lowers er_eff further",
          f"{ee_tight:.4f} vs {ee_c:.4f}")


def main():
    for fn in (test_elliptic, test_cpwg_limits, test_cpwg_z0_behaviour,
               test_cpwg_applies, test_cpwg_width_solver, test_differential_cpwg,
               test_microstrip_thickness, test_width_scale_is_neutral,
               test_asymmetric_stripline, test_checker_helpers,
               test_calculate_width_selects_model, test_epsilon_eff_backcompat):
        fn()
    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("All #486 impedance-geometry tests passed.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
