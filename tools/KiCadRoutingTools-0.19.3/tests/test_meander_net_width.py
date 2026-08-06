#!/usr/bin/env python3
"""
Regression test for issue #175: length-matching meander clearance must be sized
from the net's ACTUAL routed width (impedance-controlled --impedance layer widths
or a power_net_widths override), not the base config.track_width.

A net routed wider than track_width that then gets length-/time-matched had its
meander arms under-reserved by ~(net_width - track_width)/2, so the meander could
graze neighbors at the routed clearance.

Covers:
  * Single-ended keep-out (get_safe_amplitude_at_point): a wide power net pulls its
    meander back relative to a base-width net, by exactly (net_w - track_width)/2.
  * The common case (net_w == track_width) is byte-identical (no behavior change).
  * Diff-pair keep-out (get_safe_amplitude_for_diff_pair): an impedance-widened
    pair pulls its centerline meander back too.
  * Board-edge keep-out: a meander bump near the board outline is clamped so its
    copper stays edge_clearance inside the bounds (the meander pass used to ignore
    the outline, unlike the A* router, so bumps printed off the edge).

Run with:  python3 tests/test_meander_net_width.py
"""
import math
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, "rust_router"))

from kicad_parser import PCBData, Segment, BoardInfo          # noqa: E402
from routing_config import GridRouteConfig                    # noqa: E402
from length_matching import (                                 # noqa: E402
    get_safe_amplitude_at_point,
    get_safe_amplitude_for_diff_pair,
)

MEANDER_NET = 5
FOREIGN_NET = 99


def _cfg(power_w=None, layer_w=None):
    c = GridRouteConfig()
    c.track_width = 0.15
    c.clearance = 0.15
    c.via_size = 0.6
    c.grid_step = 0.05
    c.layers = ["F.Cu", "B.Cu"]
    c.meander_amplitude = 1.0
    if power_w is not None:
        c.power_net_widths = {MEANDER_NET: power_w}
    if layer_w is not None:
        c.layer_widths = {"F.Cu": layer_w}
    return c


def _pcb_with_neighbor(offset, net_id=FOREIGN_NET):
    """A single foreign track parallel to the meander centerline, offset in +y."""
    seg = Segment(start_x=0.0, start_y=offset, end_x=10.0, end_y=offset,
                  width=0.15, layer="F.Cu", net_id=net_id)
    bi = BoardInfo(layers={}, board_bounds=None, copper_layers=["F.Cu", "B.Cu"])
    return PCBData(board_info=bi, nets={}, footprints={}, vias=[],
                   segments=[seg], pads_by_net={})


# Meander centerline sits at (5, 0) running +x; perpendicular +y aims at the neighbor.
_COMMON = dict(cx=5.0, cy=0.0, ux=1.0, uy=0.0, px=0.0, py=1.0, direction=1,
               max_amplitude=1.0, min_amplitude=0.1, layer="F.Cu")


def _pcb_no_obstacles(bounds=None):
    """Empty board (no copper) with optional board_bounds for edge tests."""
    bi = BoardInfo(layers={}, board_bounds=bounds, copper_layers=["F.Cu", "B.Cu"])
    return PCBData(board_info=bi, nets={}, footprints={}, vias=[],
                   segments=[], pads_by_net={})


def _fail(msg):
    print("FAIL  " + msg)
    sys.exit(1)


def test_board_edge_keepout():
    # No copper anywhere, but the board outline is close on the +y side: the meander
    # must not print past it. Centerline at (5,0), bump goes +y; a 0.4mm-wide net's
    # copper edge (centerline + net_half 0.2) must stay edge_clearance inside max_y.
    # edge_clearance falls back to config.clearance (0.15). So the bump peak must be
    # <= max_y - (0.15 + 0.2). The width-scaled pitch (#501) gives a 0.4 net a 0.4
    # chamfer, so its bump reach floors at 2*0.4 + 0.1 = 0.9 -- max_y=1.3 leaves a
    # 0.95 budget that rejects full amplitude (reach 1.0) but accepts the 0.7 rung.
    max_y = 1.3
    limit = max_y - (0.15 + 0.4 / 2)   # clearance + net_half
    near = get_safe_amplitude_at_point(pcb_data=_pcb_no_obstacles(bounds=(-5.0, -5.0, 15.0, max_y)),
                                       net_id=MEANDER_NET, config=_cfg(power_w=0.4), **_COMMON)
    if not (0 < near <= limit + 1e-9):
        _fail(f"board-edge: amplitude {near} should be clamped to <={limit:.3f} (edge at y={max_y})")
    # With no board_bounds the edge check is skipped -> full amplitude available.
    unbounded = get_safe_amplitude_at_point(pcb_data=_pcb_no_obstacles(bounds=None),
                                            net_id=MEANDER_NET, config=_cfg(power_w=0.4), **_COMMON)
    if unbounded <= near:
        _fail(f"board-edge: unbounded amp {unbounded} should exceed edge-limited {near}")
    # A far edge must not constrain a modest bump at all.
    far = get_safe_amplitude_at_point(pcb_data=_pcb_no_obstacles(bounds=(-5.0, -5.0, 15.0, 50.0)),
                                      net_id=MEANDER_NET, config=_cfg(power_w=0.4), **_COMMON)
    if abs(far - unbounded) > 1e-9:
        _fail(f"board-edge: far edge should not clamp ({far} vs {unbounded})")


def test_single_ended_widths():
    # With the width-scaled pitch (#501) a 0.5 net's chamfer is 0.5, so its bump
    # reach floors at 2*0.5 + 0.1 = 1.1: it either fits near-full amplitude or not
    # at all. At a loose offset both widths meander; at 1.2 only the base width does
    # (the wide net's keep-out AND its pitch-scaled bump both outgrow the gap).
    loose = 2.2
    base = get_safe_amplitude_at_point(pcb_data=_pcb_with_neighbor(loose),
                                       net_id=MEANDER_NET, config=_cfg(), **_COMMON)
    wide_power = get_safe_amplitude_at_point(pcb_data=_pcb_with_neighbor(loose),
                                             net_id=MEANDER_NET,
                                             config=_cfg(power_w=0.5), **_COMMON)
    wide_imp = get_safe_amplitude_at_point(pcb_data=_pcb_with_neighbor(loose),
                                           net_id=MEANDER_NET,
                                           config=_cfg(layer_w=0.5), **_COMMON)

    if not (base > 0 and wide_power > 0):
        _fail(f"expected both to meander at offset {loose} (base={base}, wide={wide_power})")
    if abs(wide_imp - wide_power) > 1e-9:
        _fail(f"impedance layer width should match power width keep-out "
              f"({wide_imp:.4f} vs {wide_power:.4f})")

    # A wide net must REFUSE a gap that only a base-width arm fits: its half-width
    # keep-out contribution grew from track_width/2 to net_w/2 (plus corner bloat),
    # and its minimum bump reach is pitch-scaled.
    for tight in (0.8, 1.2):
        base_tight = get_safe_amplitude_at_point(pcb_data=_pcb_with_neighbor(tight),
                                                 net_id=MEANDER_NET, config=_cfg(), **_COMMON)
        wide_tight = get_safe_amplitude_at_point(pcb_data=_pcb_with_neighbor(tight),
                                                 net_id=MEANDER_NET,
                                                 config=_cfg(power_w=0.5), **_COMMON)
        if not (base_tight > 0 and wide_tight == 0):
            _fail(f"gap {tight}: base should fit ({base_tight}) and wide should not ({wide_tight})")


def test_own_width_override():
    # A segment widened beyond its netclass (no impedance/power config) must still
    # pull its meander back, driven purely by own_width (mirrors obstacle_cache's
    # max(net_w, seg.width)). This is the width the meander arms actually carry.
    # Post-#501 the pull-back shows as refusal: own_width scales the pitch too, so
    # the 0.5-wide arm's minimum bump no longer fits the 1.2 gap the 0.15 arm does.
    offset = 1.2
    base = get_safe_amplitude_at_point(pcb_data=_pcb_with_neighbor(offset),
                                       net_id=MEANDER_NET, config=_cfg(),
                                       own_width=0.15, **_COMMON)
    wide = get_safe_amplitude_at_point(pcb_data=_pcb_with_neighbor(offset),
                                       net_id=MEANDER_NET, config=_cfg(),
                                       own_width=0.5, **_COMMON)
    if not (base > 0 and wide == 0):
        _fail(f"own_width=0.5 should refuse the 1.2 gap the 0.15 arm fits (base={base}, wide={wide})")
    loose = get_safe_amplitude_at_point(pcb_data=_pcb_with_neighbor(2.2),
                                        net_id=MEANDER_NET, config=_cfg(),
                                        own_width=0.5, **_COMMON)
    if not (loose > 0):
        _fail(f"own_width=0.5 should still meander at a loose offset (got {loose})")
    # own_width == track_width must be identical to not passing it at all.
    default = get_safe_amplitude_at_point(pcb_data=_pcb_with_neighbor(offset),
                                          net_id=MEANDER_NET, config=_cfg(), **_COMMON)
    if abs(base - default) > 1e-9:
        _fail(f"own_width==track_width not identical to default ({base} vs {default})")


def test_pad_corner_reach():
    """#526: a RECT pad's corner reaches hypot(sx,sy)/2, not max(sx,sy)/2.
    The old circle model let a meander arm graze a pad corner-on (a13
    ddr-a3 vs C210.2, 59um deep). Place a square foreign pad diagonally off
    the bump path so the CORNER intrudes where the old circle said clear:
    the safe amplitude must shrink versus the same pad rotated 45 degrees
    (circle-equivalent presentation is not available, so compare against a
    FAR pad instead)."""
    from kicad_parser import Pad
    from length_matching import _pad_bounding_radius, ClearanceIndex

    def pad_at(x, y, sx=1.0, sy=1.0, shape='roundrect'):
        p = Pad(pad_number='1', net_id=FOREIGN_NET, net_name='F',
                global_x=x, global_y=y, local_x=0, local_y=0,
                size_x=sx, size_y=sy, shape=shape,
                layers=['F.Cu'], drill=0.0, component_ref='C1')
        bi = BoardInfo(layers={}, board_bounds=None,
                       copper_layers=["F.Cu", "B.Cu"])
        return PCBData(board_info=bi, nets={}, footprints={}, vias=[],
                       segments=[], pads_by_net={FOREIGN_NET: [p]}), p

    # radius model itself
    _, p = pad_at(0, 0)
    if abs(_pad_bounding_radius(p) - (2 ** 0.5) / 2) > 1e-9:
        _fail(f"square pad bounding radius should be half-diagonal, "
              f"got {_pad_bounding_radius(p)}")
    _, pc = pad_at(0, 0, shape='circle')
    if abs(_pad_bounding_radius(pc) - 0.5) > 1e-9:
        _fail("circle pad radius should stay exact size/2")

    # placement: bump goes +y from (5,0); put the pad so its CORNER (at
    # ~0.707 reach) intrudes into the arm corridor while the old 0.5-radius
    # circle cleared it. Center distance from the riser tip region ~1.05.
    pcb, _ = pad_at(5.75, 1.75)
    idx = ClearanceIndex()
    idx.build(pcb, _cfg(), None, None)
    amp_near = get_safe_amplitude_at_point(pcb_data=pcb, net_id=MEANDER_NET,
                                           config=_cfg(), clearance_index=idx,
                                           **_COMMON)
    pcb_far, _ = pad_at(5.75, 4.0)
    idx2 = ClearanceIndex()
    idx2.build(pcb_far, _cfg(), None, None)
    amp_far = get_safe_amplitude_at_point(pcb_data=pcb_far, net_id=MEANDER_NET,
                                          config=_cfg(), clearance_index=idx2,
                                          **_COMMON)
    if not (amp_near < amp_far):
        _fail(f"corner-on pad should shrink the safe amplitude "
              f"(near={amp_near}, far={amp_far})")
    # the corner reach must be respected: with the pad corner at
    # (5.25, 1.25), an arm at full amplitude 1.0 through x=5 would pass
    # within the old model's blind wedge -- the new model must not allow
    # the amplitude that grazes it.
    reach = (2 ** 0.5) / 2
    if amp_near > 1.75 - reach - 0.15:   # pad_cy - reach - (net_half+clr margin-ish)
        _fail(f"amplitude {amp_near} still enters the corner wedge")


def test_common_case_unchanged():
    # net_w == track_width everywhere -> keep-out must be identical to a config with
    # no impedance/power overrides at all. This guards the "byte-identical" promise.
    for offset in (0.6, 0.8, 1.0, 1.2, 1.5):
        plain = get_safe_amplitude_at_point(pcb_data=_pcb_with_neighbor(offset),
                                            net_id=MEANDER_NET, config=_cfg(), **_COMMON)
        # power width equal to track_width, and layer width equal to track_width:
        eq_power = get_safe_amplitude_at_point(pcb_data=_pcb_with_neighbor(offset),
                                               net_id=MEANDER_NET,
                                               config=_cfg(power_w=0.15), **_COMMON)
        eq_layer = get_safe_amplitude_at_point(pcb_data=_pcb_with_neighbor(offset),
                                               net_id=MEANDER_NET,
                                               config=_cfg(layer_w=0.15), **_COMMON)
        if abs(plain - eq_power) > 1e-9 or abs(plain - eq_layer) > 1e-9:
            _fail(f"common case not byte-identical at offset {offset}: "
                  f"plain={plain}, eq_power={eq_power}, eq_layer={eq_layer}")


def test_diff_pair_widths():
    # Diff-pair centerline meander: get_safe_amplitude_for_diff_pair takes an int
    # layer index and P/N net ids. A wider pair must pull its meander back too.
    p_net, n_net = 5, 6
    spacing = 0.2  # P/N offset from centerline
    offset = 1.2   # close enough that the neighbor constrains the meander amplitude

    def cfg_pair(width):
        c = _cfg()
        if width != c.track_width:
            c.power_net_widths = {p_net: width, n_net: width}
        return c

    common = dict(cx=5.0, cy=0.0, ux=1.0, uy=0.0, px=0.0, py=1.0, direction=1,
                  max_amplitude=1.0, min_amplitude=0.1, layer=0,
                  p_net_id=p_net, n_net_id=n_net, spacing_mm=spacing)

    base = get_safe_amplitude_for_diff_pair(pcb_data=_pcb_with_neighbor(offset),
                                            config=cfg_pair(0.15), **common)
    wide = get_safe_amplitude_for_diff_pair(pcb_data=_pcb_with_neighbor(offset),
                                            config=cfg_pair(0.5), **common)
    if not (base > 0 and wide > 0):
        _fail(f"diff pair: expected both to meander (base={base}, wide={wide})")
    if not (wide < base):
        _fail(f"diff-pair wide net should pull meander back (base={base}, wide={wide})")


def test_oval_pad_is_bounded_exactly_not_by_the_diagonal():
    """#504 follow-up to #526: an OVAL pad is a stadium -- a rect with
    semicircular ends -- so its farthest copper is the end cap on the long axis
    and its exact bounding radius is max(sx,sy)/2, same as a circle. It has no
    corner to reach.

    _pad_bounding_radius originally special-cased only 'circle', so every oval
    was inflated by up to sqrt(2). Oval is the shape of essentially every
    through-hole pad: across kicad_files/ all 1225 of them were over-modelled,
    mean +274um, worst +966um (kit-dev J201.2, 5.0x4.8: 2.5 -> 3.466). That
    also contradicted the convention the rest of the repo applies -- see
    check_drc, obstacle_map, obstacle_cache, and diff_pair_routing's
    `short if pad.shape in ('circle','oval') else short * sqrt(2)`, which is
    this same corner correction."""
    from length_matching import _pad_bounding_radius

    class _P:
        def __init__(s, shape, sx, sy):
            s.shape, s.size_x, s.size_y = shape, sx, sy

    for shape, sx, sy in (('oval', 2.4, 2.4), ('oval', 5.0, 4.801),
                          ('oval', 1.7, 3.4), ('circle', 1.7, 1.7)):
        got = _pad_bounding_radius(_P(shape, sx, sy))
        want = max(sx, sy) / 2.0
        if abs(got - want) > 1e-9:
            _fail(f"{shape} {sx}x{sy}: radius {got:.4f}, want exactly {want:.4f}")

    # rect/roundrect must KEEP the half-diagonal -- that is #526's fix.
    for shape in ('rect', 'roundrect'):
        got = _pad_bounding_radius(_P(shape, 1.09, 1.14))
        want = math.hypot(1.09, 1.14) / 2.0
        if abs(got - want) > 1e-9:
            _fail(f"{shape}: radius {got:.4f}, want the half-diagonal {want:.4f}")


if __name__ == "__main__":
    test_single_ended_widths()
    test_own_width_override()
    test_common_case_unchanged()
    test_diff_pair_widths()
    test_board_edge_keepout()
    test_pad_corner_reach()
    test_oval_pad_is_bounded_exactly_not_by_the_diagonal()
    print("PASS  meander keep-out sized from actual net width (#175)")
