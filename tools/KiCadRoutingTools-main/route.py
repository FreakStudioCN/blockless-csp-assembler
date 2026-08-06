"""
Batch PCB Router using Rust-accelerated A* - Routes single-ended nets sequentially.

For differential pair routing, use route_diff.py instead.

Usage:
    python route.py input.kicad_pcb output.kicad_pcb --nets "Net-(U2A-*)"

Requires the Rust router module. Build it with:
    python3 build_router.py
(never bare `cargo build` -- build_router.py also places the library and
verifies the version; see CLAUDE.md).
"""
from __future__ import annotations

import env_knobs
import sys
import os
import copy

# Run startup checks before other imports
from startup_checks import exit_on_error_if_main
# Stays at module scope, ABOVE the heavy imports, so a missing dep is
# reported before numpy/grid_router blow up with something cryptic. But it
# raises instead of exiting when this module is IMPORTED rather than run,
# so pytest can still collect a suite on a checkout with no built router
# (#457 item 3).
exit_on_error_if_main(__name__)

import time
import fnmatch
import json
from typing import List, Optional, Tuple, Dict, Set

from kicad_parser import parse_kicad_pcb, PCBData, Pad
from kicad_writer import (
    generate_segment_sexpr, generate_via_sexpr, generate_gr_line_sexpr, generate_gr_text_sexpr,
    swap_segment_nets_at_positions, swap_via_nets_at_positions, swap_pad_nets_in_content,
    modify_segment_layers
)
from output_writer import write_routed_output
from cleanup_pipeline import (run_post_route_cleanup, verify_board_file_parity,
                              verify_written_file_parity)
from schematic_updater import apply_swaps_to_schematics

# Import from refactored modules
from routing_config import GridRouteConfig, GridCoord
from connectivity import (
    get_stub_endpoints, find_stub_free_ends, find_connected_groups,
    is_edge_stub, get_net_endpoints, find_connected_segment_positions
)
from net_queries import (
    get_all_unrouted_net_ids, get_chip_pad_positions,
    compute_mps_net_ordering, find_pad_nearest_to_position,
    expand_net_patterns, find_single_ended_nets, identify_power_nets,
    filter_routable_nets
)
from impedance import calculate_layer_widths_for_impedance, print_impedance_routing_plan
from obstacle_map import (
    build_base_obstacle_map, add_net_stubs_as_obstacles, add_net_pads_as_obstacles,
    add_net_vias_as_obstacles, add_same_net_via_clearance,
    get_net_bounds,
    draw_exclusion_zones_debug, add_vias_list_as_obstacles, add_segments_list_as_obstacles
)
from obstacle_costs import (
    add_stub_proximity_costs, compute_track_proximity_for_net,
    merge_track_proximity_costs, add_cross_layer_tracks
)
from obstacle_cache import (
    precompute_all_net_obstacles, build_working_obstacle_map, update_net_obstacles_after_routing
)
from single_ended_routing import (route_net_with_obstacles,
                                   route_multipoint_taps, build_corridor_waypoints)
from blocking_analysis import analyze_frontier_blocking, print_blocking_analysis, filter_rippable_blockers
from rip_up_reroute import rip_up_net, restore_net
from layer_swap_optimization import apply_single_ended_layer_swaps
from routing_context import (
    build_single_ended_obstacles,
    record_single_ended_success,
    restore_ripped_net
)
from routing_state import RoutingState, create_routing_state, print_failed_net_histories
from memory_debug import (
    get_process_memory_mb, format_memory_stats,
    estimate_net_obstacles_cache_mb, estimate_track_proximity_cache_mb,
    estimate_routed_paths_mb, format_obstacle_map_stats
)
from single_ended_loop import route_single_ended_nets
from reroute_loop import run_reroute_loop
from phase3_routing import run_phase3_tap_routing
from net_ordering import order_nets_mps, order_nets_inside_out
from routing_common import (
    setup_bga_exclusion_zones, resolve_net_ids, filter_already_routed,
    run_length_matching, sync_pcb_data_segments,
    get_common_config_kwargs, warn_targets_outside_board
)
import routing_defaults as defaults
import re
from terminal_colors import RED, RESET
from routing_constants import DEFAULT_4_LAYER_STACK, POWER_NET_EXCLUSION_PATTERNS

# Import Rust router (startup_checks ensures it's available and up-to-date)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'rust_router'))
import rust_alloc  # noqa: E402,F401  # issue #419: set MIMALLOC_PURGE_DELAY before grid_router loads
from grid_router import GridObstacleMap, GridRouter


def _compute_stale_input_copper(orig_by_net, scope_ids, final_copper,
                                emitted_copper, full_sig, pos_sig,
                                final_ids=None):
    """Shared core for #284 stale-copper stripping (vias and segments).

    The output writer copies the input file verbatim, then APPENDS the routing
    results' copper (each result's new segments/vias plus stub layer-swap
    copper). So an original item of a ripped/re-routed net can ship TWICE, or
    next to a replacement, leaving redundant same-net copper stacked on itself.

    Strip an in-scope net's original item when EITHER:

    - it is no longer on the final (frozen committed) board -- the net was
      rerouted away or ripped-and-not-restored. With ``final_ids`` (object
      identity at freeze time) the test is exact; rip/restore preserves object
      identity (restore_net re-adds the saved objects), so a kept original
      always passes. The signature fallback (when ``final_ids`` is None) has a
      TWIN-SHIELDING hole: an original whose net was ripped and re-routed onto
      the byte-identical span is "matched" by the ROUTED twin's signature and
      kept -- and when a later cleanup pass (the cycle prune) removes that
      routed twin, the original ships as copper the board model no longer has
      (neo6502 +3.3V slivers, found by KICAD_BOARD_LEDGER).
    - the writer will RE-EMIT an item for the SAME net at the SAME POSITION: the
      re-emitted item is authoritative, so the verbatim original is the redundant
      copy (this covers a byte-identical replacement, which a full-signature match
      alone treats as "kept").

    ``full_sig`` distinguishes a superseded item from a kept one; ``pos_sig`` is
    the coarser locus a replacement lands on (via drill hole / segment span).
    """
    final = {}
    if final_ids is None:
        for c in final_copper:
            final.setdefault(c.net_id, set()).add(full_sig(c))
    emit = {}
    for c in emitted_copper:
        emit.setdefault(c.net_id, set()).add(pos_sig(c))
    stale = []
    for nid in scope_ids:
        nfinal = final.get(nid, ())
        nemit = emit.get(nid, ())
        for c in orig_by_net.get(nid, []):
            on_board = (id(c) in final_ids) if final_ids is not None \
                else (full_sig(c) in nfinal)
            if not on_board or pos_sig(c) in nemit:
                stale.append(c)
    return stale


def _via_full_sig(v):
    return (round(v.x, 3), round(v.y, 3), round(v.size, 3), round(v.drill, 3))


def _via_pos_sig(v):
    # A via's drill hole is a point: two same-net vias at one (x, y) overlap
    # regardless of size, and the drill hole-to-hole check is net-independent.
    return (round(v.x, 3), round(v.y, 3))


def _seg_span_sig(s):
    a = (round(s.start_x, 3), round(s.start_y, 3))
    b = (round(s.end_x, 3), round(s.end_y, 3))
    return (min(a, b), max(a, b), s.layer)


def compute_stale_input_vias(orig_via_by_net, scope_ids, final_vias, emitted_vias,
                             final_ids=None):
    """Original input vias to strip from the verbatim output copy (issue #284).

    A ripped/re-routed net's reroute often lands a via-in-pad via at the EXACT
    position of the net's original via; without stripping the original, the
    output ships two same-net vias in one hole -- and the drill hole-to-hole
    check is net-independent, so this violates DRC whether the two are the same
    size or not. See ``_compute_stale_input_copper``.
    """
    return _compute_stale_input_copper(
        orig_via_by_net, scope_ids, final_vias, emitted_vias,
        full_sig=_via_full_sig, pos_sig=_via_pos_sig, final_ids=final_ids)


def compute_stale_input_segments(orig_seg_by_net, scope_ids, final_segments,
                                 emitted_segments, final_ids=None):
    """Original input segments to strip from the verbatim output copy (issue
    #284, segment twin of ``compute_stale_input_vias``).

    Unlike vias, two exactly-overlapping same-net segments are DRC-benign (KiCad
    permits same-net copper overlap and there is no net-independent segment
    check), so this is a cleanliness fix, not a DRC one -- it keeps a ripped/
    re-routed net from shipping a verbatim original segment next to a byte-
    identical re-emitted copy. A segment is keyed by span+layer for both roles
    (the reroute either reproduces the span exactly or routes a different one).
    """
    return _compute_stale_input_copper(
        orig_seg_by_net, scope_ids, final_segments, emitted_segments,
        full_sig=_seg_span_sig, pos_sig=_seg_span_sig, final_ids=final_ids)


def _write_passthrough_output(input_file: str, output_file: str) -> None:
    """Write the output as an unchanged copy of the input (issue #86).

    When routing produced nothing - no valid nets, or everything already
    connected - the result is still representable: the board is unchanged.
    Pipelines that chain output->input then keep working instead of dying on
    a missing file. Skipped when output is the input (in-place).
    """
    from pcb_io_utils import passthrough_copy
    if passthrough_copy(input_file, output_file):
        print(f"Wrote unchanged copy to {output_file} (nothing to route)")


def _dump_engine_config(engine, cfg):
    """Config-parity probe for the plane engines (#362), mirroring batch_route's
    dump. Only active in APPEND/CONTINUE mode (KICAD_DUMP_BATCH_KWARGS +
    KICAD_DUMP_BATCH_KWARGS_CONTINUE=1): writes one JSONL line per engine call
    and never alters routing, so a whole GUI plan run is captured in one pass."""
    if not (env_knobs.DUMP_BATCH_KWARGS and env_knobs.DUMP_BATCH_KWARGS_CONTINUE):
        return
    import json as _json
    d = {'_engine': engine}
    for k, v in cfg.items():
        # Skip the board payload and non-config callables. all_layers/plane_layers
        # ARE kept: layer ORDER is a live GUI/CLI divergence class (the pcbnew
        # layer-ID vs stackup-order bug), so it must be visible in the dump.
        # progress_callback is skipped by NAME, not just callable(): the CLI
        # passes None (not callable, would dump as null) while the GUI passes
        # a function (skipped) -- a phantom key diff in the parity harness.
        if k in ('input_file', 'output_file', 'pcb_data',
                 'progress_callback', 'cancel_check') or callable(v):
            continue
        try:
            _json.dumps(v)
            d[k] = v
        except (TypeError, ValueError):
            d[k] = repr(v)
    try:
        with open(env_knobs.DUMP_BATCH_KWARGS, 'a') as _f:
            _f.write(_json.dumps(d, sort_keys=True) + '\n')
    except Exception:
        pass


def _empty_results_data() -> dict:
    """The return_results contract with every field empty (#382 E5).

    batch_route's early-return paths (nothing to route, KWARGS-dump exit) used
    to emit ad-hoc subsets of these keys, so a GUI caller that iterated a key
    the full path always provides would KeyError on an early exit. This is the
    single source for the empty shape; it must carry EXACTLY the keys the full
    path builds (see the `if return_results:` block), all as empty lists.
    """
    return {
        'results': [],
        'all_swap_vias': [],
        'all_swap_segments': [],
        'pad_swaps': [],
        'single_ended_target_swap_info': [],
        'all_segment_modifications': [],
        'exclusion_zone_lines': [],
        'boundary_debug_labels': [],
        'segments_to_remove': [],
        'vias_to_remove': [],
        'blockers': [],
        'pad_pairs_open': [],
    }



# #562 finalize re-entry guard. batch_route's plane finalize calls
# repair_planes, whose own rip-casualty / pad-repair sub-runs call batch_route
# again -- if such a sub-run finalizes, the two recurse without bound
# (measured 57 levels and ~7 GB on one corpus board). The sub-runs now pass
# final_reconcile=False, and this flag is the structural backstop so a future
# caller that forgets cannot reintroduce the recursion.
_PLANE_FINALIZE_DEPTH = 0


def _finalize_depth(delta: int) -> None:
    global _PLANE_FINALIZE_DEPTH
    _PLANE_FINALIZE_DEPTH = max(0, _PLANE_FINALIZE_DEPTH + delta)


def _plane_finalize_active() -> bool:
    return _PLANE_FINALIZE_DEPTH > 0


def batch_route(input_file: str, output_file: str, net_names: List[str],
                layers: List[str] = None,
                bga_exclusion_zones: Optional[List[Tuple[float, float, float, float]]] = None,
                direction_order: str = None,
                ordering_strategy: str = "inside_out",
                disable_bga_zones: Optional[List[str]] = None,
                track_width: float = defaults.TRACK_WIDTH,
                track_width_from_class: bool = False,
                impedance: Optional[float] = None,
                coplanar_gap: float = 0.0,
                coplanar_nets: Optional[List[str]] = None,
                power_nets: Optional[List[str]] = None,
                power_nets_widths: Optional[List[float]] = None,
                power_tap_neckdown: bool = True,
                neckdown_length: float = 2.5,
                neckdown_taper_length: float = 0.5,
                clearance: float = defaults.CLEARANCE,
                via_size: float = defaults.VIA_SIZE,
                via_drill: float = defaults.VIA_DRILL,
                grid_step: float = 0.1,
                via_cost: int = 50,
                max_iterations: int = 200000,
                max_probe_iterations: int = 5000,
                heuristic_weight: float = 1.9,
                turn_cost: int = 1000,
                direction_preference_cost: int = defaults.DIRECTION_PREFERENCE_COST,
                bus_enabled: bool = False,
                bus_detection_radius: float = 5.0,
                bus_attraction_radius: float = 5.0,
                bus_attraction_bonus: int = 5000,
                bus_min_nets: int = 2,
                guide_corridor_enabled: bool = False,
                guide_corridor_layer: str = "User.1",
                guide_corridor_spacing: float = 0.0,
                keepout_enabled: bool = False,
                keepout_layer: str = "User.2",
                proximity_heuristic_factor: float = 0.02,
                stub_proximity_radius: float = 2.0,
                stub_proximity_cost: float = 0.2,
                via_proximity_cost: float = 10.0,
                bga_proximity_radius: float = 7.0,
                bga_proximity_cost: float = 0.2,
                track_proximity_distance: float = 2.0,
                track_proximity_cost: float = defaults.TRACK_PROXIMITY_COST,
                debug_lines: bool = False,
                verbose: bool = False,
                max_rip_up_count: int = 3,
                ripup_abandon_metric: str = 'stranded',
                ripup_blocker_select: str = 'count',
                enable_layer_switch: bool = True,
                crossing_layer_check: bool = True,
                can_swap_to_top_layer: bool = False,
                swappable_net_patterns: Optional[List[str]] = None,
                crossing_penalty: float = 1000.0,
                mps_unroll: bool = True,
                skip_routing: bool = False,
                routing_clearance_margin: float = 1.0,
                hole_to_hole_clearance: float = defaults.HOLE_TO_HOLE_CLEARANCE,
                board_edge_clearance: float = 0.0,
                vertical_attraction_radius: float = 1.0,
                vertical_attraction_cost: float = 0.0,
                ripped_route_avoidance_radius: float = 1.0,
                ripped_route_avoidance_cost: float = 0.1,
                length_match_groups: Optional[List[List[str]]] = None,
                length_match_tolerance: float = 0.1,
                meander_amplitude: float = 1.0,
                meander_spacing: float = 2.0,
                time_matching: bool = False,
                time_match_tolerance: float = 1.0,
                debug_memory: bool = False,
                mps_reverse_rounds: bool = False,
                mps_layer_swap: bool = False,
                mps_segment_intersection: bool = False,
                minimal_obstacle_cache: bool = False,
                schematic_dir: Optional[str] = None,
                layer_costs: Optional[List[float]] = None,
                # #498: {layer: mm} per-layer clearance. None (both fronts) ->
                # auto-read the sibling .kicad_dru; explicit dict (tests) wins.
                layer_clearances: Optional[Dict[str, float]] = None,
                final_reconcile: bool = True,
                add_teardrops: bool = False,
                collect_stats: bool = False,
                cancel_check=None,
                progress_callback=None,
                return_results: bool = False,
                pcb_data=None,
                net_clearances: dict = None,
                keep_input_copper: bool = False,
                rip_existing_nets: Optional[List[str]] = None,
                force_reroute: bool = False,
                # The RAW --nets patterns as the operator typed them, BEFORE
                # main()'s expand_net_patterns. #521's protection override is
                # "named EXACTLY, no glob" -- but expansion turns '/LED_*' into
                # exact names, so the engine can't tell a typed name from a
                # glob hit without this. None (the GUI, tests) = net_names ARE
                # the operator's literal selection (per-net checkboxes).
                net_name_patterns: Optional[List[str]] = None,
                # #540 item 2: corridor ghosts of nets ripped by an OUTER pass
                # (the plane repair) and still awaiting reconnect --
                # {net_id: {'new_segments': [...], 'new_vias': [...]}}. Seeded
                # into the ripped-route avoidance dicts so every search in
                # this batch prices the vacated corridors exactly like an
                # internal rip's; keys must not be nets routed by this batch.
                external_ripped_ghosts: Optional[Dict[int, dict]] = None,
                # #562 GUI parity: zero-arg callable returning a path to a
                # freshly saved copy of the caller's LIVE board (the GUI's
                # pcbnew.SaveBoard). Only the GUI can produce one, and the
                # plane finalize's ORACLE leg needs a real file because
                # kicad-cli computes the exact fill. With it the GUI runs the
                # oracle IN-RUN at the CLI's own sequence point -- so oracle
                # copper lands before the final reconciliation and its
                # unroutable links feed custody, exactly as in file mode.
                # Without it (tests, headless callers) the leg is skipped.
                # NOTE: it must save the LIVE board, not re-read input_file --
                # the GUI's input_file is the ORIGINAL on disk and does not
                # carry copper earlier chain steps applied in-session.
                stage_board_fn=None) -> Tuple[int, int, float]:
    """
    Route single-ended nets using the Rust router.

    For differential pair routing, use route_diff.py instead.

    Every run ends with the in-run PLANE FINALIZE (#562, gate
    KICAD_PLANE_FINALIZE=1 default ON): for zone nets in this run's net
    scope it runs the repair engine (taps + region joins), the shared
    cleanup pipeline, and the kicad-cli oracle verify, then folds stubborn
    oracle links into the final reconciliation as custody. The standalone
    repair step no longer exists in the chain; a pour is only welded by a
    route step whose --nets cover the plane nets.

    Args:
        input_file: Path to input KiCad PCB file
        output_file: Path to output KiCad PCB file
        net_names: List of net names to route
        layers: List of copper layers to route on (must be specified - cannot auto-detect
                which layers are ground planes vs signal layers)
        bga_exclusion_zones: Optional list of BGA exclusion zones (auto-detected if None)
        direction_order: Direction search order - "forward" or "backward"
                        (None = use GridRouteConfig default)
        ordering_strategy: Net ordering strategy:
            - "mps": Use Maximum Planar Subset algorithm to minimize crossing conflicts (default)
            - "inside_out": Sort BGA nets by distance from BGA center
            - "original": Keep nets in original order
        track_width: Track width in mm (default: defaults.TRACK_WIDTH, 0.3)
        clearance: Clearance between tracks in mm (default: defaults.CLEARANCE, 0.25)
        via_size: Via outer diameter in mm (default: defaults.VIA_SIZE, 0.5)
        via_drill: Via drill size in mm (default: defaults.VIA_DRILL, 0.3)
        grid_step: Grid resolution in mm (default: 0.1)
        via_cost: Penalty for placing a via in 0.1mm grid steps (default: 50 = 5mm; mm-equivalent at any grid_step)
        max_iterations: Max A* iterations before giving up (default: 200000)
        heuristic_weight: A* heuristic weight, higher=faster but less optimal (default: 1.9)
        stub_proximity_radius: Radius around stubs to penalize in mm (default: 2.0)
        stub_proximity_cost: Cost penalty near stubs in mm equivalent (default: 0.2)
        bga_proximity_radius: Radius around BGA edges to penalize in mm (default: 7.0)
        bga_proximity_cost: Cost penalty near BGA edges in mm equivalent (default: 0.2)
        debug_lines: Output debug geometry on User.3 (connectors), User.4
                     (stub dirs), User.8 (simplified), User.9 (raw A*)
        minimal_obstacle_cache: If True, only build obstacle cache for nets being routed
                               (faster when re-routing a small number of nets)
        cancel_check: Optional callable returning True if routing should be cancelled
        progress_callback: Optional callable(current, total, net_name) for progress updates
        return_results: If True, return results data instead of writing to file

    Returns:
        If return_results=False: (successful_count, failed_count, total_time)
        If return_results=True: (successful_count, failed_count, total_time, results_data)
    """
    # Snapshot of THIS call's parameters, taken before any body code runs:
    # the end-of-run reconciliation self-invocation forwards every parameter
    # verbatim (only overriding the self-referential ones) so a rescue pass
    # can never route with different rules than the run it is rescuing --
    # forwarding a hand-picked subset silently dropped board_edge_clearance,
    # impedance, net_clearances, ordering and more (review finding).
    _reconcile_kwargs = dict(locals())
    # net_name_patterns must not forward either: the reconcile sub-run scopes
    # by exact retried-net names, which are then the correct override source.
    for _k in ('input_file', 'output_file', 'net_names', 'pcb_data',
               'net_name_patterns'):
        _reconcile_kwargs.pop(_k, None)
    if env_knobs.DUMP_BATCH_KWARGS:
        # Parameter-parity probe: dump THIS call's full parameter set so the
        # CLI front (argparse->main) and the GUI front (plan setters->tab
        # config->call site) can be diffed key by key on identical inputs.
        # Default: overwrite the file and RETURN without routing (single-call
        # A/B). CONTINUE mode (KICAD_DUMP_BATCH_KWARGS_CONTINUE=1): APPEND one
        # JSONL line per call and keep routing, so a whole multi-step GUI plan
        # run can be captured in one pass without breaking the chain (#362).
        import json as _json
        _dump = {}
        for _k, _v in sorted(_reconcile_kwargs.items()):
            if callable(_v) or _k in ('cancel_check',
                                      'progress_callback'):
                continue
            try:
                _json.dumps(_v)
                _dump[_k] = _v
            except (TypeError, ValueError):
                _dump[_k] = repr(_v)
        _dump['net_names'] = net_names
        if env_knobs.DUMP_BATCH_KWARGS_CONTINUE:
            with open(env_knobs.DUMP_BATCH_KWARGS, 'a') as _f:
                _f.write(_json.dumps(_dump, sort_keys=True) + '\n')
            # fall through -- route normally
        else:
            with open(env_knobs.DUMP_BATCH_KWARGS, 'w') as _f:
                _json.dump(_dump, _f, indent=1, sort_keys=True)
            if return_results:
                return 0, 0, 0.0, _empty_results_data()
            return 0, 0, 0.0

    # Board-setup copper-to-edge rule (#338): KiCad enforces the sibling
    # .kicad_pro's min_copper_edge_clearance, so route to at least it. Done in
    # the ENGINE (not main()) so the GUI and manifest/plan replays inherit it;
    # a missing project reads 0.0 (no-op) and an explicit larger
    # --board-edge-clearance still wins (max).
    if input_file:
        try:
            from fix_kicad_drc_settings import effective_board_edge_clearance
            _eff_edge = effective_board_edge_clearance(input_file, board_edge_clearance)
            if _eff_edge > (board_edge_clearance or 0.0):
                print(f"Board edge clearance {_eff_edge}mm "
                      f"(project min_copper_edge_clearance)")
                board_edge_clearance = _eff_edge
        except Exception:
            pass
        # Carry the RESOLVED value into the end-of-run reconciliation kwargs:
        # the snapshot above was taken before this resolution, and the
        # reconciliation self-invocation reads the OUTPUT file, whose sibling
        # .kicad_pro does not exist yet (main() writes it after batch_route
        # returns) -- so the sub-run re-resolved 0.0 and stamped its board-edge
        # band at the track-clearance fallback (ottercast_audio BT_PCM_DIN/
        # BT_PCM_SYNC: 16 board-edge violations laid by the reconciliation's
        # phase-1/phase-3 routes inside the 0.5mm project edge band).
        _reconcile_kwargs['board_edge_clearance'] = board_edge_clearance

    # Track memory if debug_memory enabled
    mem_start = get_process_memory_mb() if debug_memory else 0.0
    if debug_memory:
        print(format_memory_stats("Initial memory", mem_start))

    if pcb_data is None:
        print(f"Loading {input_file}...")
        pcb_data = parse_kicad_pcb(input_file, guide_layer=guide_corridor_layer,
                                   keepout_layer=keepout_layer)
    else:
        print("Using provided PCB data...")

    # KICAD_DUP_TRAP=1: report the call site that re-appends the SAME copper
    # object to pcb_data. Inert otherwise. Armed here so it covers the whole
    # run, including the cleanup pipeline and the in-run plane finalize.
    from dup_trap import install as _install_dup_trap
    _install_dup_trap(pcb_data)

    # Canonicalise the STARTING copper order, BEFORE any decision reads it.
    # The GUI adds tracks to a live pcbnew board and the CLI writer emits them
    # from its own lists, so the two fronts hand the engine identical copper in
    # different ORDER (measured on eth_tap: the same 1159-segment set in three
    # different orders across two GUI chains and the CLI). List position then
    # leaks into decisions -- representative endpoints, connected-group order,
    # stub free ends, MST terminal labelling, unit distances -- each of which
    # was fixed individually before the next one surfaced. Doing it once, here,
    # closes the class.
    #
    # Placement matters: this MUST precede apply_single_ended_layer_swaps and
    # the MPS ordering (both far above the routing state setup). An earlier
    # attempt sat next to create_routing_state and had NO effect, because the
    # swap phase had already consumed the un-canonical order.
    from kicad_parser import canonicalize_pcb_data_order
    canonicalize_pcb_data_order(pcb_data)

    # Cross-class clearance: when no map was passed (net_clearances is None -- e.g.
    # the plane routers reroute ripped nets by calling batch_route directly), AUTO-
    # READ the board's non-Default netclasses from the sibling .kicad_pro. #439:
    # cap each class at the routing `clearance` (min) -- stock classes are often
    # aspirational, so the CLI ceiling applies here too. A caller that already
    # resolved the map (route.py main, the GUI) passes a dict (possibly empty) so
    # this does not re-read.
    if net_clearances is None and input_file and os.path.isfile(input_file):
        try:
            from list_nets import net_clearance_map_by_id
            net_clearances = net_clearance_map_by_id(
                input_file, {nid: n.name for nid, n in pcb_data.nets.items()})
            if net_clearances:
                net_clearances = {nid: min(clr, clearance)
                                  for nid, clr in net_clearances.items()}
                print(f"Auto-read netclass clearances for {len(net_clearances)} net(s), "
                      f"capped at clearance {clearance}mm (#439).")
        except Exception as _e:
            print(f"Warning: could not auto-read netclass clearances ({_e}); "
                  f"routing at the uniform clearance.")
            net_clearances = None

    # #435 companion: when --track-width was OMITTED, route each net at its OWN
    # netclass track width (a controlled-impedance signal class or a power class,
    # each with a different width), not the single Default-class width. Explicit
    # --track-width is honored verbatim for all nets (this stays empty). Floored at
    # the fab track minimum; a manual --power-nets-widths override still wins.
    net_track_widths = {}
    if track_width_from_class and input_file and os.path.isfile(input_file):
        try:
            from list_nets import net_track_width_map_by_id, fab_floors
            _twfloor = fab_floors(len(getattr(pcb_data.board_info, 'copper_layers', None)
                                      or []) or 4).get('track_width', 0.0)
            net_track_widths = {nid: max(w, _twfloor) for nid, w in
                                net_track_width_map_by_id(
                                    input_file,
                                    {nid: n.name for nid, n in pcb_data.nets.items()}).items()}
            if net_track_widths:
                print(f"Auto-read netclass track widths for {len(net_track_widths)} "
                      f"non-Default net(s) (#435 single-ended companion).")
        except Exception as _e:
            print(f"Warning: could not auto-read netclass track widths ({_e}).")
            net_track_widths = {}

    # Issue #8: snapshot the input board's copper per net BEFORE any routing.
    # The final connectivity reconciliation reports against the copper that will
    # be WRITTEN (this original copper + the write-list's new copper), not against
    # pcb_data -- which accumulates orphan copper from rip/reroute that never
    # reaches the write-list and would make a net look connected when the output
    # has it split (glasgow /IO_Banks/IO_Buffer_A/P1).
    _orig_seg_by_net: Dict[int, list] = {}
    for _s in pcb_data.segments:
        _orig_seg_by_net.setdefault(_s.net_id, []).append(_s)
    _orig_via_by_net: Dict[int, list] = {}
    for _v in pcb_data.vias:
        _orig_via_by_net.setdefault(_v.net_id, []).append(_v)

    # Layers must be specified - we can't auto-detect which are ground planes
    if layers is None:
        layers = DEFAULT_4_LAYER_STACK
    print(f"Using {len(layers)} routing layers: {layers}")

    # Set default layer costs if not specified
    # 4+ layers: all 1.0 (inner layers available for routing)
    # 2 layers: F.Cu=1.0, B.Cu=3.0 (prefer top layer)
    if not layer_costs:
        if len(layers) >= 4:
            layer_costs = [1.0] * len(layers)
        else:
            layer_costs = [1.0 if layer == 'F.Cu' else 3.0 for layer in layers]

    # Full-stack normalization: the config must ALWAYS carry every board
    # copper layer -- copper on a layer absent from config.layers is invisible
    # to the obstacle maps, yet a via spans the whole stack, so a run invoked
    # with a layer subset on a 6/8-layer board could drop vias straight onto
    # unseen inner copper (butterstick DQ11: a rescue via on In3 +3V3 copper,
    # a real kicad clearance violation). Board layers the caller did not
    # request are APPENDED with FORBIDDEN cost (-1): no routed copper, but
    # their copper blocks vias and through-vias may span them (the documented
    # --layer-costs -1 semantics). Requested layers keep their order and
    # costs, so index-derived behavior (H/V direction preferences) is
    # unchanged for them.
    _board_cu = list(getattr(pcb_data.board_info, 'copper_layers', None) or [])
    _missing_cu = [l for l in _board_cu if l not in layers]
    if _missing_cu:
        from routing_constants import FORBIDDEN_LAYER_COST
        layers = list(layers) + _missing_cu
        layer_costs = list(layer_costs) + [FORBIDDEN_LAYER_COST] * len(_missing_cu)
        print(f"  Full-stack: appended {len(_missing_cu)} unrequested copper layer(s) "
              f"as FORBIDDEN obstacles (no routing, vias respect their copper): "
              f"{', '.join(_missing_cu)}")

    # Validate layer costs: any negative = forbidden (no copper placed; still an
    # obstacle), otherwise a multiplier in [1.0, 1000].
    for i, cost in enumerate(layer_costs):
        if cost >= 0 and (cost < 1.0 or cost > 1000):
            layer_name = layers[i] if i < len(layers) else f"layer {i}"
            from routing_exceptions import ConfigurationError
            raise ConfigurationError(f"Layer cost for {layer_name} must be negative (forbidden) or "
                                     f"between 1.0 and 1000, got {cost}")

    costs_str = ', '.join(f"{layers[i]}={layer_costs[i]}x" for i in range(min(len(layers), len(layer_costs))))
    print(f"  Layer costs: {costs_str}")

    # Calculate layer-specific widths for impedance-controlled routing
    layer_widths = {}
    coplanar_layer_widths = {}
    coplanar_net_ids = set()
    if impedance is None and coplanar_gap:
        # The coplanar gap only changes which IMPEDANCE formula picks the width.
        # With no target it has nothing to act on, and silently ignoring it
        # would leave the caller believing they routed a CPW (#486).
        print("WARNING: --coplanar-gap given without --impedance; it only "
              "selects the impedance model, so it has NO effect here. Add "
              "--impedance <ohms> to route as a coplanar waveguide.")
    if impedance is not None:
        if not pcb_data.board_info.stackup:
            print("WARNING: No stackup found in PCB file. Using fixed track width.")
        else:
            # #486: which nets run through a ground pour on their own layer?
            # An empty coplanar_nets with a gap set means "all of them", so the
            # base width dict itself is CPW-derived; a net list means only
            # those get CPW widths and everyone else stays microstrip.
            _cop_all = bool(coplanar_gap and coplanar_gap > 0 and not coplanar_nets)
            _cop_some = bool(coplanar_gap and coplanar_gap > 0 and coplanar_nets)

            print(f"\nCalculating trace widths for {impedance}Ω single-ended impedance...")
            layer_widths = calculate_layer_widths_for_impedance(
                pcb_data, layers, impedance,
                spacing=0.0, is_differential=False,
                fallback_width=track_width,
                min_width=track_width,
                coplanar_gap=coplanar_gap if _cop_all else 0.0
            )
            print_impedance_routing_plan(pcb_data, layers, impedance, is_differential=False,
                                        min_width=track_width,
                                        coplanar_gap=coplanar_gap if _cop_all else 0.0)

            if _cop_some:
                coplanar_net_ids = {nid for _nm, nid in
                                    resolve_net_ids(pcb_data, coplanar_nets)}
                if not coplanar_net_ids:
                    print(f"WARNING: --coplanar-nets matched no nets: "
                          f"{' '.join(coplanar_nets)} (widths stay microstrip)")
                else:
                    coplanar_layer_widths = calculate_layer_widths_for_impedance(
                        pcb_data, layers, impedance,
                        spacing=0.0, is_differential=False,
                        fallback_width=track_width,
                        min_width=track_width,
                        coplanar_gap=coplanar_gap
                    )
                    print(f"\n{len(coplanar_net_ids)} net(s) declared coplanar "
                          f"(gap {coplanar_gap}mm):")
                    print_impedance_routing_plan(pcb_data, layers, impedance,
                                                 is_differential=False,
                                                 min_width=track_width,
                                                 coplanar_gap=coplanar_gap)

    # #521: impedance declarations persist per net. A step routed WITH
    # --impedance records {net: ohms/pair_gap/coplanar_gap} (persisted next to
    # the DRC writeback); a later step touching those nets WITHOUT --impedance
    # recomputes the SAME widths from the stackup and applies them per-net, so
    # a rip/reroute keeps the impedance geometry instead of silently dropping
    # to this call's default width.
    net_layer_widths_map: Dict[int, Dict[str, float]] = {}
    _targets = resolve_net_ids(pcb_data, net_names) if net_names else []
    if impedance is not None:
        from protected_nets import note_impedance_specs
        note_impedance_specs({
            _nm: {'ohms': impedance, 'differential': False,
                  'coplanar_gap': (coplanar_gap if (coplanar_gap and coplanar_gap > 0
                                   and (not coplanar_nets or _nid in coplanar_net_ids))
                                   else 0.0)}
            for _nm, _nid in _targets})
    elif pcb_data.board_info.stackup:
        from protected_nets import read_impedance_for_pcb_data
        _stored = read_impedance_for_pcb_data(pcb_data, input_file)
        _redo = [(nm, nid, _stored[nm]) for nm, nid in _targets if nm in _stored]
        if _redo:
            # One width map per distinct declaration; per-net application via
            # config.net_layer_widths (single-ended engine margins ride
            # get_net_track_width, so per-net widths need no stamp change).
            _by_spec: Dict[tuple, list] = {}
            for nm, nid, sp in _redo:
                key = (float(sp.get('ohms', 0) or 0), bool(sp.get('differential')),
                       float(sp.get('pair_gap', 0) or 0), float(sp.get('coplanar_gap', 0) or 0))
                _by_spec.setdefault(key, []).append((nm, nid))
            for (ohms, is_diff, pair_gap, cop_gap), members in sorted(_by_spec.items()):
                if not ohms:
                    continue
                widths = calculate_layer_widths_for_impedance(
                    pcb_data, layers, ohms,
                    spacing=pair_gap, is_differential=is_diff,
                    fallback_width=track_width, min_width=track_width,
                    coplanar_gap=cop_gap)
                for _nm, _nid in members:
                    net_layer_widths_map[_nid] = widths
                kind = 'differential' if is_diff else 'single-ended'
                cop = f", coplanar gap {cop_gap}mm" if cop_gap else ""
                print(f"  Reapplying stored {ohms:g} ohm {kind} impedance widths to "
                      f"{len(members)} net(s){cop} (recorded in .kicad_pro by an "
                      f"earlier --impedance step)")

    # Auto-detect BGA exclusion zones if not specified
    _sel_ids = [nid for _nm, nid in _targets]
    bga_exclusion_zones = setup_bga_exclusion_zones(
        pcb_data, disable_bga_zones, bga_exclusion_zones,
        selected_net_ids=_sel_ids)

    config_kwargs = get_common_config_kwargs(
        track_width=track_width, clearance=clearance, via_size=via_size,
        via_drill=via_drill, grid_step=grid_step, via_cost=via_cost,
        layers=layers, max_iterations=max_iterations,
        max_probe_iterations=max_probe_iterations, heuristic_weight=heuristic_weight,
        turn_cost=turn_cost, direction_preference_cost=direction_preference_cost,
        bus_enabled=bus_enabled, bus_detection_radius=bus_detection_radius,
        bus_attraction_radius=bus_attraction_radius, bus_attraction_bonus=bus_attraction_bonus,
        bus_min_nets=bus_min_nets,
        guide_corridor_enabled=guide_corridor_enabled, guide_corridor_layer=guide_corridor_layer,
        guide_corridor_spacing=guide_corridor_spacing,
        keepout_enabled=keepout_enabled, keepout_layer=keepout_layer,
        proximity_heuristic_factor=proximity_heuristic_factor,
        bga_exclusion_zones=bga_exclusion_zones,
        stub_proximity_radius=stub_proximity_radius, stub_proximity_cost=stub_proximity_cost,
        via_proximity_cost=via_proximity_cost, bga_proximity_radius=bga_proximity_radius,
        bga_proximity_cost=bga_proximity_cost, track_proximity_distance=track_proximity_distance,
        track_proximity_cost=track_proximity_cost, debug_lines=debug_lines, verbose=verbose,
        max_rip_up_count=max_rip_up_count, ripup_abandon_metric=ripup_abandon_metric,
        ripup_blocker_select=ripup_blocker_select,
        crossing_penalty=crossing_penalty,
        crossing_layer_check=crossing_layer_check, routing_clearance_margin=routing_clearance_margin,
        hole_to_hole_clearance=hole_to_hole_clearance, board_edge_clearance=board_edge_clearance,
        vertical_attraction_radius=vertical_attraction_radius,
        vertical_attraction_cost=vertical_attraction_cost,
        ripped_route_avoidance_radius=ripped_route_avoidance_radius,
        ripped_route_avoidance_cost=ripped_route_avoidance_cost,
        length_match_groups=length_match_groups,
        length_match_tolerance=length_match_tolerance, meander_amplitude=meander_amplitude,
        meander_spacing=meander_spacing,
        time_matching=time_matching, time_match_tolerance=time_match_tolerance,
        debug_memory=debug_memory, layer_costs=layer_costs
    )
    config_kwargs['power_tap_neckdown'] = power_tap_neckdown
    config_kwargs['neckdown_length'] = neckdown_length
    config_kwargs['neckdown_taper_length'] = neckdown_taper_length
    if direction_order is not None:
        config_kwargs['direction_order'] = direction_order
    if layer_widths:
        config_kwargs['layer_widths'] = layer_widths
        config_kwargs['impedance_target'] = impedance
    if coplanar_gap:
        config_kwargs['coplanar_gap'] = coplanar_gap
    if coplanar_net_ids and coplanar_layer_widths:
        config_kwargs['coplanar_net_ids'] = coplanar_net_ids
        config_kwargs['coplanar_layer_widths'] = coplanar_layer_widths
    if net_layer_widths_map:
        config_kwargs['net_layer_widths'] = net_layer_widths_map
    if collect_stats:
        config_kwargs['collect_stats'] = collect_stats
    config = GridRouteConfig(**config_kwargs)

    try:
        config.bus_rip_resistance = float(
            env_knobs.BUS_RIP_RESISTANCE or config.bus_rip_resistance)
    except ValueError:
        pass
    if config.bus_rip_resistance != 1.0:
        print(f"Bus rip resistance: {config.bus_rip_resistance}x "
              f"(bus members deprioritized in the rip ladder)")
    # The SE loop needs the strategy to apply the explicit 'bus' ordering.
    config.ordering_strategy = ordering_strategy
    if config.ripup_blocker_select != 'count':
        print(f"Rip-up blocker-select algorithm: {config.ripup_blocker_select}")

    # Build guide-corridor waypoints once (issue #7). These steer the per-segment
    # A* through a user-drawn polyline; empty when the feature is off / no guide.
    config.corridor_waypoints = build_corridor_waypoints(pcb_data, config)
    if config.corridor_waypoints:
        print(f"Guide corridor: steering routes through {len(config.corridor_waypoints)} "
              f"waypoint(s) from {len(pcb_data.guide_paths)} polyline(s) on {config.guide_corridor_layer}")

    # Report keepout zones (issue #27): tracks are blocked from these polygons.
    if config.keepout_enabled and pcb_data.keepout_zones:
        print(f"Keepout: blocking routes from {len(pcb_data.keepout_zones)} "
              f"polygon(s) on {config.keepout_layer}")

    # Per-net netclass clearances (#326 B5): carried on the config so the
    # per-net obstacle cache and the same-run copper stamps reserve each net's
    # OWN class clearance (the base map additionally applies the max-flatten
    # below). net_id-keyed, GUI-fed today; harmless when empty.
    if net_clearances:
        config.net_clearances = {nid: c for nid, c in net_clearances.items()
                                 if c and c > 0}

    # #435 companion: per-net netclass track widths (auto-read above when
    # --track-width was omitted). get_net_track_width() routes each net at its own
    # class width; a manual --power-nets-widths override below still wins.
    if net_track_widths:
        config.net_track_widths = dict(net_track_widths)

    # Identify power nets and set up per-net widths
    if power_nets and power_nets_widths:
        if len(power_nets) != len(power_nets_widths):
            raise ValueError(f"--power-nets ({len(power_nets)}) and --power-nets-widths ({len(power_nets_widths)}) must have same length")
        power_net_widths = identify_power_nets(pcb_data, power_nets, power_nets_widths)
        if power_net_widths:
            config.power_net_widths = power_net_widths
            print(f"\nPower net width assignments ({len(power_net_widths)} nets):")
            # Group by width for summary
            width_groups: Dict[float, List[str]] = {}
            for net_id, width in power_net_widths.items():
                net_name = pcb_data.nets[net_id].name if net_id in pcb_data.nets else f"Net {net_id}"
                if width not in width_groups:
                    width_groups[width] = []
                width_groups[width].append(net_name)
            for width, names in sorted(width_groups.items()):
                if len(names) <= 5:
                    print(f"  {width}mm: {', '.join(names)}")
                else:
                    print(f"  {width}mm: {len(names)} nets ({', '.join(names[:3])}...)")

    # Find net IDs and filter already-routed nets
    net_ids = resolve_net_ids(pcb_data, net_names)
    # Flag target pads that sit at/over the board edge before routing, so an
    # unroutable off-board pad reads as a clear warning rather than a silent
    # exhaustive-search failure (issue #195).
    _edge_clear = board_edge_clearance if board_edge_clearance > 0 else clearance
    warn_targets_outside_board(pcb_data, net_ids,
                               edge_margin=_edge_clear + track_width / 2)
    # Every net in this run's --nets filter, by name (not just the routable ones
    # resolve_net_ids keeps). The dead-end sweep uses this so it also cleans
    # inherited stubs on in-filter nets it did not actively route -- single-pad,
    # already-connected, or failed nets -- while still excluding nets the user
    # left out (GND / power planes routed in a later stage). Issue #84.
    _scope_names = set(net_names or [])
    # #369 A16: resolve_net_ids returns (name, id) TUPLES -- the old
    # `or set(net_ids)` fallback filled the scope with tuples that can never
    # equal an int net_id, silently no-op'ing the dead-end sweep and the
    # stale-copper strips whenever the name scope came up empty. Union the
    # resolved ids in directly (as ints): pad-only nets, present in
    # pads_by_net but absent from pcb.nets, match no pcb.nets name and fell
    # out of scope entirely.
    sweep_scope_ids = ({nid for nid, net in pcb_data.nets.items()
                        if net.name in _scope_names}
                       | {nid for _name, nid in net_ids})
    if not net_ids:
        print("No valid nets to route!")
        if return_results:
            return 0, 0, 0.0, _empty_results_data()
        _write_passthrough_output(input_file, output_file)
        return 0, 0, 0.0

    # #515 follow-up (PR #533): --force-reroute is an explicit request to
    # REPLACE the selected nets' existing routes. Strip their copper BEFORE the
    # already-connected filter, so they re-enter the route set and replan from
    # scratch. The originals were snapshotted into _orig_seg_by_net/_orig_via_
    # by_net above, so on success the #220/#284 stale strip drops them from the
    # written file; a net whose replan lands NO new copper gets them restored
    # (identity-preserved) before the freeze -- see the custody block ahead of
    # the #209 gate. Protection follows the rip-up rules exactly (#521):
    # matched groups / diff pairs are skipped unless named EXACTLY (no glob) in
    # --nets, KiCad-locked copper is never stripped, and plane (zone-owning)
    # nets belong to route_planes.
    force_ripped: Dict[int, Tuple[list, list]] = {}
    if force_reroute and not net_names:
        print("WARNING: --force-reroute requires an explicit --nets scope "
              "(it rips every selected net); ignoring it for this run.")
    elif force_reroute:
        _fr_zone_nids = {z.net_id for z in pcb_data.zones}
        _fr_copper_nids = ({s.net_id for s in pcb_data.segments}
                           | {v.net_id for v in pcb_data.vias})
        _fr_cand, _fr_skipped_plane = [], []
        for _name, _nid in net_ids:
            if _nid not in _fr_copper_nids:
                continue  # no existing copper -- routes normally anyway
            if _nid in _fr_zone_nids:
                _fr_skipped_plane.append(_name)
                continue
            _fr_cand.append((_name, _nid))
        if _fr_skipped_plane:
            print(f"--force-reroute: skipping {len(_fr_skipped_plane)} plane "
                  f"(zone-owning) net(s) -- planes belong to route_planes: "
                  f"{', '.join(_fr_skipped_plane[:6])}"
                  f"{', ...' if len(_fr_skipped_plane) > 6 else ''}")
        if _fr_cand:
            from protected_nets import protection_map, filter_rippable_names
            _fr_prot = protection_map(pcb_data, input_file)
            _fr_keep = set(_n for _n, _ in _fr_cand)
            if _fr_prot:
                _fr_keep = set(filter_rippable_names(
                    [_n for _n, _ in _fr_cand], _fr_prot,
                    override_patterns=list(net_name_patterns
                                           if net_name_patterns is not None
                                           else net_names),
                    context="--force-reroute"))
            for _name, _nid in _fr_cand:
                if _name not in _fr_keep:
                    continue
                force_ripped[_nid] = (
                    [s for s in pcb_data.segments if s.net_id == _nid],
                    [v for v in pcb_data.vias if v.net_id == _nid])
        if force_ripped:
            pcb_data.segments = [s for s in pcb_data.segments
                                 if s.net_id not in force_ripped]
            pcb_data.vias = [v for v in pcb_data.vias
                             if v.net_id not in force_ripped]
            sweep_scope_ids |= set(force_ripped)
            print(f"--force-reroute: stripped "
                  f"{sum(len(s) for s, _ in force_ripped.values())} segment(s) / "
                  f"{sum(len(v) for _, v in force_ripped.values())} via(s) from "
                  f"{len(force_ripped)} net(s) for a from-scratch re-route")

    net_ids, _already_routed = filter_already_routed(pcb_data, net_ids, config)
    # #515: --rip-existing-nets only rips copper that BLOCKS a net being
    # routed; a net dropped here as already-connected never routes, so naming
    # it in both --nets and --rip-existing-nets is a no-op. Warn instead of
    # staying silent.
    if rip_existing_nets and net_names:
        from net_queries import matches_net_filter as _mnf_ripwarn
        _rip_noop = [n for n, _reason in _already_routed
                     if not _reason.startswith('Only')
                     and _mnf_ripwarn(n, rip_existing_nets)]
        if _rip_noop:
            print(f"WARNING: {len(_rip_noop)} net(s) named in both --nets and "
                  f"--rip-existing-nets are already fully connected and will "
                  f"NOT be re-routed ({', '.join(_rip_noop[:6])}"
                  f"{', ...' if len(_rip_noop) > 6 else ''}). "
                  f"--rip-existing-nets only rips nets that block another "
                  f"route; pass --force-reroute to rip and re-route them "
                  f"from scratch (#515).")
    if not net_ids:
        print("All nets are already fully connected - nothing to route!")
        if return_results:
            return 0, 0, 0.0, _empty_results_data()
        _write_passthrough_output(input_file, output_file)
        return 0, 0, 0.0

    # Track all segment layer modifications for file output
    all_segment_modifications = []
    all_swap_segments = []  # new copper from swap via-reuse connectors (#340)
    # Track all vias added during stub layer swapping
    all_swap_vias = []
    # Track total number of layer swaps applied
    total_layer_swaps = 0

    # Apply target swaps for single-ended swappable-nets
    single_ended_target_swaps: Dict[str, str] = {}
    single_ended_target_swap_info: List[Dict] = []
    boundary_debug_labels: List[Dict] = []  # Debug labels for boundary positions
    if swappable_net_patterns:
        from target_swap import apply_single_ended_target_swaps, summarize_target_swaps

        # Find matching single-ended nets
        swappable_se_nets = find_single_ended_nets(
            pcb_data,
            swappable_net_patterns,
            exclude_net_ids=set()
        )

        if len(swappable_se_nets) >= 2:
            print(f"\nAnalyzing target swaps for {len(swappable_se_nets)} single-ended net(s)...")
            single_ended_target_swaps, single_ended_target_swap_info = apply_single_ended_target_swaps(
                pcb_data, swappable_se_nets, config,
                lambda net_id: get_net_endpoints(pcb_data, net_id, config),
                use_boundary_ordering=mps_unroll
            )

    # Single-ended layer swap optimization (before MPS ordering)
    all_stubs_by_layer = {}
    stub_endpoints_by_layer = {}
    if enable_layer_switch and net_ids:
        total_layer_swaps += apply_single_ended_layer_swaps(
            pcb_data, config, net_ids,
            can_swap_to_top_layer, all_segment_modifications, all_swap_vias,
            verbose=verbose, all_swap_segments=all_swap_segments
        )
        # NOTE: apply_stub_layer_switch already appends each swap via to
        # pcb_data.vias itself -- re-appending all_swap_vias here put every
        # pad swap via on the board TWICE (double obstacle stamp, and the
        # board carried one more via than the written file; found by the
        # FILE_LEDGER audit on ottercast AP_WAKE_BT et al).

    # Skip (and loudly list) nets with <2 pads -- unroutable, and attempting
    # them wastes the router/ordering. Do this BEFORE ordering so MPS never
    # sees them.
    net_ids = filter_routable_nets(pcb_data, net_ids)

    # Apply net ordering strategy
    if ordering_strategy in ("mps", "bus"):
        # 'bus' = mps base order; the SE loop then moves bus groups to the
        # front (members middle-out). Explicit form of what --bus implied.
        net_ids, mps_layer_swaps = order_nets_mps(
            pcb_data=pcb_data,
            net_ids=net_ids,
            diff_pairs={},
            mps_unroll=mps_unroll,
            bga_exclusion_zones=bga_exclusion_zones,
            mps_reverse_rounds=mps_reverse_rounds,
            crossing_layer_check=crossing_layer_check,
            mps_segment_intersection=mps_segment_intersection,
            mps_layer_swap=mps_layer_swap,
            enable_layer_switch=enable_layer_switch,
            config=config,
            can_swap_to_top_layer=can_swap_to_top_layer,
            all_segment_modifications=all_segment_modifications,
            all_swap_vias=all_swap_vias,
            all_stubs_by_layer=all_stubs_by_layer,
            stub_endpoints_by_layer=stub_endpoints_by_layer,
            verbose=verbose
        )
        total_layer_swaps += mps_layer_swaps

    elif ordering_strategy == "inside_out" and bga_exclusion_zones:
        net_ids = order_nets_inside_out(pcb_data, net_ids, bga_exclusion_zones)

    elif ordering_strategy == "original":
        print("\nUsing original net order (no sorting)")

    # #472 direct-first ordering (KICAD_DIRECT_FIRST=0 disables): nets with a
    # BARE BGA ball (>=2 pads, no attached copper -- the fanout-deferred
    # direct-route class) move to the FRONT of the order, keeping relative
    # order within each partition. Their short natural surface lanes get
    # claimed before the long nets and bus corridors contend for the same
    # space (the human's sequence: nearby connections first). Inert on
    # boards with no bare balls -- stubs/vias at every ball leave the order
    # untouched. Board-state-driven and engine-level (GUI parity).
    if net_ids and env_knobs.DIRECT_FIRST:
        _bga_refs = set()
        from kicad_parser import find_components_by_type
        _sel_set = {nid for _nm, nid in net_ids}
        _pts_by = {}
        for _s in pcb_data.segments:
            if _s.net_id in _sel_set:
                _pts_by.setdefault(_s.net_id, []).append((_s.start_x, _s.start_y))
                _pts_by[_s.net_id].append((_s.end_x, _s.end_y))
        for _v in pcb_data.vias:
            if _v.net_id in _sel_set:
                _pts_by.setdefault(_v.net_id, []).append((_v.x, _v.y))
        _direct_ids = set()
        for _fp in find_components_by_type(pcb_data, 'BGA'):
            for _p in _fp.pads:
                if (_p.net_id in _sel_set and not _p.drill
                        and len(pcb_data.pads_by_net.get(_p.net_id, [])) >= 2
                        and not any(abs(_x - _p.global_x) < 0.05
                                    and abs(_y - _p.global_y) < 0.05
                                    for (_x, _y) in _pts_by.get(_p.net_id, ()))):
                    _direct_ids.add(_p.net_id)
        # BIG nets are NOT promoted. Direct-first exists to let short natural
        # surface lanes get claimed early, but a big power rail has the most BGA
        # balls, so the rule front-loads exactly the most expensive nets on the
        # board -- and everything routed afterwards then rips them. Measured on
        # muzy_zynq2: +1V8/+3V3/+1V0 were routed #1/#2/#3 of 136 and ripped
        # 26/47/12 times each afterwards, with +3V3 alone accounting for 17115 of
        # the board's 42447 torn segments (40% of ALL destroyed copper). +5V, the
        # one big rail that landed late (#131), was ripped only 7 times.
        # Threshold is a MULTIPLE OF THE BOARD'S MEDIAN net cost, not an absolute
        # value: span is in mm, so an absolute cut-off does not port between a
        # 20mm module and a 200mm backplane. KICAD_BIG_NET_FACTOR=0 disables.
        _big_ids = set()
        if _direct_ids:
            from net_cost import net_cost, big_net_threshold
            _th = big_net_threshold(pcb_data, [nid for _nm, nid in net_ids])
            if _th > 0:
                _big_ids = {nid for nid in _direct_ids
                            if net_cost(pcb_data, nid) > _th}
                _direct_ids = _direct_ids - _big_ids
        if _direct_ids:
            _front = [t for t in net_ids if t[1] in _direct_ids]
            _rest = [t for t in net_ids if t[1] not in _direct_ids]
            net_ids = _front + _rest
            print(f"Direct-first ordering (#472): {len(_front)} bare-ball "
                  f"net(s) moved to the front: "
                  f"{', '.join(nm for nm, _ in _front[:8])}"
                  f"{', ...' if len(_front) > 8 else ''}")
        if _big_ids:
            _names = [nm for nm, nid in net_ids if nid in _big_ids]
            print(f"  ({len(_big_ids)} BIG net(s) held OUT of direct-first to cut "
                  f"rip churn: {', '.join(_names[:6])}"
                  f"{', ...' if len(_names) > 6 else ''})")

    # All nets are single-ended in this tool
    single_ended_nets = net_ids
    total_routes = len(single_ended_nets)

    # Generate stub position labels for single-ended nets (when debug_lines enabled)
    if debug_lines and single_ended_nets:
        from target_swap import generate_single_ended_debug_labels
        stub_labels = generate_single_ended_debug_labels(
            pcb_data, single_ended_nets,
            lambda net_id: get_net_endpoints(pcb_data, net_id, config),
            use_mps_ordering=mps_unroll
        )
        if stub_labels:
            print(f"Generated {len(stub_labels)} stub position labels for single-ended nets")
            boundary_debug_labels.extend(stub_labels)

    results = []
    pad_swaps = []  # List of (pad1, pad2) tuples for nets that need swapping
    successful = 0
    failed = 0
    total_time = 0
    total_iterations = 0
    # Note: all_swap_vias is initialized at line 476 and populated during layer swaps

    # Skip routing if requested - just write output with swaps and debug info
    if skip_routing:
        print(f"\n--skip-routing: Skipping actual routing of {total_routes} items")
        print("Writing output file with swaps and debug labels only...")
        # Clear the lists so routing loops don't execute
        single_ended_nets = []
    else:
        print(f"\nRouting {total_routes} single-ended net(s)...")
        print("=" * 60)

    # Build base obstacle map once (excludes all nets we're routing)
    all_net_ids_to_route = [nid for _, nid in net_ids]

    # Issue #103: pre-existing routed nets matching --rip-existing-nets become
    # ELIGIBLE for rip-up when blocking analysis names them. Their copper is
    # excluded from the static base map and registered like in-process routed
    # nets (per-net obstacles + routed_results), so the normal rip/re-route/
    # restore machinery applies. Without this, tracks committed by a previous
    # run are unrippable and every rechained retry dies with
    # 'no rippable blockers found'.
    existing_rippable: List[int] = []
    if rip_existing_nets:
        from net_queries import matches_net_filter
        to_route_set = set(all_net_ids_to_route)
        zone_net_ids = {z.net_id for z in pcb_data.zones}
        seg_net_ids = {s.net_id for s in pcb_data.segments}
        for nid in sorted(seg_net_ids):
            if nid == 0 or nid in to_route_set:
                continue
            if nid in zone_net_ids:
                continue  # plane nets belong to route_planes, not rip-up
            net = pcb_data.nets.get(nid)
            if not net or not net.name:
                continue
            if matches_net_filter(net.name, rip_existing_nets):
                existing_rippable.append(nid)
        # #521: protected nets (length-matched groups, routed diff pairs --
        # recorded in the sibling .kicad_pro by the step that made them) are
        # excluded from COLLATERAL rips. Naming a net exactly (no glob) in
        # --rip-existing-nets or --nets is the deliberate override. Nets with
        # KiCad-LOCKED copper are excluded unconditionally (no override).
        if existing_rippable:
            from protected_nets import protection_map, filter_rippable_names
            _prot = protection_map(pcb_data, input_file)
            if _prot:
                # Override source: the RAW --nets patterns when main() passed
                # them -- expansion turns a glob into exact names, which made
                # any glob-selected protected net "exactly named" here (#521's
                # override is deliberately no-glob; found by the force-reroute
                # test). rip_existing_nets is never expanded, so it stays as-is.
                _keep = set(filter_rippable_names(
                    [pcb_data.nets[n].name for n in existing_rippable], _prot,
                    override_patterns=list(rip_existing_nets)
                    + list(net_name_patterns if net_name_patterns is not None
                           else (net_names or [])),
                    context="--rip-existing-nets"))
                existing_rippable = [n for n in existing_rippable
                                     if pcb_data.nets[n].name in _keep]
        if existing_rippable:
            names = [pcb_data.nets[n].name for n in existing_rippable[:6]]
            print(f"{len(existing_rippable)} pre-existing net(s) eligible for rip-up: "
                  f"{', '.join(names)}{'...' if len(existing_rippable) > 6 else ''}")
            # A ripped-existing net's copper is this run's responsibility from
            # here on: include it in the cleanup/strip scope so the #220 stale
            # input-copper strip (and dead-end/cycle sweeps) cover it. Without
            # this a rerouted +3.3V shipped BOTH its original copper (crossing
            # nets routed through the vacated corridor) and its reroute (#300
            # follow-up, rp2350_dev GPIO4).
            sweep_scope_ids |= set(existing_rippable)
            # #513 item 5: a ripped-existing net re-routes at THIS invocation's
            # width resolution, silently dropping e.g. a 1.5mm power width to
            # the 0.25 default when the retry omitted --power-nets (nascom VCC;
            # check_connected doesn't check width, so nothing caught it).
            # Preserve each rippable net's routed width: inject its
            # dominant-by-length input width into the per-net width map when it
            # is wider than what this run would use. An explicit --power-nets
            # entry for the net still wins (priority 1 in get_net_track_width).
            from routing_common import dominant_net_widths
            _in_widths = dominant_net_widths(pcb_data.segments)
            for _rid in existing_rippable:
                _w_in = _in_widths.get(_rid, 0.0)
                if _w_in <= 0:
                    continue
                if _rid in (getattr(config, 'power_net_widths', None) or {}):
                    continue  # explicit override this run wins
                _w_now = config.get_net_track_width(_rid, config.layers[0])
                if _w_in > _w_now + 1e-6:
                    if config.net_track_widths is None:
                        config.net_track_widths = {}
                    config.net_track_widths[_rid] = _w_in
                    _rn = pcb_data.nets[_rid].name if _rid in pcb_data.nets else _rid
                    print(f"  Preserving routed width {_w_in}mm of rippable "
                          f"existing net {_rn} (this run would re-route it at "
                          f"{_w_now}mm; pass --power-nets/--power-nets-widths "
                          f"to override)")
    # #513 item 5, --force-reroute edition: a force-ripped net replans at THIS
    # invocation's width resolution; preserve its routed width the same way.
    # Its input segments are already stripped from pcb_data, so source the
    # dominant width from the force_ripped stash.
    if force_ripped:
        from routing_common import dominant_net_widths as _dnw_fr
        _fr_widths = _dnw_fr([s for _segs, _ in force_ripped.values()
                              for s in _segs])
        for _rid, _w_in in _fr_widths.items():
            if _w_in <= 0:
                continue
            if _rid in (getattr(config, 'power_net_widths', None) or {}):
                continue  # explicit override this run wins
            _w_now = config.get_net_track_width(_rid, config.layers[0])
            if _w_in > _w_now + 1e-6:
                if config.net_track_widths is None:
                    config.net_track_widths = {}
                config.net_track_widths[_rid] = _w_in
                _rn = pcb_data.nets[_rid].name if _rid in pcb_data.nets else _rid
                print(f"  Preserving routed width {_w_in}mm of force-rerouted "
                      f"net {_rn} (this run would re-route it at {_w_now}mm; "
                      f"pass --power-nets/--power-nets-widths to override)")
    if progress_callback:
        progress_callback(0, 0, "Building base obstacle map...")
    print("Building base obstacle map...")
    base_start = time.time()

    # Tap relocation (#424): zone-backed plane nets become base-map-excluded
    # and per-net cached so single-tap surgery is exact whole-net cache
    # recompute (ref-count-balanced by construction); deliberately NOT in
    # routed_results, so the whole-net rip ladder can never name them.
    relocatable_plane_ids = []
    from tap_relocation import tap_relocation_enabled as _tap_reloc_on
    if _tap_reloc_on():
        _zone_nids = {z.net_id for z in pcb_data.zones if z.net_id > 0}
        relocatable_plane_ids = sorted(
            n for n in _zone_nids
            if n not in all_net_ids_to_route and n not in existing_rippable
            and n in pcb_data.nets)
        if relocatable_plane_ids:
            print(f"Tap relocation armed: {len(relocatable_plane_ids)} plane "
                  f"net(s) cached for single-tap surgery")
    base_map_exclusions = all_net_ids_to_route + existing_rippable + relocatable_plane_ids
    # Cross-class clearance: install the per-net class map + routing-side floor on
    # config so BOTH the base map and every incremental in-run obstacle stamper
    # price foreign copper at KiCad's pairwise max(classA, classB). The floor is
    # computed over the ROUTED nets (== base map's nets_to_route) so the base map
    # and the incremental stampers agree. Inert when net_clearances is empty.
    config.set_net_clearances(net_clearances, base_map_exclusions)
    # #498: per-layer .kicad_dru clearance rules, installed engine-side so the
    # GUI inherits them with no wiring (see kicad_dru.install_layer_clearances).
    from kicad_dru import install_layer_clearances
    install_layer_clearances(config, layer_clearances, input_file, pcb_data)
    # #568: arming is run-scoped and the flag is module-global, so reset it
    # per call -- otherwise one unfrozen-base run would disarm rung-1 legality
    # for every later run in the same process (the GUI's whole session).
    try:
        from obstacle_cache import set_rung_unsafe as _rearm
        _rearm(bool(getattr(pcb_data, '_via_rung_unsafe', False)))
    except Exception:
        pass
    # Carry the RESOLVED map into the end-of-run reconciliation kwargs, exactly
    # like board_edge_clearance above: the reconciliation self-invocation reads
    # the OUTPUT file, whose sibling .kicad_dru does not exist yet (main()'s
    # fix_project_for_output copies it after batch_route returns), so its own
    # auto-read would find NOTHING and the sub-run would route blind to the
    # rules (caught by test_dru_layer_clearance_e2e: a reconciliation +3V3 via
    # 0.25mm inside the B.Cu rule against GND).
    _reconcile_kwargs['layer_clearances'] = dict(config.layer_clearances)
    # #568: rung-1 via legality is only sound when base copper blocks EVERY
    # rung, i.e. when the base is frozen into the static keep-out bitmap. The
    # one remaining path that builds an UNFROZEN base is KICAD_NO_STATIC_BASE
    # (the vis branch that used to share this hazard is gone with #569), so
    # disarm the dual stamping there -- pcb_data for the cache path,
    # set_rung_unsafe for the RAW map mirrors, which see only
    # (obstacles, config) and cannot read the flag.
    if env_knobs.NO_STATIC_BASE:
        pcb_data._via_rung_unsafe = True
        from obstacle_cache import set_rung_unsafe as _set_rung_unsafe
        _set_rung_unsafe(True)
    base_obstacles = build_base_obstacle_map(
        pcb_data, config, base_map_exclusions,
        net_clearances=net_clearances,
        # #422: base holds only permanent copper/geometry (target + rippable
        # nets live in the per-net caches on a CLONE); stamp it straight into
        # the static keep-out bitmap so the working clone carries it as bits.
        static_base=not env_knobs.NO_STATIC_BASE,
        # #556: sub-phase progress so the GUI bar moves during the build
        progress_callback=progress_callback)

    base_elapsed = time.time() - base_start
    print(f"Base obstacle map built in {base_elapsed:.2f}s")
    if debug_memory:
        mem_after_base = get_process_memory_mb()
        print(format_memory_stats("After base obstacle map", mem_after_base, mem_after_base - mem_start))

    # Save original (pre-routing) segment signatures to preserve stubs during sync
    # We use object identity since segments are mutable and could be duplicated
    # Keep the objects alive alongside the id set: recycled ids of GC'd
    # originals otherwise alias NEW segments during sync (see route_diff, #195).
    _original_segments_keepalive = list(pcb_data.segments)
    original_segment_ids = set(id(s) for s in _original_segments_keepalive)

    # Get unrouted nets for stub proximity costs
    # Use sorted list for deterministic iteration order
    if minimal_obstacle_cache:
        # Only consider nets we're routing (faster for re-routing a few nets)
        all_unrouted_net_ids = sorted(all_net_ids_to_route)
        print(f"Using minimal obstacle cache for {len(all_unrouted_net_ids)} nets being routed")
    else:
        # All unrouted nets in the PCB (full analysis for stub proximity)
        all_unrouted_net_ids = sorted(set(get_all_unrouted_net_ids(pcb_data)))
        print(f"Found {len(all_unrouted_net_ids)} unrouted nets in PCB for stub proximity")

    # Get exclusion zone lines for User.5 if debug_lines is enabled
    exclusion_zone_lines = []
    if debug_lines:
        all_unrouted_stubs = get_stub_endpoints(pcb_data, list(all_unrouted_net_ids))
        all_chip_pads = get_chip_pad_positions(pcb_data, list(all_unrouted_net_ids))
        all_proximity_points = all_unrouted_stubs + all_chip_pads
        exclusion_zone_lines = draw_exclusion_zones_debug(config, all_proximity_points)
        print(f"Will draw {len(config.bga_exclusion_zones)} BGA zones and {len(all_proximity_points)} stub/pad proximity circles on User.5")

    # Pre-compute net obstacles for caching (speeds up per-route setup)
    if progress_callback:
        progress_callback(0, 0, "Pre-computing net obstacle cache...")
    print("Pre-computing net obstacle cache...")
    cache_start = time.time()
    net_obstacles_cache = precompute_all_net_obstacles(
        pcb_data, list(all_unrouted_net_ids), config,
        extra_clearance=0.0, diagonal_margin=defaults.DIAGONAL_MARGIN
    )
    # Rippable pre-existing nets need cache entries too: the working obstacle
    # map is base + cache, and their copper was excluded from base (issue #103).
    if existing_rippable or relocatable_plane_ids:
        from obstacle_cache import precompute_net_obstacles
        for nid in existing_rippable + relocatable_plane_ids:
            net_obstacles_cache[nid] = precompute_net_obstacles(
                pcb_data, nid, config, extra_clearance=0.0, diagonal_margin=defaults.DIAGONAL_MARGIN)

    cache_time = time.time() - cache_start
    print(f"Net obstacle cache built in {cache_time:.2f}s ({len(net_obstacles_cache)} nets)")
    if debug_memory:
        mem_after_cache = get_process_memory_mb()
        cache_size = estimate_net_obstacles_cache_mb(net_obstacles_cache)
        print(format_memory_stats("After net obstacles cache", mem_after_cache, mem_after_cache - mem_start))
        print(f"[MEMORY]   Cache estimated size: {cache_size:.1f} MB for {len(net_obstacles_cache)} nets")

    # Build working obstacle map (base + all nets) for incremental updates
    # Uses reference counting in Rust to correctly handle cells blocked by multiple nets
    if progress_callback:
        progress_callback(0, 0, "Building working obstacle map...")
    working_obstacles = build_working_obstacle_map(base_obstacles, net_obstacles_cache)
    # Shrink internal allocations to reduce memory footprint
    working_obstacles.shrink_to_fit()
    if debug_memory:
        mem_after_working = get_process_memory_mb()
        print(format_memory_stats("After working obstacle map", mem_after_working, mem_after_working - mem_start))

    # Route trace (#482, KICAD_ROUTE_TRACE=1): attach a recorder to pcb_data so
    # the two copper choke points (add_route_to_pcb_data /
    # remove_route_from_pcb_data) log every segment/via added, ripped, and
    # restored, in order, for animating the routing process. Default-off.
    # Gate on a real output path: an internal reconnect batch_route (e.g.
    # repair_planes) calls this with output_file="" and must not
    # start/dump a trace of its own (the plane front-end keeps a local one).
    if output_file:
        from route_trace import attach_trace as _attach_route_trace
        _attach_route_trace(pcb_data)

    # Create routing state object to hold all shared state
    state = create_routing_state(
        pcb_data=pcb_data,
        config=config,
        all_net_ids_to_route=all_net_ids_to_route,
        base_obstacles=base_obstacles,
        diff_pair_base_obstacles=None,  # Not used for single-ended
        diff_pair_extra_clearance=0.0,
        gnd_net_id=None,  # Not used for single-ended
        all_unrouted_net_ids=all_unrouted_net_ids,
        total_routes=total_routes,
        enable_layer_switch=enable_layer_switch,
        debug_lines=debug_lines,
        target_swaps={},  # Diff pair swaps not used
        target_swap_info=[],
        single_ended_target_swaps=single_ended_target_swaps,
        single_ended_target_swap_info=single_ended_target_swap_info,
        all_segment_modifications=all_segment_modifications,
        all_swap_vias=all_swap_vias,
        total_layer_swaps=total_layer_swaps,
        net_obstacles_cache=net_obstacles_cache,
        working_obstacles=working_obstacles,
    )

    # Create local aliases for frequently-used state fields
    routed_net_ids = state.routed_net_ids
    routed_net_paths = state.routed_net_paths
    routed_results = state.routed_results
    track_proximity_cache = state.track_proximity_cache
    layer_map = state.layer_map

    # #540 item 2: price the outer pass's vacated corridors in every search
    # of this batch. The entries ride the existing ripped-route avoidance
    # dicts (merge_ripped_route_costs), so per-net maps, rip-up retries and
    # reroute rounds all see them; the owners never route here, so the
    # routed_net_ids skip never drops them and no net repels itself.
    if external_ripped_ghosts and config.ripped_route_avoidance_cost > 0:
        from obstacle_costs import compute_ripped_route_costs as _crrc540
        _gcells = 0
        for _gnid, _gres in sorted(external_ripped_ghosts.items()):
            _glc, _gvp = _crrc540(_gres, config, layer_map)
            if len(_glc):
                state.ripped_route_layer_costs[_gnid] = _glc
                _gcells += len(_glc)
            if _gvp:
                state.ripped_route_via_positions[_gnid] = _gvp
        print(f"  (#540 ghosts: pricing {len(external_ripped_ghosts)} pending "
              f"casualty corridor(s), {_gcells} cost cells at "
              f"{config.ripped_route_avoidance_cost}mm)")

    # BGA proximity costs live in the track-proximity cache under a reserved
    # key (soft-knobs review B1): stamped into the base map they were wiped
    # by prepare_obstacles_inplace's clear_stub_proximity before EVERY
    # single-ended net, so the knob silently no-op'd in the most common path.
    # The cache is re-merged on every prepare in every path (single-ended,
    # diff pair, Phase 3 via the working-map clone).
    from obstacle_costs import compute_bga_proximity_cost_cells, BGA_PROXIMITY_CACHE_KEY
    _bga_cells = compute_bga_proximity_cost_cells(config, len(config.layers))
    if len(_bga_cells):
        track_proximity_cache[BGA_PROXIMITY_CACHE_KEY] = _bga_cells

    # Congestion-aware soft costs (#424 Phase D): all-layer copper-density
    # field under a second reserved cache key; env-gated (KICAD_CONGESTION_
    # COST=0 default off). Vias in hot cells pay via_proximity_cost x the
    # cell cost via the Rust via branch.
    from congestion_field import register_congestion_field
    register_congestion_field(pcb_data, config, track_proximity_cache)

    # Plane-fragility soft costs (#424 planes-first): near-boundary pour
    # cells (of the EXACT fill when the board file is reachable) cost extra
    # so signals avoid BISECTING a plane at its necks. DEFAULT ON at 2.0
    # mm-equiv (KICAD_PLANE_FRAGILITY_COST=0 reverts); inert on boards
    # with no zones.
    from plane_fragility import register_plane_fragility
    register_plane_fragility(pcb_data, config, track_proximity_cache)

    # Congestion v2 (#424): demand/capacity bins + owner terminals; per-net
    # stamping happens at prepare (routing_context.stamp_congestion2).
    from congestion_field import build_congestion2
    config._congestion2 = build_congestion2(pcb_data, config,
                                            list(all_net_ids_to_route))

    # Register rippable pre-existing nets as already-routed (issue #103):
    # blocking analysis iterates routed_net_paths (cells are recomputed from
    # pcb_data, so an empty path is fine), filter_rippable_blockers requires
    # routed_results membership, and rip_up_net removes exactly the
    # 'new_segments'/'new_vias' listed - the net's own file copper here.
    for nid in existing_rippable:
        state.routed_net_ids.append(nid)
        state.routed_net_paths[nid] = []
        state.routed_results[nid] = {
            'net_id': nid,
            'new_segments': [s for s in pcb_data.segments if s.net_id == nid],
            'new_vias': [v for v in pcb_data.vias if v.net_id == nid],
            'iterations': 0,
            'is_existing_route': True,
        }
    ripup_success_pairs = state.ripup_success_pairs
    rerouted_pairs = state.rerouted_pairs
    remaining_net_ids = state.remaining_net_ids
    results = state.results

    # Counters (kept as locals, not aliased from state)
    route_index = 0

    # Route single-ended nets
    se_successful, se_failed, se_time, se_iterations, route_index, user_quit = route_single_ended_nets(
        state, single_ended_nets,
        route_index_start=route_index,
        cancel_check=cancel_check, progress_callback=progress_callback
    )
    successful += se_successful
    failed += se_failed
    total_time += se_time
    total_iterations += se_iterations

    # Checkpoint abort (KICAD_STOP_AFTER / KICAD_STOP_FILE, set by the SE
    # loop): skip every later routing/repair pass and fall through to the
    # WRITE, so the partial board ships exactly as it stood at the stop.
    _ckpt_stop = getattr(state, 'checkpoint_stop', False)
    if _ckpt_stop:
        print("Checkpoint stop: skipping reroute/length-match/Phase 3/rescue/"
              "cleanup/reconciliation; writing the board as-is")

    # Run reroute loop for nets that were ripped during diff pair or single-ended routing
    rq_successful, rq_failed, rq_time, rq_iterations, route_index = (0, 0, 0.0, 0, route_index) if _ckpt_stop else run_reroute_loop(
        state, route_index_start=route_index,
        cancel_check=cancel_check, progress_callback=progress_callback,
        failed_so_far=failed
    )
    successful += rq_successful
    failed += rq_failed
    total_time += rq_time
    total_iterations += rq_iterations

    # Apply length matching if configured
    if length_match_groups and not _ckpt_stop:
        run_length_matching(routed_results, length_match_groups, config, pcb_data)

    # Sync pcb_data with length-matched segments before Phase 3
    # This ensures tap routes see meanders from other nets as obstacles
    if progress_callback:
        progress_callback(0, 0, "Syncing pcb_data...")
    sync_pcb_data_segments(pcb_data, routed_results, original_segment_ids, state, config)

    # Phase 3: Complete multi-point routing (tap connections)
    # This happens AFTER length matching so tap routes connect to meandered main routes
    num_multipoint_nets = len(state.pending_multipoint_nets) if state.pending_multipoint_nets else 0

    # Create phase 3 progress callback
    def phase3_progress_callback(current, total, net_name):
        if progress_callback:
            progress_callback(current, num_multipoint_nets, f"Multi-point: {net_name}")

    if not _ckpt_stop:
      run_phase3_tap_routing(
        state=state,
        pcb_data=pcb_data,
        config=config,
        base_obstacles=base_obstacles,
        gnd_net_id=None,  # Not used for single-ended
        all_unrouted_net_ids=all_unrouted_net_ids,
        routed_net_ids=routed_net_ids,
        remaining_net_ids=remaining_net_ids,
        routed_net_paths=routed_net_paths,
        routed_results=routed_results,
        diff_pair_by_net_id=state.diff_pair_by_net_id,  # Empty for single-ended
        results=results,
        track_proximity_cache=track_proximity_cache,
        layer_map=layer_map,
        progress_callback=phase3_progress_callback,
        cancel_check=cancel_check,
    )

    # Issue #134: nets whose stale copper would have shorted another net on
    # restore were left ripped instead of re-added (collision-safe restore).
    # Now that the board is stable, give them one clean reroute pass so the fix
    # does not cost completion. Only runs when a refusal actually happened, so
    # boards without the collision are unaffected.
    if state.collision_refused_net_ids:
        recover = []
        for nid in sorted(state.collision_refused_net_ids):
            if nid in routed_results or nid not in pcb_data.nets:
                continue
            if len(pcb_data.pads_by_net.get(nid, [])) < 2:
                continue
            if nid in state.diff_pair_by_net_id:
                pair_name, pair = state.diff_pair_by_net_id[nid]
                if (pair.p_net_id in state.queued_net_ids
                        or pair.n_net_id in state.queued_net_ids):
                    continue
                state.reroute_queue.append(('diff_pair', pair_name, pair))
                state.queued_net_ids.add(pair.p_net_id)
                state.queued_net_ids.add(pair.n_net_id)
                recover.append(pair_name)
            else:
                if nid in state.queued_net_ids:
                    continue
                state.reroute_queue.append(('single', pcb_data.nets[nid].name, nid))
                state.queued_net_ids.add(nid)
                recover.append(pcb_data.nets[nid].name)
        if recover and not _ckpt_stop:
            print(f"Issue #134 recovery: re-routing {len(recover)} net(s) left ripped "
                  f"to avoid a short: {', '.join(recover)}")
            run_reroute_loop(state, route_index_start=route_index,
                             cancel_check=cancel_check,
                             progress_callback=progress_callback)
        # Last resort (parity with the plane tools' piece-level settle,
        # 72ca5f9): a refused net whose clean reroute ALSO failed ships at
        # zero copper -- restore the saved route's non-colliding pieces
        # instead, leaving a small gap for a later pass rather than a
        # destroyed net. Piece-wise _saved_route_collides against the settled
        # board, so nothing restored can short copper routed meanwhile.
        _stash_134 = getattr(pcb_data, '_refused_saved_134', {}) or {}
        for nid in sorted(state.collision_refused_net_ids):
            if nid in routed_results or nid not in _stash_134:
                continue
            saved = _stash_134[nid]
            from rip_up_reroute import _saved_route_collides
            from routing_context import add_route_to_pcb_data
            keep_segs = [sg for sg in (saved.get('new_segments') or [])
                         if not _saved_route_collides(
                             {'new_segments': [sg], 'new_vias': []},
                             pcb_data, [nid], config.clearance)]
            keep_vias = [v for v in (saved.get('new_vias') or [])
                         if not _saved_route_collides(
                             {'new_segments': [], 'new_vias': [v]},
                             pcb_data, [nid], config.clearance)]
            from pcb_modification import drop_orphan_restore_pieces
            drop_orphan_restore_pieces(keep_segs, keep_vias, nid, pcb_data)
            if not keep_segs and not keep_vias:
                continue
            dropped = (len(saved.get('new_segments') or []) - len(keep_segs)
                       + len(saved.get('new_vias') or []) - len(keep_vias))
            pruned = dict(saved)
            pruned['new_segments'] = keep_segs
            pruned['new_vias'] = keep_vias
            pruned['partial_restore_134'] = True
            add_route_to_pcb_data(pcb_data, pruned, debug_lines=config.debug_lines)
            results.append(pruned)
            # #508 finding 8: register the restore as the net's AUTHORITATIVE
            # result, or the #87 superseded-result filter (`_authoritative`,
            # identity-keyed off routed_results) silently drops it from the
            # write-list on EVERY firing -- the copper landed in pcb_data,
            # the log said "restored", and the file shipped none of it (the
            # exact trap net_rescue.py's partial branch documents; evidence
            # runs_set14/rusefi_alphax4/step2b_retry.log). The entry
            # condition guarantees the net has no other result.
            routed_results[nid] = pruned
            nm = pcb_data.nets[nid].name if nid in pcb_data.nets else nid
            print(f"Issue #134 last resort: {nm} reroute failed; restored "
                  f"{len(keep_segs)} segment(s) + {len(keep_vias)} via(s) of its "
                  f"pre-rip route (dropped {dropped} colliding piece(s)); "
                  f"net remains PARTIAL for a later reconnect pass")

    # T5 zero-copper custody: casualties-only final reconciliation (route.py
    # front; parity with batch_route_diff_pairs' 43e6d10). A net RIPPED during
    # this run whose queued reroute never landed used to ship at ZERO copper
    # with no custody -- the #134 recovery above only covers nets whose RESTORE
    # was refused, not nets that were never restored at all (the ulx3s
    # GN8/GP2/GN22 class: pre-existing rippable nets ripped by the rip-up
    # ladder, reroute failed, stale-strip then removed their input copper from
    # the output). Restore-first semantics, collision-aware (never leaves
    # restored copper colliding with newly routed copper); honest 'unrecovered'
    # when nothing safe can be re-instated. Engine-side, so the GUI inherits.
    from diff_pair_custody import run_casualty_reconcile
    casualty_summary = None if _ckpt_stop else run_casualty_reconcile(
        state, progress_callback=progress_callback, cancel_check=cancel_check)

    # Issues #331/#371: last-chance scoped fine-parameter rescue for nets the
    # whole pipeline (main loop, rip-up ladder, reroute loop, Phase 3, #134
    # recovery) still left failed or partially connected. No rip-up and no
    # flags - scoped windows at finer grid/track/clearance only (net_rescue).
    # Runs BEFORE the summary counts so recovered nets grade as routed, and
    # before the cleanup pipeline so rescue copper is swept like all other
    # copper. KICAD_NET_RESCUE=0 disables it for A/B debugging.
    if progress_callback:
        progress_callback(0, 0, "Rescuing failed nets...")
    from net_rescue import rescue_failed_nets
    rescue_summary = None if _ckpt_stop else rescue_failed_nets(
        state, single_ended_nets, net_clearances=net_clearances,
        progress_callback=progress_callback, cancel_check=cancel_check)

    # Final progress update
    if progress_callback:
        progress_callback(total_routes, total_routes, "Routing complete")

    # Build summary data
    import json
    # routed_single / failed_single are derived below, AFTER the authoritative
    # connectivity sweep -- a net with a result in routed_results may still have
    # left pads disconnected (a failed MST edge), and must not be reported routed
    # (issue #189). failed_single stays "no result at all"; the partially-routed
    # disconnected nets are surfaced via failed_multipoint (and mp_deficit).

    # Keep only each net's authoritative result in the write-list. routed_results
    # holds one result per net; rip-reroute paths (restore_net, layer-swap) can
    # leave a superseded duplicate of a net's result still in `results`, and its
    # copper is then written alongside the authoritative one -- stacking coincident
    # same-net vias (#87). Dropping by identity is safe now that the Phase-3 commit
    # reconciles each net's result against its actual copper, so the authoritative
    # result is the complete one (a dropped tap is flagged and re-routed, not lost).
    _authoritative = {id(r) for r in routed_results.values()}
    _stale = [r for r in results if id(r) not in _authoritative]
    if _stale:
        results[:] = [r for r in results if id(r) in _authoritative]
        print(f"Dropped {len(_stale)} superseded rip-reroute result(s) from the write-list")

    # --force-reroute custody: a force-ripped net whose replan landed NO new
    # copper gets its ORIGINAL copper back. Restore re-adds the SAVED objects
    # (identity-preserved), so the freeze snapshot below includes them and the
    # #220/#284 stale strip keeps them -- exactly the rip/restore contract
    # _compute_stale_input_copper documents. Without this, a failed replan
    # would strip a working route from the output in exchange for nothing.
    # Partial replans (some new copper) keep the new copper and report through
    # the normal failed-nets summary instead: restoring originals on top of a
    # partial reroute would stack stale copper the replan deliberately left.
    if force_ripped:
        _fr_new_copper = set()
        for _r in results:
            for _s in _r.get('new_segments') or []:
                _fr_new_copper.add(_s.net_id)
            for _v in _r.get('new_vias') or []:
                _fr_new_copper.add(_v.net_id)
        _fr_restored = []
        for _nid, (_segs, _vias) in force_ripped.items():
            if _nid in _fr_new_copper:
                continue
            pcb_data.segments = list(pcb_data.segments) + _segs
            pcb_data.vias = list(pcb_data.vias) + _vias
            _fr_restored.append(pcb_data.nets[_nid].name
                                if _nid in pcb_data.nets else str(_nid))
        if _fr_restored:
            print(f"--force-reroute: replan produced no copper for "
                  f"{len(_fr_restored)} net(s); ORIGINAL copper restored: "
                  f"{', '.join(_fr_restored[:6])}"
                  f"{', ...' if len(_fr_restored) > 6 else ''}")

    # ---- Issue #209 fix C: catch cleanup passes that disconnect a completed route ----
    # Snapshot each in-scope multi-pad net's connectivity on the WRITE-LIST copper
    # (original + routed new copper -- the same basis the output is written from)
    # BEFORE the post-routing cleanup passes (snap / phantom / cycle-prune /
    # dead-end sweep / neck). If a pass turns a connected net (or pad) disconnected,
    # that is a cleanup bug -- surface it loudly and in the JSON summary rather than
    # ship a silently-broken net (free_dap +3V3 IC2.13).
    from check_connected import check_net_connectivity as _cnc209

    # Gate basis: ON-BOARD originals at gate-setup time (routing is done, so
    # pcb_data already reflects rip-and-not-restored losses). Using the
    # pristine input snapshot here mis-attributed ROUTING losses to cleanup:
    # a ripped-and-lost net's original copper made the PRE check read
    # "connected", the stale strip then truthfully removed the ghost copper
    # from the file, and the POST check blamed the cleanup pass (smartknob
    # +5V: "cleanup DISCONNECTED ... dropped 36 segment(s)" on every wave,
    # while the disconnect was really a routing failure).
    _gate_seg_by_net: Dict[int, list] = {}
    for _s0 in pcb_data.segments:
        if id(_s0) in original_segment_ids:
            _gate_seg_by_net.setdefault(_s0.net_id, []).append(_s0)
    _gate_orig_via_ids = {id(v) for lst in _orig_via_by_net.values() for v in lst}
    _gate_via_by_net: Dict[int, list] = {}
    for _v0 in pcb_data.vias:
        if id(_v0) in _gate_orig_via_ids:
            _gate_via_by_net.setdefault(_v0.net_id, []).append(_v0)
    # Board membership for RESULTS copper at gate-setup time: a result can
    # still reference copper a later net's rip-up removed from the board (the
    # phantom drop reconciles that inside the pipeline, AFTER the pre-check).
    # Counting it inflated the PRE connectivity, so a net routing honestly
    # failed (smartknob +5V, 5 failed multipoint pads) was re-blamed on
    # cleanup when the phantom drop removed the ghost copper from the
    # write-list.
    _gate_board_seg_ids = {id(s) for s in pcb_data.segments}
    _gate_board_via_ids = {id(v) for v in pcb_data.vias}
    # ...but only SETUP-ERA results copper needs board membership: cleanup
    # passes legitimately ADD copper to results afterwards (snap connectors,
    # octolinear/microshift replacements, soft-joint bridges) which is not in
    # the setup snapshot -- filtering those out made every repaired net grade
    # "disconnected" in the post check (false +5V//STRAIN_S- reports).
    _gate_setup_result_seg_ids = {id(s) for r in results
                                  for s in (r.get('new_segments') or [])}
    _gate_setup_result_via_ids = {id(v) for r in results
                                  for v in (r.get('new_vias') or [])}

    def _writelist_copper_209(strip_seg_ids=frozenset(), strip_via_ids=frozenset()):
        # strip_*_ids: original input copper the writer will DELETE from its
        # verbatim copy (cleanup strip lists + the #220/#284 stale strips).
        # The post-cleanup check must exclude it, or a strip that disconnects a
        # net is invisible here -- the pre-snapshot passes empty sets (nothing
        # stripped yet), so pre and post grade the same write model.
        _s = {nid: [s for s in lst if id(s) not in strip_seg_ids]
              for nid, lst in _gate_seg_by_net.items()}
        _v = {nid: [v for v in lst if id(v) not in strip_via_ids]
              for nid, lst in _gate_via_by_net.items()}
        for _r in results:
            for _seg in _r.get('new_segments') or []:
                if (id(_seg) not in _gate_setup_result_seg_ids
                        or id(_seg) in _gate_board_seg_ids):
                    _s.setdefault(_seg.net_id, []).append(_seg)
            for _via in _r.get('new_vias') or []:
                if (id(_via) not in _gate_setup_result_via_ids
                        or id(_via) in _gate_board_via_ids):
                    _v.setdefault(_via.net_id, []).append(_via)
        # Stub layer-swap vias are written to the output but live only in
        # all_swap_vias (not in any result's new_vias, and the per-net snapshot
        # predates them). Without them every layer-swapped stub looks severed
        # from its pad and the net reports a phantom disconnection (issue #292:
        # nebula_watch's 14 layer swaps == its 14 "failed" multi-point nets).
        for _via in all_swap_vias:
            _v.setdefault(_via.net_id, []).append(_via)
        # Swap reuse-connector SEGMENTS ride all_swap_segments the same way
        # (#508 finding 13): written to the output, in no result's
        # new_segments -- omitting them severs the swapped stub from its pad
        # in this gate's model and manufactures phantom disconnects.
        for _seg in all_swap_segments:
            _s.setdefault(_seg.net_id, []).append(_seg)
        return _s, _v

    def _seg_sig_209(s):
        a, b = (round(s.start_x, 3), round(s.start_y, 3)), (round(s.end_x, 3), round(s.end_y, 3))
        return (min(a, b), max(a, b), s.layer)

    _zbn_209 = {}
    for _z in getattr(pcb_data, 'zones', []) or []:
        _zbn_209.setdefault(_z.net_id, []).append(_z)
    _pre_s_209, _pre_v_209 = _writelist_copper_209()
    _pre_conn_209 = {}
    for _nid in sweep_scope_ids:
        _pads209 = pcb_data.pads_by_net.get(_nid, [])
        if len(_pads209) < 2:
            continue
        _r209 = _cnc209(_nid, _pre_s_209.get(_nid, []), _pre_v_209.get(_nid, []),
                        _pads209, _zbn_209.get(_nid, []), tolerance=0.02)
        _pre_conn_209[_nid] = (
            bool(_r209.get('connected')),
            len(_r209.get('disconnected_pads') or []),
            {_seg_sig_209(s) for s in _pre_s_209.get(_nid, [])},
            {(round(v.x, 3), round(v.y, 3)) for v in _pre_v_209.get(_nid, [])},
        )

    # ---- Post-route cleanup: the ONE shared pipeline (#319 restructure) ----
    # All passes run inside run_post_route_cleanup in their canonical order
    # (see cleanup_pipeline.py), under the uniform contract that pcb_data is
    # mutated in lockstep with the write-list -- pcb_data IS the board that
    # will be written at every point after a pass.
    #
    # The freeze hook fires after the phantom drop, when the board holds
    # exactly what ROUTING committed (rips applied, write-list reconciled) and
    # no cleanup pass has trimmed it yet. The #220/#284 stale-input strip below
    # must reference THAT board -- the rip signal -- not one a cleanup pass has
    # since trimmed: a cleanup pass's own removals are already tracked in the
    # pipeline's strip list, and feeding them into the stale strip too made it
    # over-remove LOAD-BEARING input copper (the glasgow_revC B1 regression:
    # 187 vs 28 stale strips, 6 nets dropped). copy.copy each object, not just
    # the list: a list() alone is immune to list REBINDS but still shares
    # objects, so an in-place field move (nudge_grazing_vias shifts a this-run
    # via's x,y in place) would leak into the snapshot's signatures.
    _committed_segments: list = []
    _committed_vias: list = []
    _committed_seg_ids: set = set()
    _committed_via_ids: set = set()

    def _freeze_committed():
        _committed_segments.extend(copy.copy(s) for s in pcb_data.segments)
        _committed_vias.extend(copy.copy(v) for v in pcb_data.vias)
        # Identity of the LIVE objects at freeze time: the stale strip's
        # "original still on the committed board" test is by id(), not
        # signature -- a routed twin re-created on the byte-identical span
        # must not shield a ripped original from the strip (twin-shielding
        # leak, see _compute_stale_input_copper).
        _committed_seg_ids.update(id(s) for s in pcb_data.segments)
        _committed_via_ids.update(id(v) for v in pcb_data.vias)

    # Checkpoint stop skips the cleanup pipeline, but the FREEZE must still
    # happen: the #220/#284 stale-input strip below grades any input copper
    # absent from the committed snapshot as stale -- with an empty snapshot
    # it stripped EVERY routed net's input copper from the written file
    # (checkpoint boards shipped bus nets consisting only of their new tap
    # segments; 31-segment trunks vanished). The board at this point IS what
    # routing committed, which is exactly the freeze semantics.
    # KICAD_STOP_CLEANUP=1: run the cleanup pipeline ON the checkpoint
    # snapshot (dead-end sweep, grazes, etc.) before writing -- shows what
    # cleanup would retire (e.g. a stale escape stub the island-launch
    # route no longer uses). The freeze then fires inside the pipeline.
    _ckpt_cleanup = _ckpt_stop and env_knobs.STOP_CLEANUP
    if _ckpt_stop and not _ckpt_cleanup:
        _freeze_committed()
    # #473: nets still carrying unfinished pads keep ALL their copper
    # through the dead-end sweep -- their spurs are the landing sites the
    # next chain step / rescue needs, and sweeping them each step eroded
    # failed nets across chains.
    _protect_unfinished = {nid for nid, _r in routed_results.items()
                           if _r.get('failed_pads_info')}
    _protect_unfinished |= {nid for _nm, nid in single_ended_nets
                            if nid not in routed_results}
    _cleanup = None if (_ckpt_stop and not _ckpt_cleanup) else run_post_route_cleanup(
        results, pcb_data, sweep_scope_ids, config,
        protect_net_ids=_protect_unfinished,
        freeze_hook=_freeze_committed,
        # Lets the phantom step also remove ORPHAN routed copper from pcb_data
        # (rip/reroute slivers no surviving result references), so board ==
        # write model. Original input copper is identified by object identity
        # (the keepalive prevents id() recycling, see _original_segments_keepalive).
        # Stub layer-swap vias live in all_swap_vias (not in any result) but ARE
        # written to the output -- protect them like originals or the orphan
        # sweep would strip them from the board while the file keeps them.
        original_segment_ids=original_segment_ids,
        original_via_ids=({id(v) for lst in _orig_via_by_net.values() for v in lst}
                          | {id(v) for v in all_swap_vias}),
        keep_input_copper=keep_input_copper,
        progress_callback=progress_callback)
    dead_end_input_segments = _cleanup.input_strip_segments if _cleanup is not None else []

    # Issue #220: the output writer copies the INPUT FILE verbatim, then adds the
    # write-list results and strips `segments_to_remove`. So an in-scope net's
    # original input copper survives to the output even after route.py ripped it
    # and did NOT restore/re-route it -- the net's authoritative final copper is
    # what's on the board (pcb_data). For a net routed-then-ripped (cparti +1V8),
    # that stale input copper not only lingers but can CROSS nets that routed into
    # its vacated corridor while it was ripped. Strip every in-scope net's original
    # input segment that is no longer on the final board. A segment route.py kept
    # is still on the frozen board (matched by OBJECT IDENTITY -- rip/restore
    # preserves it) and is NOT stripped, so legitimate copper is untouched.
    # Issue #284 (segment twin of the via strip below): also strip an original
    # segment the writer will RE-EMIT verbatim (same net, same span+layer, in
    # results.new_segments) -- otherwise a ripped/re-routed net that reproduces a
    # span exactly ships the verbatim original next to a byte-identical copy. This
    # is DRC-benign (same-net overlap is permitted) but keeps the output clean.
    # The strip references the FROZEN committed board (_committed_segments,
    # snapshotted by the pipeline's freeze hook), never live pcb_data: a
    # cleanup pass's removals must not masquerade as rip signals (B1).
    _emitted_segs = [_s for _r in results for _s in (_r.get('new_segments') or [])]
    _stale_input_segs = compute_stale_input_segments(
        _orig_seg_by_net, sweep_scope_ids, _committed_segments, _emitted_segs,
        final_ids=_committed_seg_ids)
    if _stale_input_segs:
        dead_end_input_segments = list(dead_end_input_segments) + _stale_input_segs
        print(f"Stripped {len(_stale_input_segs)} stale input segment(s) of "
              f"ripped/re-routed nets not on the final board (#220)")
    # Same for input VIAS (the #220 strip was segments-only): a ripped-existing
    # net's original vias otherwise ship next to its reroute's replacements as
    # same-net drill pairs.
    # Issue #284: strip an original input via of a ripped/re-routed net when it is
    # superseded on the final board (size/drill-aware) OR when the writer will
    # re-emit a via for the same net at the same position -- either way keeping
    # the verbatim original stacks two vias in one hole. The emitted-via set is
    # the writer's: each result's new_vias plus the stub layer-swap vias (same
    # basis as _writelist_copper_209 above).
    _emitted_vias = [_v for _r in results for _v in (_r.get('new_vias') or [])]
    _emitted_vias += list(all_swap_vias)
    stale_input_vias = compute_stale_input_vias(
        _orig_via_by_net, sweep_scope_ids, _committed_vias, _emitted_vias,
        final_ids=_committed_via_ids)
    # Orphan-island removals happen AFTER the freeze snapshot, so their vias
    # look 'committed' to the stale computation -- merge their strip list
    # explicitly (an island's barrel would otherwise ship floating).
    _oi_vias = getattr(_cleanup, 'input_strip_vias', None) or []
    if _oi_vias:
        _known = {id(v) for v in stale_input_vias}
        stale_input_vias = list(stale_input_vias) + \
            [v for v in _oi_vias if id(v) not in _known]
    if stale_input_vias:
        print(f"Stripping {len(stale_input_vias)} stale input via(s) of "
              f"ripped/re-routed nets not on the final board")

    # #508 finding 3: a COMMITTED tap relocation removed a PRE-EXISTING
    # plane-net via + stub from pcb_data. Plane nets are deliberately outside
    # sweep_scope_ids (never in routed_results), so neither strip computation
    # above can see the removal -- without this merge the writer re-emits the
    # removed via from the input text, under a net that has since routed
    # through the vacated pocket: a short in the file only. (The replacement
    # re-tap via ships via all_swap_vias.)
    if state.tap_relocation_removed_segments:
        _known_trs = {id(s) for s in dead_end_input_segments}
        dead_end_input_segments = list(dead_end_input_segments) + \
            [s for s in state.tap_relocation_removed_segments
             if id(s) not in _known_trs]
    if state.tap_relocation_removed_vias:
        _known_trv = {id(v) for v in stale_input_vias}
        stale_input_vias = list(stale_input_vias) + \
            [v for v in state.tap_relocation_removed_vias
             if id(v) not in _known_trv]
    if state.tap_relocation_removed_segments or state.tap_relocation_removed_vias:
        print(f"Stripping {len(state.tap_relocation_removed_segments)} "
              f"segment(s) + {len(state.tap_relocation_removed_vias)} via(s) "
              f"removed by committed tap relocations (#508)")

    # Uniform contract, stale-strip edition: the #284 re-emit clause can strip
    # an original that is STILL on the board (a routed twin reproduced its
    # span, so the file keeps only the emitted copy) -- mirror that removal
    # into pcb_data like every other subtractive step, or the board carries
    # copper the file won't have (ottercast Net-(C46-Pad1) sliver, found by
    # KICAD_BOARD_LEDGER). Identity-based; absent objects are a no-op.
    _stale_ids = {id(s) for s in _stale_input_segs}
    if _stale_ids:
        pcb_data.segments = [s for s in pcb_data.segments if id(s) not in _stale_ids]
    _stale_via_ids = {id(v) for v in stale_input_vias}
    if _stale_via_ids:
        pcb_data.vias = [v for v in pcb_data.vias if id(v) not in _stale_via_ids]

    # Board-vs-file ledger (KICAD_BOARD_LEDGER=1): audit the pipeline contract
    # now that every strip is known -- per in-scope net, pcb_data must equal
    # the write model (original input copper - strips + emitted results).
    verify_board_file_parity(
        pcb_data, sweep_scope_ids, _orig_seg_by_net, results,
        list(dead_end_input_segments) + list(_stale_input_segs),
        label=' route')

    # Issue #209 fix C: re-check the snapshotted nets against the post-cleanup
    # write-list and report any net a cleanup pass disconnected, listing the
    # dropped copper. A non-empty list here is a cleanup BUG (the routed net was
    # connected and a graph-preserving pass severed it), not a routing failure.
    # The post basis excludes everything the writer will STRIP (cleanup strip
    # lists + stale strips) -- otherwise a strip that disconnects a net is
    # invisible to this gate (the pre basis had no strips, so the comparison
    # stays like-for-like).
    cleanup_disconnected = []
    if _pre_conn_209:
        _post_s_209, _post_v_209 = _writelist_copper_209(
            strip_seg_ids={id(s) for s in dead_end_input_segments}
                          | {id(s) for s in _stale_input_segs},
            strip_via_ids={id(v) for v in stale_input_vias})
        for _nid, (_was_conn, _was_disc, _pre_segsig, _pre_viasig) in _pre_conn_209.items():
            _pads209 = pcb_data.pads_by_net.get(_nid, [])
            _r209 = _cnc209(_nid, _post_s_209.get(_nid, []), _post_v_209.get(_nid, []),
                            _pads209, _zbn_209.get(_nid, []), tolerance=0.02)
            _now_disc = len(_r209.get('disconnected_pads') or [])
            if not ((_was_conn and not _r209.get('connected')) or _now_disc > _was_disc):
                continue
            _net_name = pcb_data.nets[_nid].name if _nid in pcb_data.nets else f"Net {_nid}"
            _post_segsig = {_seg_sig_209(s) for s in _post_s_209.get(_nid, [])}
            _post_viasig = {(round(v.x, 3), round(v.y, 3)) for v in _post_v_209.get(_nid, [])}
            cleanup_disconnected.append({
                'net_name': _net_name,
                'net_id': _nid,
                'disconnected_pads': [
                    {'x': round(p[0], 3), 'y': round(p[1], 3),
                     'component_ref': p[3] if len(p) > 3 else '?'}
                    for p in (_r209.get('disconnected_pads') or [])],
                'dropped_segments': [
                    {'start': list(sa[0]), 'end': list(sa[1]), 'layer': sa[2]}
                    for sa in (_pre_segsig - _post_segsig)],
                'dropped_vias': [{'x': vx, 'y': vy} for (vx, vy) in (_pre_viasig - _post_viasig)],
            })
        if cleanup_disconnected:
            print(f"\n{RED}WARNING: post-routing cleanup DISCONNECTED "
                  f"{len(cleanup_disconnected)} completed route(s) "
                  f"(this is a cleanup bug, not a routing failure):{RESET}")
            for _cd in cleanup_disconnected:
                print(f"  {RED}{_cd['net_name']}: dropped "
                      f"{len(_cd['dropped_segments'])} segment(s) + "
                      f"{len(_cd['dropped_vias'])} via(s) -> "
                      f"{len(_cd['disconnected_pads'])} pad(s) now unconnected{RESET}")
                for _dv in _cd['dropped_vias']:
                    print(f"      dropped via @ ({_dv['x']:.3f}, {_dv['y']:.3f})")

    # ---- VIA DEDUP (run-7 E3) ----------------------------------------------
    # Failed/partial attempts can leave a result emitting a via at the exact
    # position of a surviving input via, or of another result's via for the
    # same net: identical coords, same span -- two barrels stacked in one
    # hole. KiCad tolerates same-net stacks, so no DRC flags them; they ship
    # silently and read as parser/net rot when the file is inspected later.
    # Drop the duplicates from the write list HERE, before total_vias and the
    # authoritative sweep, so the tally, the sweep, the writer and the GUI
    # results all see the same deduped copper. Same-net only (the key carries
    # net_id): cross-net coincidence is a real short the DRC sweep owns.
    # The key is EXACT, not a proximity bucket: this pass exists to drop
    # byte-identical re-emissions, and only those. Rounding to a coarse grid
    # (the original 0.01mm bucket) is not a tolerance -- it is a bucket, so it
    # both misses duplicates that straddle a bucket edge and can collapse two
    # DISTINCT vias that happen to share one. Quantize instead to the 0.1um
    # the writer itself emits (KiCad's 4-decimal mm text), which makes the key
    # exact for anything that would round-trip through the file identically.
    # drill/size are IN the key: two same-net barrels at one point with
    # different geometry are not interchangeable, so dropping either would be
    # a silent geometry change rather than a duplicate removal. Whatever this
    # pass declines to merge is still censused into summary['stacked_copper'],
    # which is the disclosure that actually matters (KiCad never flags a
    # same-net stack).
    _survive_via_strip = {id(v) for v in stale_input_vias}

    def _via_dedup_key(v):
        return (v.net_id, tuple(v.layers or ()),
                round(v.drill, 4), round(v.size, 4),
                round(v.x, 4), round(v.y, 4))

    _kept_via_keys = set()
    for _lst0 in _orig_via_by_net.values():
        for _v0 in _lst0:
            if id(_v0) not in _survive_via_strip:
                _kept_via_keys.add(_via_dedup_key(_v0))
    for _v0 in all_swap_vias:
        _kept_via_keys.add(_via_dedup_key(_v0))
    _via_dup_dropped = []
    for _r0 in results:
        _nv0 = _r0.get('new_vias') or []
        if not _nv0:
            continue
        _kept0 = []
        for _v0 in _nv0:
            _k0 = _via_dedup_key(_v0)
            if _k0 in _kept_via_keys:
                _via_dup_dropped.append(_v0)
                continue
            _kept_via_keys.add(_k0)
            _kept0.append(_v0)
        if len(_kept0) != len(_nv0):
            _r0['new_vias'] = _kept0
    if _via_dup_dropped:
        # Keep pcb_data == write model: remove the dropped OBJECTS from the
        # board too -- unless the same object also survives as a kept
        # occurrence (the same via listed by two results keeps its first).
        _kept_obj_ids = {id(v) for _r0 in results
                         for v in (_r0.get('new_vias') or [])}
        _drop_obj_ids = {id(v) for v in _via_dup_dropped} - _kept_obj_ids
        if _drop_obj_ids:
            pcb_data.vias = [v for v in pcb_data.vias
                             if id(v) not in _drop_obj_ids]
        print(f"Via dedup: dropped {len(_via_dup_dropped)} duplicate stacked "
              f"via(s) already present at the same position/span for the "
              f"same net")

    # Count total vias from results
    total_vias = sum(len(r.get('new_vias', [])) for r in results)

    # Issue #8: reconcile the reported success counts with the FINAL segment
    # graph -- i.e. the board that will actually be written. This runs AFTER the
    # stale-result drop, snap, phantom drop and dead-end sweep (and pcb_data was
    # synced to the write-list at the stale drop), so pcb_data now matches the
    # output. A net's pads are reconciled at Phase-3 commit, but its copper can
    # change afterwards (a later net's rip-up, the recovery reroute, a dropped
    # superseded result) and leave it split, or a multi-pad net may be routed
    # with a result that never tracked every pad -- either way it would ship as
    # phantom success (neo6502 /GPIO4, glasgow /IO_Banks/IO_Buffer_A/P1). Use the
    # AUTHORITATIVE union-find (check_net_connectivity -- the model
    # filter_already_routed and check_connected.py use); the stricter geometric
    # pad-group split wrongly splits genuinely-connected power/bus nets.
    from check_connected import (check_net_connectivity,
                                 net_break_within_outlines,
                                 net_pad_pairs_within_outlines)
    # Per-net copper as it will be WRITTEN: the input board's original copper
    # MINUS everything the writer strips (cleanup strip lists + the #220/#284
    # stale strips of ripped/re-routed nets) plus the write-list's new copper.
    # NOT pcb_data, which also holds orphan copper from rip/reroute that never
    # reaches the write-list (issue #8). Counting STRIPPED originals here (the
    # pre-fix behavior) graded ripped-and-stripped nets on ghost copper the
    # output file will not contain -- a rip victim whose reroute landed only
    # partially "passed" this sweep on its stripped pre-rip copper and shipped
    # broken with no failure record (T5 zero-copper custody, ulx3s GN12 class).
    _sweep_strip_seg_ids = ({id(s) for s in dead_end_input_segments}
                            | {id(s) for s in _stale_input_segs})
    _sweep_strip_via_ids = {id(v) for v in stale_input_vias}
    _segs_by_net: Dict[int, list] = {
        nid: [s for s in lst if id(s) not in _sweep_strip_seg_ids]
        for nid, lst in _orig_seg_by_net.items()}
    _vias_by_net: Dict[int, list] = {
        nid: [v for v in lst if id(v) not in _sweep_strip_via_ids]
        for nid, lst in _orig_via_by_net.items()}
    for _r0 in results:
        for _s in _r0.get('new_segments', []):
            _segs_by_net.setdefault(_s.net_id, []).append(_s)
        for _v in _r0.get('new_vias', []):
            _vias_by_net.setdefault(_v.net_id, []).append(_v)
    # Include the stub layer-swap vias the writer adds to the output (issue
    # #292): they are not in any result's new_vias, and the original-copper
    # snapshot predates them, so without this every layer-swapped net's pad
    # looks disconnected and ships as a phantom "failed multi-point" entry.
    for _v in all_swap_vias:
        _vias_by_net.setdefault(_v.net_id, []).append(_v)
    # Swap reuse-connector segments too (#508 finding 13): all_swap_segments
    # is written to the output but is in no result's new_segments, so
    # omitting it here severs layer-swapped stubs from their pads in this
    # sweep's model -- phantom failed_multipoint entries.
    for _s in all_swap_segments:
        _segs_by_net.setdefault(_s.net_id, []).append(_s)

    # ---- STACKED-COPPER SURFACING (run-7 E3) -------------------------------
    # The dedup above drops the attributable duplicates; census what is STILL
    # stacked on the write model (duplicate same-net segments, coincident
    # same-net vias) and surface it in the summary. check_drc stays silent on
    # same-net stacks by design (KiCad permits them), so without this the
    # only way to see one was to read the raw file.
    stacked_copper_findings = []
    try:
        from check_weird import stacked_copper_over_model
        stacked_copper_findings = stacked_copper_over_model(
            _segs_by_net, _vias_by_net,
            lambda _n: (pcb_data.nets[_n].name if _n in pcb_data.nets
                        else f"Net {_n}"))
    except Exception as _swe:
        print(f"  (stacked-copper check failed: {_swe})")
    if stacked_copper_findings:
        print(f"\n{RED}WARNING: {len(stacked_copper_findings)} stacked-copper "
              f"finding(s) on the write model (same-net duplicates KiCad "
              f"will not flag):{RESET}")
        for _f in stacked_copper_findings[:8]:
            print(f"  {RED}{_f['net']} {_f['layer']}: {_f['detail']}{RESET}")
        if len(stacked_copper_findings) > 8:
            print(f"  {RED}... and {len(stacked_copper_findings) - 8} more{RESET}")

    _zones_by_net: Dict[int, list] = {}
    for _z in getattr(pcb_data, 'zones', []) or []:
        _zones_by_net.setdefault(_z.net_id, []).append(_z)
    # (pairs_total, pairs_connected) per graded net, outline-aware -- raw
    # material for the pad-pair tallies emitted with the summary (#409
    # follow-up). Filled by the sweep below and by the straggler grading at
    # summary time.
    _pad_pair_stats: Dict[int, Tuple[int, int]] = {}
    for _nid, _res in routed_results.items():
        if _nid in state.diff_pair_by_net_id:
            continue  # diff pairs report via their own path
        _pads = pcb_data.pads_by_net.get(_nid, [])
        if len(_pads) < 2:
            continue
        _r = check_net_connectivity(
            _nid, _segs_by_net.get(_nid, []), _vias_by_net.get(_nid, []),
            _pads, _zones_by_net.get(_nid, []), tolerance=0.02,
            pcb_data=pcb_data)
        # #479 multi-board: pads split only ACROSS board outlines are not
        # failures (no copper can join them); keep within-outline breaks.
        _broken, _dp = net_break_within_outlines(pcb_data, _r)
        if _dp:
            _res['failed_pads_info'] = [
                {'x': _p[0], 'y': _p[1],
                 'component_ref': _p[3] if len(_p) > 3 else '?',
                 'pad_number': '?'}
                for _p in _dp]
        elif _res.get('failed_pads_info'):
            # Authoritatively connected now: drop a stale (stricter-model or
            # pre-rip) failure flag so the net is not reported as a phantom fail.
            _res['failed_pads_info'] = []
        # Issue #184: re-derive the multi-point pad counts from this same
        # authoritative union-find (which credits pads reached via planes/zones,
        # fanout stubs, and rip-up/retry reroutes), not the per-net MST edge tally
        # gathered during routing -- otherwise the headline under-reports
        # connectivity (e.g. upsy_desky 73/101) on boards check_connected.py
        # confirms fully connected, and the tap-retry loop chases already-connected
        # pads. Match check_connected.py semantics: count over all of the net's pads.
        if _res.get('is_multipoint'):
            _res['tap_pads_total'] = len(_pads)
            _res['tap_pads_connected'] = len(_pads) - len(_dp)
        # #409 follow-up: keep this net's outline-aware pair credit for the
        # pad-pair tallies (num_components is otherwise discarded by this
        # sweep). Cross-outline gaps are neither routable nor open (#479).
        _pad_pair_stats[_nid] = net_pad_pairs_within_outlines(
            pcb_data, _r, _pads)

    # Collect multi-point tap routing stats and failed pad details
    tap_pads_connected = 0
    tap_pads_total = 0
    tap_edges_routed = 0
    tap_edges_failed = 0
    multipoint_nets = 0
    failed_multipoint = []  # List of {net_name, net_id, failed_pads: [...]}
    for net_id, result in routed_results.items():
        if result.get('is_multipoint'):
            multipoint_nets += 1
            tap_pads_connected += result.get('tap_pads_connected', 0)
            tap_pads_total += result.get('tap_pads_total', 0)
            tap_edges_routed += result.get('tap_edges_routed', 0)
            tap_edges_failed += result.get('tap_edges_failed', 0)
        # Collect failed pad details for any net with unreached pads. Issue #8:
        # non-multipoint multi-pad nets can also end disconnected (glasgow P1),
        # so this is no longer gated on is_multipoint.
        failed_pads_info = result.get('failed_pads_info', [])
        if failed_pads_info:
            net_name = pcb_data.nets[net_id].name if net_id in pcb_data.nets else f"Net {net_id}"
            failed_multipoint.append({
                'net_name': net_name,
                'net_id': net_id,
                'failed_pads': failed_pads_info
            })

    # ---- COVERAGE GATE (T5 zero-copper custody) ----------------------------
    # Invariant: a net this run DISTURBED must not ship broken and unreported.
    # The sweep above only covers routed_results; a net OUTSIDE it -- skipped as
    # "already fully connected", or a pre-existing rippable net -- can still be
    # broken by this run (ripped by the rip-up ladder and never restored, its
    # restore refused, or its input copper stale-stripped). Those nets are in no
    # failure list, so the final reconciliation never retried them and they
    # shipped at ZERO copper with no owner (ulx3s GN8/GP2/GN22). Check every
    # disturbed, unclassified, multi-pad net against the WRITTEN copper and
    # report the broken ones as failed_multipoint entries -- which both surfaces
    # them honestly in the summary/JSON and feeds them to the end-of-run
    # reconciliation retry below.
    coverage_gate_nets = []
    _cov_disturbed = (set(getattr(state, 'casualty_custody', {}) or {})
                      | set(state.collision_refused_net_ids or set())
                      | {s.net_id for s in _stale_input_segs}
                      | {v.net_id for v in stale_input_vias}
                      # #468 follow-up: a broken terminal restore ('full_open'
                      # / 'stub') puts the original copper BACK, unwinding the
                      # stale-strip signal above -- without this the net leaves
                      # the disturbed set and ships open, unreported.
                      | {nid for nid, v in
                         (getattr(state, 'terminal_restores', None) or {}).items()
                         if v != 'full'})
    _cov_classified = {nid for _, nid in single_ended_nets} | set(routed_results)
    for _nid in sorted((_cov_disturbed & sweep_scope_ids) - _cov_classified):
        _pads = pcb_data.pads_by_net.get(_nid, [])
        if len(_pads) < 2:
            continue
        _r = check_net_connectivity(
            _nid, _segs_by_net.get(_nid, []), _vias_by_net.get(_nid, []),
            _pads, _zones_by_net.get(_nid, []), tolerance=0.02,
            pcb_data=pcb_data)
        # #479 multi-board: only within-outline breaks gate coverage.
        _broken, _dp = net_break_within_outlines(pcb_data, _r)
        if not _broken or not _dp:
            continue
        _net_name = pcb_data.nets[_nid].name if _nid in pcb_data.nets else f"Net {_nid}"
        print(f"{RED}COVERAGE GATE: {_net_name} was disturbed by this run "
              f"(ripped/stripped) and ships with {len(_dp)} disconnected "
              f"pad(s) -- reporting as failed and queuing for the final "
              f"reconciliation{RESET}")
        coverage_gate_nets.append(_net_name)
        failed_multipoint.append({
            'net_name': _net_name,
            'net_id': _nid,
            'failed_pads': [
                {'x': _p[0], 'y': _p[1],
                 'component_ref': _p[3] if len(_p) > 3 else '?',
                 'pad_number': '?'}
                for _p in _dp],
        })

    # Derive final counts set-based from this run's scope rather than the loop
    # counters (issue #87): a net with unconnected pads is not fully routed, and
    # a net ripped during Phase 3 whose re-route failed never reaches the failure
    # counter.
    scope_ids = {nid for _, nid in single_ended_nets}
    failed_multipoint_ids = {m['net_id'] for m in failed_multipoint}
    fully_routed_ids = {nid for nid in scope_ids
                        if nid in routed_results and nid not in failed_multipoint_ids}
    successful = len(fully_routed_ids)
    failed = len(scope_ids) - successful

    # Now classify each net for the summary. A net only counts as routed_single
    # if it is fully connected (issue #189): one that routed a result but left
    # pads disconnected (a failed MST edge) is in failed_multipoint_ids and is
    # NOT routed. failed_single stays "no result at all" so the place/route loop
    # does not double-count it (its deficit is already in mp_deficit). The third
    # bucket, open_single, names the nets BETWEEN those two: a kept result whose
    # copper still leaves pads disconnected. Multipoint nets are excluded from
    # it -- their shortfall is already priced pad-by-pad in the multipoint
    # deficit, and counting the net again would double-charge it; a
    # non-multipoint open net previously contributed to NO count at all, so a
    # verdict of failed_single + pad-deficit read 0 while the board shipped
    # open (run-7 west-fan class).
    routed_single = []
    failed_single = []
    failed_single_ids = []  # Track IDs for history output
    open_single = []
    open_single_ids = []
    for net_name, net_id in single_ended_nets:
        if net_id in fully_routed_ids:
            routed_single.append(net_name)
        elif net_id not in routed_results:
            failed_single.append(net_name)
            failed_single_ids.append(net_id)
        elif not routed_results[net_id].get('is_multipoint'):
            open_single.append(net_name)
            open_single_ids.append(net_id)

    # Print human-readable summary
    print("\n" + "=" * 60)
    print("Routing complete")
    print("=" * 60)
    if single_ended_nets:
        if failed_single or open_single:
            _bad_bits = []
            if failed_single:
                _bad_bits.append(f"{len(failed_single)} FAILED")
            if open_single:
                _bad_bits.append(f"{len(open_single)} OPEN")
            print(f"  {RED}Single-ended:  {len(routed_single)}/{len(single_ended_nets)} routed ({', '.join(_bad_bits)}){RESET}")
        else:
            print(f"  Single-ended:  {len(routed_single)}/{len(single_ended_nets)} routed")
    if multipoint_nets > 0:
        tap_pads_failed = tap_pads_total - tap_pads_connected
        if tap_pads_failed > 0:
            print(f"  {RED}Multi-point:   {tap_pads_connected}/{tap_pads_total} pads connected ({tap_pads_failed} FAILED){RESET}")
        else:
            print(f"  Multi-point:   {tap_pads_connected}/{tap_pads_total} pads connected ({multipoint_nets} nets)")
    if ripup_success_pairs:
        print(f"  Rip-up success: {len(ripup_success_pairs)} (routes that ripped blockers)")
    if rerouted_pairs:
        print(f"  Rerouted:      {len(rerouted_pairs)} (ripped nets re-routed)")
    if single_ended_target_swaps:
        from target_swap import summarize_target_swaps
        swap_pairs = summarize_target_swaps(single_ended_target_swaps)
        print(f"  Target swaps:  {len(swap_pairs)}")
    print(f"  Total vias:    {total_vias}")
    print(f"  Total time:    {total_time:.2f}s")
    print(f"  Iterations:    {total_iterations:,}")
    _frag = getattr(config, '_fragility_field', None)
    if _frag is not None and _frag.refreshes:
        print(f"  Fragility refresh (#466): {_frag.refreshes} windows, "
              f"{_frag.refresh_s:.2f}s total")

    # Print detailed failure summary
    if failed_single:
        print(f"\n{RED}Failed single-ended nets:{RESET}")
        for net_name in failed_single:
            print(f"  {RED}{net_name}{RESET}")
    if open_single:
        # A kept result is NOT a connection: these nets ship copper with pads
        # still disconnected (pad detail follows in the multi-point list).
        print(f"\n{RED}Routed-but-OPEN single-ended nets (result kept, pads disconnected):{RESET}")
        for net_name in open_single:
            print(f"  {RED}{net_name}{RESET}")
    if failed_multipoint:
        print(f"\n{RED}Failed multi-point connections:{RESET}")
        for item in failed_multipoint:
            net_name = item['net_name']
            for pad in item['failed_pads']:
                print(f"  {RED}{net_name}: {pad['component_ref']} pad {pad['pad_number']} at ({pad['x']:.2f}, {pad['y']:.2f}) not connected{RESET}")

    # Print history for all failed nets (helps debug why they failed) --
    # open nets included: their history shows which attempt left the stub.
    if failed_single_ids or open_single_ids:
        print_failed_net_histories(state, failed_single_ids + open_single_ids,
                                   pcb_data)

    from target_swap import summarize_target_swaps  # cycle-safe swap list (#380)
    summary = {
        'routed_single': routed_single,
        'failed_single': failed_single,
        # Nets with a KEPT result whose pads are still disconnected (their pad
        # detail is in failed_multipoint). Excludes multipoint nets, whose
        # shortfall is already the multipoint pad deficit -- so a verdict may
        # safely count len(failed_single) + len(open_single) + pad-deficit.
        'open_single': open_single,
        'failed_multipoint': [
            {
                'net_name': item['net_name'],
                'failed_pads': [
                    {'component_ref': p['component_ref'], 'pad_number': p['pad_number'], 'x': p['x'], 'y': p['y']}
                    for p in item['failed_pads']
                ]
            }
            for item in failed_multipoint
        ],
        'multipoint_nets': multipoint_nets,
        'multipoint_pads_connected': tap_pads_connected,
        'multipoint_pads_total': tap_pads_total,
        'multipoint_edges_routed': tap_edges_routed,
        'multipoint_edges_failed': tap_edges_failed,
        'ripup_success_pairs': sorted(ripup_success_pairs),
        'rerouted_pairs': sorted(rerouted_pairs),
        'single_ended_target_swaps': [{'net1': k, 'net2': v} for k, v in summarize_target_swaps(single_ended_target_swaps)],
        'layer_swaps': total_layer_swaps,
        'successful': successful,
        'failed': failed,
        'total_time': round(total_time, 2),
        'total_iterations': total_iterations,
        'total_vias': total_vias,
        # Issue #209 fix C: nets a post-routing cleanup pass disconnected (a
        # cleanup bug). Empty in the normal case; non-empty flags dropped copper.
        'cleanup_disconnected': cleanup_disconnected,
        # Smallest copper clearance any step actually routed at (e.g. fine-pitch
        # taps below the nominal). Grade/check_drc the board at this floor.
        'min_clearance_used': __import__('clearance_ledger').effective(clearance),
    }
    if rescue_summary:
        # #331/#371 rescue pass outcome (key absent when nothing was rescued,
        # so pre-rescue JSON_SUMMARY consumers/diffs are unaffected).
        summary['rescue'] = rescue_summary
    if casualty_summary and casualty_summary.get('attempted'):
        # T5 custody: casualties-only reconcile tally (additive; key absent
        # when no rip casualty occurred -- the common case).
        summary['casualty_reconcile'] = casualty_summary
    if coverage_gate_nets:
        # T5 coverage gate: disturbed out-of-scope nets shipping broken
        # (additive; also present as failed_multipoint entries above).
        summary['coverage_gate_nets'] = coverage_gate_nets
    if getattr(state, 'terminal_restores', None):
        # #468 follow-up (run-7 E2): terminal-restore outcomes per net --
        # 'full' is a true restoration; 'full_open' restored copper that never
        # covered every pad; 'stub' kept only the escape stubs. Additive; the
        # broken outcomes are also graded by the sweeps/coverage gate above.
        summary['terminal_restores'] = {
            (pcb_data.nets[_n].name if _n in pcb_data.nets else str(_n)): _v
            for _n, _v in sorted(state.terminal_restores.items())}
    if stacked_copper_findings:
        # run-7 E3: same-net duplicate copper still on the write model after
        # the via dedup (additive; KiCad's DRC never flags same-net stacks,
        # so this key is the only disclosure).
        summary['stacked_copper'] = stacked_copper_findings
    # #409: report-only frontier-blocking attribution per net still failed at
    # END of run (additive; key omitted when no failed net has a recorded
    # analysis). Last-wins per net -- 'stage' names the loop that recorded it;
    # a net that failed early but was later rescued/rerouted is filtered out
    # here because the final failed sets are authoritative.
    blockers_report = []
    try:
        _final_failed_ids = list(dict.fromkeys(
            failed_single_ids + [m['net_id'] for m in failed_multipoint]))
        for _nid in _final_failed_ids:
            _fb = state.frontier_blocking.get(_nid)
            if not _fb or not _fb.get('blocked_by'):
                continue
            _name = (pcb_data.nets[_nid].name if _nid in pcb_data.nets
                     else f"Net {_nid}")
            _rec = {'net': _name, 'stage': _fb['stage'],
                    'blocked_by': _fb['blocked_by']}
            if _fb.get('more'):
                _rec['more'] = _fb['more']
            blockers_report.append(_rec)
        if blockers_report:
            summary['blockers'] = blockers_report
    except Exception:
        blockers_report = []
    # #409 follow-up: pad-pair routability tallies (PRR ingredients: connected
    # = |pads| - pad components from the authoritative union-find above, total
    # = |pads| - 1, both counted per outer outline -- a cross-outline gap is
    # neither a routable pair nor an open one, matching #479's
    # net_break_within_outlines semantics) plus a per-open-net outcome.
    # Population: every multi-pad net graded against the WRITTEN board -- the
    # sweep over routed_results (which includes pre-existing rippable nets)
    # plus scope nets with no result and coverage-gate nets, graded here
    # identically. NOT reconcilable with multipoint_edges_* (component-MST:
    # pre-existing copper joins terminals, so there are fewer edges than pad
    # pairs). route.py runs no DRC, so every route-time deficit is an 'open';
    # shorts are check_drc.py's domain ('outcome' exists so a DRC-integrated
    # emitter can add 'short' without a schema break). Computed before the
    # final reconciliation, like 'blockers'.
    pad_pairs_open_report = []
    try:
        _straggler_ids = list(failed_single_ids) + \
            [m['net_id'] for m in failed_multipoint]
        for _nid in _straggler_ids:
            if _nid in _pad_pair_stats or _nid in state.diff_pair_by_net_id:
                continue
            _pads = pcb_data.pads_by_net.get(_nid, [])
            if len(_pads) < 2:
                continue
            _r = check_net_connectivity(
                _nid, _segs_by_net.get(_nid, []), _vias_by_net.get(_nid, []),
                _pads, _zones_by_net.get(_nid, []), tolerance=0.02,
                pcb_data=pcb_data)
            _pad_pair_stats[_nid] = net_pad_pairs_within_outlines(
                pcb_data, _r, _pads)
        _pp_conn = 0
        _pp_total = 0
        _refused = set(state.collision_refused_net_ids or set())
        for _nid, (_pt, _pc) in _pad_pair_stats.items():
            # The k == 0 (padless-copper answer) and invisible-pad clamps live
            # in net_pad_pairs_within_outlines, applied per outline.
            _pp_total += _pt
            _pp_conn += _pc
            if _pc >= _pt:
                continue
            _name = (pcb_data.nets[_nid].name if _nid in pcb_data.nets
                     else f"Net {_nid}")
            if _nid in _refused:
                _sub = 'collision_refused'
            elif _nid in failed_multipoint_ids and _nid not in routed_results:
                _sub = 'coverage_gate'
            elif _nid not in routed_results:
                _sub = 'unrouted'
            else:
                _sub = 'partial'
            pad_pairs_open_report.append({
                'net': _name, 'pairs_connected': _pc, 'pairs_total': _pt,
                'outcome': 'open', 'open_subtype': _sub})
        pad_pairs_open_report.sort(key=lambda e: e['net'])
        if _pp_total:
            summary['pad_pairs_connected'] = _pp_conn
            summary['pad_pairs_total'] = _pp_total
        if pad_pairs_open_report:
            summary['pad_pairs_open'] = pad_pairs_open_report
    except Exception:
        pad_pairs_open_report = []

    # #487: trace-side ampacity for the POWER nets this call sized (the IPC
    # model was plane-only; a routed power trace's capacity went unreported).
    # Bottleneck segment at the layer's stackup copper weight; report-only,
    # printed AND carried in the summary so chains/graders can gate on it.
    try:
        from plane_resistance import (analyze_power_trace_net,
                                      print_power_trace_ampacity)
        _amp = {}
        for _pnid in sorted(config.power_net_widths or {}):
            _r = analyze_power_trace_net(pcb_data, _pnid)
            if _r:
                _pname = (pcb_data.nets[_pnid].name
                          if _pnid in pcb_data.nets else f"net_{_pnid}")
                _amp[_pname] = _r
        if _amp:
            print_power_trace_ampacity(_amp)
            summary['power_trace_ampacity'] = [
                {'net': _n,
                 'bottleneck_width_mm': round(_r['bottleneck_width'], 4),
                 'bottleneck_layer': _r['bottleneck_layer'],
                 'copper_oz': _r['copper_oz'],
                 'max_current_a': round(_r['max_current'], 2),
                 'max_current_ipc2152_a': round(_r['max_current_ipc2152'], 2),
                 'temp_rise_c': _r['temp_rise_c'],
                 'trace_length_mm': round(_r['trace_length'], 1)}
                for _n, _r in sorted(_amp.items())]
    except Exception:
        pass
    print(f"JSON_SUMMARY: {json.dumps(summary)}")

    # Write output file or return results for direct application
    if return_results:
        # Return results data for direct application (e.g., KiCad plugin).
        # Swap/modification info must be applied to the live board just like
        # write_routed_output applies it to the output file.
        results_data = {
            'results': results,
            'all_swap_vias': all_swap_vias,
            'all_swap_segments': all_swap_segments,
            'pad_swaps': pad_swaps,
            'single_ended_target_swap_info': single_ended_target_swap_info,
            'all_segment_modifications': all_segment_modifications,
            'exclusion_zone_lines': exclusion_zone_lines if debug_lines else [],
            'boundary_debug_labels': boundary_debug_labels if debug_lines else [],
            # Original-file dead-end copper the caller (GUI) should delete from the
            # live board, mirroring the writer's strip (issue #84).
            'segments_to_remove': dead_end_input_segments,
            'vias_to_remove': stale_input_vias,
            # #409: same data as JSON_SUMMARY['blockers'] (may be empty).
            'blockers': blockers_report,
            # #409 follow-up: same data as JSON_SUMMARY['pad_pairs_open']
            # (may be empty).
            'pad_pairs_open': pad_pairs_open_report,
        }
    else:
        # Write output file using extracted output_writer module
        wrote = write_routed_output(
            input_file=input_file,
            output_file=output_file,
            results=results,
            all_segment_modifications=all_segment_modifications,
            all_swap_vias=all_swap_vias,
            all_swap_segments=all_swap_segments,
            target_swap_info=[],
            single_ended_target_swap_info=single_ended_target_swap_info,
            pad_swaps=pad_swaps,
            pcb_data=pcb_data,
            debug_lines=debug_lines,
            exclusion_zone_lines=exclusion_zone_lines,
            boundary_debug_labels=boundary_debug_labels,
            skip_routing=skip_routing,
            add_teardrops=add_teardrops,
            segments_to_remove=dead_end_input_segments,
            vias_to_remove=stale_input_vias
        )
        # When nothing could be routed (every net failed) there is no copper to
        # write, so write_routed_output produces no file. Pass the board through
        # unchanged so the pipeline never loses its board and a later step (a
        # finer-grid retry, planes, repair) can still run on it (issues #90, #167)
        # -- otherwise the whole chain FileNotFoundErrors on the missing output.
        if not wrote and output_file:
            _write_passthrough_output(input_file, output_file)
        elif wrote and output_file:
            # Deep ledger (KICAD_BOARD_LEDGER=1): the written FILE must match
            # pcb_data per in-scope net -- audits the writer's text transforms
            # (layer mods, polarity/target swap relabels, strips) on top of the
            # in-memory contract checked before the write.
            verify_written_file_parity(output_file, pcb_data, sweep_scope_ids,
                                       label=' route')

    # Update schematics with swap info if directory specified
    if schematic_dir and single_ended_target_swap_info:
        # Convert swap info to format for schematic updater
        schematic_swaps = []
        for info in single_ended_target_swap_info:
            if info.get('n1_pad') and info.get('n2_pad'):
                pad1 = info['n1_pad']
                pad2 = info['n2_pad']
                schematic_swaps.append({
                    'component_ref': pad1.component_ref,
                    'pad1': pad1.pad_number,
                    'pad2': pad2.pad_number
                })
        if schematic_swaps:
            apply_swaps_to_schematics(schematic_dir, schematic_swaps, verbose=verbose)

    # Final memory summary
    if debug_memory:
        final_mem = get_process_memory_mb()
        print("\n" + "=" * 60)
        print("[MEMORY] Final Memory Summary")
        print("=" * 60)
        print(f"  Process RSS: {final_mem:.1f} MB (delta: {final_mem - mem_start:+.1f} MB)")
        print(f"  Net obstacles cache: {estimate_net_obstacles_cache_mb(state.net_obstacles_cache):.1f} MB ({len(state.net_obstacles_cache)} nets)")
        print(f"  Track proximity cache: {estimate_track_proximity_cache_mb(state.track_proximity_cache):.1f} MB ({len(state.track_proximity_cache)} nets)")
        print(f"  Routed paths: {estimate_routed_paths_mb(state.routed_net_paths):.1f} MB ({len(state.routed_net_paths)} nets)")
        print(f"  Routed results: {len(state.routed_results)} nets")
        print(format_obstacle_map_stats(state.working_obstacles))
        print("=" * 60)

    # Obstacle-map ref-count integrity audit (issue: ref-count leak/desync hunt).
    # Invariant: working_obstacles == base_obstacles + sum(net_obstacles_cache).
    # Clone the working map, remove every net's CURRENT cache, and compare to the
    # base. Any residual means a per-net contribution the cache no longer accounts
    # for -- a leak (add not mirrored by remove) or an over-decrement. Env-gated so
    # normal runs pay nothing; fully defensive (never breaks a real route).
    if env_knobs.OBSTACLE_AUDIT:
        from obstacle_cache import run_obstacle_audit
        run_obstacle_audit(base_obstacles, state.working_obstacles,
                           state.net_obstacles_cache)

    # #348 (glasgow /SCL): END-OF-RUN RECONCILIATION. Mid-run rip churn can
    # leave a victim net partially connected whose gap is trivially routable
    # in the FINAL board state -- /SCL shipped with 6 pads open after a rip
    # victim's re-route failed against an interim board, yet route.py
    # connects it in 0.25s when asked on the finished board (Andy proved the
    # corridor by hand-bridging it with one via first). The authoritative
    # end-of-run check above knows exactly which nets are incomplete; give
    # them ONE more standard pass against the board as written. Self-invokes
    # batch_route on the output file (one level -- final_reconcile=False),
    # mirroring repair_planes' rip-casualty self-reconnect. The
    # reconcile pass prints its own summary/JSON_SUMMARY scoped to the
    # retried nets; the failure lists in that LAST summary are the honest
    # still-open set. Runs on BOTH fronts: CLI re-invokes on the written
    # file; GUI (return_results) re-invokes on the in-memory board and
    # merges the sub-run's results (claude-tab/stress parity gap closure).
    _custody_nets9 = []
    _zone_complete9 = set()
    # PLANE FINALIZE (#562): the pours-first chain's repair step, absorbed.
    # Measured (3-board arch chains): the standalone repair step's entire
    # remaining value is plane-net completion its TAP machinery + the
    # kicad-oracle exact-fill reconnect earn (ux +3V3: 21 pad taps + gate
    # multipoint + rescue; oc P3.3V: one pad tap) -- the casualties-only and
    # failed-nets reconciles above cannot reach it (multipoint plane deficits
    # are neither). Run repair's exact trio IN-RUN on the written output:
    # route_planes engine (taps + region joins) -> clean_plane_copper ->
    # oracle_reconnect (KiCad exact-fill links, the completion earner).
    # ORDER SWAP: runs BEFORE the final reconciliation -- live reuse is then
    # sound by construction, and the custody nets merge into the reconcile's
    # single sub-run (one parse/base-build serves both).
    # In-process bonus: clearance_ledger is shared, so tap-escalated floors
    # flow into this run's own DRC writeback.
    # BOTH FRONTS (#562 graduation): CLI file-mode runs repair's file trio on
    # the written output; GUI (return_results) runs the SAME engine against
    # the in-memory board -- repair_planes(pcb_data=..., return_results=True)
    # already runs the shared cleanup pipeline in-memory, and the finalize
    # merges the resulting BOARD DELTA (object diff of pcb_data before/after)
    # into results_data, so the reconcile's identity-based strip de-dup keeps
    # working. The ORACLE leg needs a real file for kicad-cli's exact fill,
    # so under return_results it is deferred to the applier: the finalize
    # posts results_data['plane_finalize_oracle'] and swig_gui runs the
    # staged-save oracle (planes-tab pattern) after the copper lands on the
    # live board.
    # Gate: KICAD_PLANE_FINALIZE=1 (default ON; env var is a kill switch).
    if (final_reconcile and not skip_routing and not _ckpt_stop
            and (output_file or return_results)
            and not _plane_finalize_active()
            and os.environ.get('KICAD_PLANE_FINALIZE', '1') == '1'):
        try:
            _finalize_depth(+1)
            from repair_planes import (
                repair_planes as _rdp_engine, auto_detect_zones as _adz)
            _gui9 = bool(return_results)
            if _gui9:
                # No written file to detect zones from: derive the (net,
                # layer) pairs from the in-memory board, in zone order,
                # deduped -- the same pairs auto_detect_zones reads from the
                # file (one entry per copper layer of each pour).
                _zpairs = []
                _seen9 = set()
                for _z9 in (getattr(pcb_data, 'zones', None) or []):
                    _nm9 = (pcb_data.nets[_z9.net_id].name
                            if _z9.net_id in pcb_data.nets
                            else getattr(_z9, 'net_name', None))
                    _ly9 = getattr(_z9, 'layer', None)
                    if not _nm9 or not _ly9 or not _ly9.endswith('.Cu'):
                        continue
                    if (_nm9, _ly9) not in _seen9:
                        _seen9.add((_nm9, _ly9))
                        _zpairs.append((_nm9, _ly9))
            else:
                _zpairs = _adz(output_file)
            # Respect the caller's net filter: a net excluded by pattern is
            # excluded BY PLAN (same rule as the reconciliation above).
            # But say so LOUDLY. Since #562 a pour ALONE connects nothing --
            # a plane net outside this route's scope gets no weld, no
            # finalize, no oracle, and if no LATER route step covers it,
            # every one of its pads ships disconnected while this step
            # reports success for its own scope (review finding F1: a
            # scoped retry chain `route.py --nets 'SDRAM_*'` after a pour
            # is a realistic shape that hits this).
            if net_names:
                from net_queries import matches_net_filter as _mnf9
                _excluded9 = sorted({n for n, _l in _zpairs
                                     if not _mnf9(n, net_names)})
                _zpairs = [(n, l) for n, l in _zpairs if _mnf9(n, net_names)]
                if _excluded9:
                    print(f"{RED}  Plane finalize: zone net(s) "
                          f"{', '.join(_excluded9)} are OUTSIDE this route's "
                          f"--nets scope -- excluded from the finalize BY "
                          f"PLAN. Since #562 a pour alone connects nothing: "
                          f"unless a later route step covers these nets, "
                          f"their pads ship disconnected.{RESET}")
            # PRE-GATE for the MODEL-BASED legs (engine taps/joins +
            # cleanup): run them only for zone nets the fill-aware checker
            # says are incomplete on THIS run's board (pcb_data == file
            # here -- the finalize precedes the reconcile). The ORACLE leg deliberately stays
            # UNGATED on the full zone-net set: the raster model
            # OVER-credits (castor-class gaps only KiCad's exact fill
            # sees), and the oracle is the model-independent verifier --
            # on a healthy board it costs one refill and exits at round 0.
            _zpairs_all = list(_zpairs)
            if _zpairs:
                from check_connected import check_net_connectivity as _cnc9
                _zbn9 = {}
                for _z9 in (getattr(pcb_data, 'zones', None) or []):
                    _zbn9.setdefault(_z9.net_id, []).append(_z9)
                _broken9 = []
                for _nid9, _net9 in pcb_data.nets.items():
                    if _net9.name not in {n for n, _l in _zpairs}:
                        continue
                    try:
                        _r9 = _cnc9(
                            _nid9,
                            [s for s in pcb_data.segments
                             if s.net_id == _nid9],
                            [v for v in pcb_data.vias if v.net_id == _nid9],
                            pcb_data.pads_by_net.get(_nid9, []),
                            _zbn9.get(_nid9, []), pcb_data=pcb_data)
                        if not _r9.get('connected'):
                            _broken9.append(_net9.name)
                    except Exception:
                        _broken9.append(_net9.name)  # unknown -> repair it
                if not _broken9:
                    print("\nPlane finalize (#562): all zone nets complete "
                          "(fill-aware) -- skipping engine/cleanup legs, "
                          "oracle verifies")
                    _zpairs = []
                else:
                    _zpairs = [(n, _l) for n, _l in _zpairs
                               if n in set(_broken9)]
            if _zpairs:
                _zn = [n for n, _l in _zpairs]
                _zl = [_l for _n, _l in _zpairs]
                print(f"\nPlane finalize (#562): repairing "
                      f"{len(set(_zn))} zone net(s) in-run: "
                      f"{', '.join(sorted(set(_zn)))}")
                import time as _time9
                _t9 = _time9.time()
                # Live-board reuse (KICAD_PLANE_FINALIZE_LIVE=1): hand the
                # engine THIS run's pcb_data instead of re-parsing the file it
                # just wrote -- kills the parse and, bigger, reuses the
                # ZoneFillModel caches the pour-launch ladder already built
                # for every plane net. SNAP FIRST (the reconcile lesson: the
                # written file is nm-grid-exact, the in-memory board is not,
                # and repairing against a different board than the file forks
                # the outcome). #508-class risk (write-list vs pcb_data
                # divergence) is why this has its own gate; A/B with
                # KICAD_BOARD_LEDGER=1 before defaulting it on.
                # Live reuse is sound BY CONSTRUCTION here: the finalize now
                # runs BEFORE the final reconciliation (order swap), so the
                # only writes since pcb_data was current are this run's own
                # (FILE_LEDGER-verified at the main write). The old guard
                # existed because the reconcile's CLI sub-run wrote file-only
                # copper ahead of the finalize -- that ordering is gone.
                _pcb9 = None
                if progress_callback:
                    _pcb9 = (lambda c, t, m, _o=progress_callback:
                             _o(c, t, f"Plane finalize: {m}"))
                _live9 = None
                if _gui9 or os.environ.get('KICAD_PLANE_FINALIZE_LIVE',
                                           '1') == '1':
                    from kicad_parser import snap_pcb_data_to_iu_grid \
                        as _snap9
                    _snapped9 = _snap9(pcb_data)
                    if _snapped9:
                        print(f"  Plane finalize: snapped {_snapped9} "
                              f"in-memory coordinate(s) onto the nm grid")
                    _live9 = pcb_data
                if _gui9:
                    # In-memory engine leg (GUI): repair appends its kept
                    # copper to pcb_data as REAL objects and its return
                    # branch runs the shared cleanup pipeline (the same
                    # run_post_route_cleanup call clean_plane_copper wraps),
                    # so the separate cleanup leg below is CLI-file-only.
                    # Merge the BOARD DELTA -- not the dict emissions --
                    # into results_data: identity survives into the
                    # reconcile's strip de-dup, and the delta also captures
                    # any strip-and-replace outcome exactly.
                    _b4sid9 = {id(s) for s in pcb_data.segments}
                    _b4vid9 = {id(v) for v in pcb_data.vias}
                    _b4segs9 = list(pcb_data.segments)
                    _b4vias9 = list(pcb_data.vias)
                    _rdp_engine(
                        input_file or "", "", _zn, _zl,
                        track_width=config.track_width,
                        clearance=config.clearance,
                        grid_step=config.grid_step,
                        via_size=config.via_size,
                        via_drill=config.via_drill,
                        hole_to_hole_clearance=config.hole_to_hole_clearance,
                        routing_layers=config.layers,
                        net_clearances=net_clearances,
                        layer_clearances=dict(config.layer_clearances or {}),
                        # #338 (review DRC-1): forward THIS run's RESOLVED
                        # copper-to-edge floor. The engine's own re-resolve
                        # cannot work here: its PLANE_EDGE_CLEARANCE default
                        # (0.5) reads as an explicit override and masks the
                        # project rule, so a board declaring 0.6 got finalize
                        # copper at 0.5 -- real edge DRC.
                        board_edge_clearance=config.board_edge_clearance,
                        pcb_data=pcb_data, return_results=True,
                        progress_callback=_pcb9)
                    _cursid9 = {id(s) for s in pcb_data.segments}
                    _curvid9 = {id(v) for v in pcb_data.vias}
                    _new_s9 = [s for s in pcb_data.segments
                               if id(s) not in _b4sid9]
                    _new_v9 = [v for v in pcb_data.vias
                               if id(v) not in _b4vid9]
                    _rm_s9 = [s for s in _b4segs9 if id(s) not in _cursid9]
                    _rm_v9 = [v for v in _b4vias9 if id(v) not in _curvid9]
                    # A removal naming copper THIS run emitted must drop it
                    # from our write-lists instead of riding the remove
                    # channels (reconcile-merge pattern: the applier's
                    # key-based remove would no-op on a not-yet-added
                    # object and the withdrawn copper would ship).
                    _ours_s9 = {id(s) for _r in results_data.get('results', [])
                                for s in (_r.get('new_segments') or [])}
                    _ours_v9 = {id(v) for _r in results_data.get('results', [])
                                for v in (_r.get('new_vias') or [])}
                    _drop_s9 = {id(s) for s in _rm_s9 if id(s) in _ours_s9}
                    _drop_v9 = {id(v) for v in _rm_v9 if id(v) in _ours_v9}
                    if _drop_s9 or _drop_v9:
                        for _r in results_data.get('results', []):
                            if _drop_s9:
                                _r['new_segments'] = [
                                    s for s in (_r.get('new_segments') or [])
                                    if id(s) not in _drop_s9]
                            if _drop_v9:
                                _r['new_vias'] = [
                                    v for v in (_r.get('new_vias') or [])
                                    if id(v) not in _drop_v9]
                    if _new_s9 or _new_v9:
                        results_data.setdefault('results', []).append({
                            'net_name': '(plane finalize)',
                            'success': True,
                            'new_segments': _new_s9,
                            'new_vias': _new_v9})
                    results_data.setdefault('segments_to_remove', []).extend(
                        s for s in _rm_s9 if id(s) not in _drop_s9)
                    results_data.setdefault('vias_to_remove', []).extend(
                        v for v in _rm_v9 if id(v) not in _drop_v9)
                    print(f"  Plane finalize (GUI): +{len(_new_s9)} seg(s) "
                          f"+{len(_new_v9)} via(s), -{len(_rm_s9)} seg(s) "
                          f"-{len(_rm_v9)} via(s) merged into results")
                else:
                    _rdp_engine(
                        output_file, output_file, _zn, _zl,
                        track_width=config.track_width,
                        clearance=config.clearance,
                        grid_step=config.grid_step,
                        via_size=config.via_size,
                        via_drill=config.via_drill,
                        hole_to_hole_clearance=config.hole_to_hole_clearance,
                        routing_layers=config.layers,
                        net_clearances=net_clearances,
                        # #498: forward THIS run's resolved per-layer map --
                        # the output file's .kicad_dru sibling does not exist
                        # yet, so the engine's own auto-read would find none
                        # and tap/join copper would route blind to the rules.
                        layer_clearances=dict(config.layer_clearances or {}),
                        # #338 (review DRC-1): same reason for the edge floor
                        # -- output_file has no sibling .kicad_pro yet, and
                        # the engine default 0.5 masks the project read.
                        board_edge_clearance=config.board_edge_clearance,
                        pcb_data=_live9,
                        progress_callback=_pcb9)
                print(f"  [finalize timing] engine leg: "
                      f"{_time9.time() - _t9:.1f}s")
                if not _gui9:
                    _t9 = _time9.time()
                    from pcb_modification import clean_plane_copper
                    _cs, _cr = clean_plane_copper(
                        output_file, sorted(set(_zn)),
                        config.clearance, config.grid_step)
                    print(f"  [finalize timing] cleanup leg: "
                          f"{_time9.time() - _t9:.1f}s")
                    if _cs or _cr:
                        print(f"  Plane finalize cleanup: closed {_cs} stub "
                              f"gap(s), trimmed {_cr} dead-end segment(s)")
            # ORACLE leg: kicad-cli computes the exact fill, so this leg
            # needs a REAL file on disk.
            #   CLI -- the written output_file.
            #   GUI -- a STAGED file: the caller's live board (stage_board_fn)
            #          with this run's pending copper written onto it, which
            #          is exactly what output_file holds at this point in file
            #          mode. Staging here (rather than deferring to the
            #          applier) is what makes the two fronts agree: the oracle
            #          copper lands BEFORE the final reconciliation and its
            #          unroutable links feed custody, same as the CLI.
            # The base MUST come from stage_board_fn, never from input_file:
            # the GUI's input_file is the ORIGINAL board on disk and is
            # missing whatever earlier chain steps applied in-session
            # (staging from it measured GUI DRC 431 vs CLI 0).
            _orc_file9 = None
            _orc_tmp9 = []
            if _zpairs_all and not _gui9:
                _orc_file9 = output_file
            elif _zpairs_all and _gui9 and stage_board_fn is not None:
                try:
                    import tempfile as _tf9
                    _base9 = stage_board_fn()
                    if _base9:
                        _orc_tmp9.append(_base9)
                        _fd9, _stg9 = _tf9.mkstemp(suffix='.kicad_pcb')
                        os.close(_fd9)
                        _orc_tmp9.append(_stg9)
                        if write_routed_output(
                                input_file=_base9, output_file=_stg9,
                                results=results,
                                all_segment_modifications=(
                                    all_segment_modifications),
                                all_swap_vias=all_swap_vias,
                                all_swap_segments=all_swap_segments,
                                target_swap_info=[],
                                single_ended_target_swap_info=(
                                    single_ended_target_swap_info),
                                pad_swaps=pad_swaps, pcb_data=pcb_data,
                                skip_routing=skip_routing,
                                add_teardrops=add_teardrops,
                                segments_to_remove=results_data.get(
                                    'segments_to_remove'),
                                vias_to_remove=results_data.get(
                                    'vias_to_remove')):
                            _orc_file9 = _stg9
                except Exception as _e9:
                    print(f"  Plane finalize (GUI): could not stage a board "
                          f"for the oracle leg ({_e9}); deferring to apply")
                    _orc_file9 = None
            if _zpairs_all and _gui9 and _orc_file9 is None:
                # FALLBACK only (no stage_board_fn, or staging failed): post
                # the net list + engine-resolved params and let the applier
                # run the staged-save oracle after the copper lands (the
                # planes-tab pattern). Custody cannot merge into the
                # reconcile on this path -- there is no verdict yet.
                _zna = sorted({n for n, _l in _zpairs_all})
                results_data['plane_finalize_oracle'] = {
                    'nets': _zna,
                    'clearance': config.clearance,
                    'track_width': config.track_width,
                    'via_size': config.via_size,
                    'via_drill': config.via_drill,
                    'grid_step': config.grid_step,
                    'hole_to_hole_clearance': config.hole_to_hole_clearance,
                    # #498: the applier's temp save has no .kicad_dru sibling
                    'layer_clearances': dict(config.layer_clearances or {}),
                }
                # Hands-off for the reconcile comes from the FILL-AWARE
                # checker instead of the oracle verdict: zone nets the model
                # says are complete after the engine leg must not be
                # re-touched by the STALE failure buckets (the pf7
                # regression class). Model over-credit only defers a fix to
                # the post-apply oracle -- never breaks a healthy net.
                try:
                    from check_connected import (
                        check_net_connectivity as _cnc9b)
                    _zbn9b = {}
                    for _z9b in (getattr(pcb_data, 'zones', None) or []):
                        _zbn9b.setdefault(_z9b.net_id, []).append(_z9b)
                    for _nid9b, _net9b in pcb_data.nets.items():
                        if _net9b.name not in set(_zna):
                            continue
                        try:
                            _r9b = _cnc9b(
                                _nid9b,
                                [s for s in pcb_data.segments
                                 if s.net_id == _nid9b],
                                [v for v in pcb_data.vias
                                 if v.net_id == _nid9b],
                                pcb_data.pads_by_net.get(_nid9b, []),
                                _zbn9b.get(_nid9b, []), pcb_data=pcb_data)
                            if _r9b.get('connected'):
                                _zone_complete9.add(_net9b.name)
                        except Exception:
                            pass
                except Exception:
                    pass
                if _zone_complete9:
                    print(f"  Plane finalize (GUI): {len(_zone_complete9)} "
                          f"zone net(s) verified complete (fill-aware) -- "
                          f"hands-off for the reconciliation")
            if _orc_file9:
                # Oracle + custody scope: ALL zone nets (see the pre-gate
                # note -- the oracle is the model-independent verifier).
                import time as _time9
                _zna = sorted({n for n, _l in _zpairs_all})
                _t9 = _time9.time()
                from kicad_oracle import oracle_reconnect
                try:
                    from fix_kicad_drc_settings import \
                        effective_board_edge_clearance
                    _oedge = effective_board_edge_clearance(input_file, 0.0)
                except Exception:
                    _oedge = 0.0
                _ocfg = GridRouteConfig(
                    clearance=config.clearance,
                    track_width=config.track_width,
                    via_size=config.via_size, via_drill=config.via_drill,
                    grid_step=config.grid_step,
                    board_edge_clearance=_oedge)
                from kicad_dru import install_layer_clearances
                install_layer_clearances(_ocfg, None, input_file, None)
                _orc = oracle_reconnect(
                    _orc_file9, _zna, _ocfg,
                    track_via_clearance=defaults.PLANE_TRACK_VIA_CLEARANCE,
                    hole_to_hole_clearance=config.hole_to_hole_clearance,
                    project_from=input_file)
                print(f"  [finalize timing] oracle leg: "
                      f"{_time9.time() - _t9:.1f}s")
                if _gui9:
                    # The staged file is a throwaway: hand the oracle's copper
                    # back through the SAME channels the engine leg uses.
                    # Removals need GEOMETRY-key matching, not id(): the
                    # oracle PARSED the staged temp file, so its
                    # removed_segments/removed_vias are parse products whose
                    # id() can never equal a write-list object's -- an
                    # identity de-dup here is dead code by construction
                    # (found by two independent reviewers; the withdrawn
                    # copper then rode the remove channel, no-op'd against
                    # the not-yet-populated live board because the applier
                    # REMOVES FIRST and adds after, and shipped anyway).
                    # Three channels are handled, all by geometry key:
                    #   1. removal names copper THIS run emitted (results'
                    #      new_segments/new_vias AND the swap channels) ->
                    #      drop it from the write-list;
                    #   2. removal names pre-existing board copper -> ride
                    #      segments_to_remove/vias_to_remove for the applier;
                    #   3. EITHER WAY, mirror the removal into pcb_data so
                    #      the final reconciliation routes against the same
                    #      board the applier will produce (the CLI reconcile
                    #      re-parses the oracle-edited file and gets this
                    #      for free).
                    _os9 = list(_orc.get('new_segments') or [])
                    _ov9 = list(_orc.get('new_vias') or [])
                    _ors9 = list(_orc.get('removed_segments') or [])
                    _orv9 = list(_orc.get('removed_vias') or [])

                    def _skey9(s):
                        a = (round(s.start_x, 4), round(s.start_y, 4))
                        b = (round(s.end_x, 4), round(s.end_y, 4))
                        return (s.net_id, s.layer, min(a, b), max(a, b))

                    def _vkey9(v):
                        return (v.net_id, round(v.x, 4), round(v.y, 4))

                    _rm_skeys9 = {_skey9(s) for s in _ors9}
                    _rm_vkeys9 = {_vkey9(v) for v in _orv9}
                    _matched_skeys9, _matched_vkeys9 = set(), set()
                    if _rm_skeys9 or _rm_vkeys9:
                        _lists9 = [(_r, 'new_segments', 'new_vias')
                                   for _r in results_data.get('results', [])]
                        for _r, _sk, _vk in _lists9:
                            if _rm_skeys9 and _r.get(_sk):
                                _keep = [s for s in _r[_sk]
                                         if _skey9(s) not in _rm_skeys9]
                                if len(_keep) != len(_r[_sk]):
                                    _matched_skeys9.update(
                                        _skey9(s) for s in _r[_sk]
                                        if _skey9(s) in _rm_skeys9)
                                    _r[_sk] = _keep
                            if _rm_vkeys9 and _r.get(_vk):
                                _keep = [v for v in _r[_vk]
                                         if _vkey9(v) not in _rm_vkeys9]
                                if len(_keep) != len(_r[_vk]):
                                    _matched_vkeys9.update(
                                        _vkey9(v) for v in _r[_vk]
                                        if _vkey9(v) in _rm_vkeys9)
                                    _r[_vk] = _keep
                        # Swap channels are this-run copper too (the #284
                        # withdrawn-copper class): a removal naming a swap
                        # via/segment must not ship it either.
                        if _rm_skeys9 and all_swap_segments:
                            _matched_skeys9.update(
                                _skey9(s) for s in all_swap_segments
                                if _skey9(s) in _rm_skeys9)
                            all_swap_segments[:] = [
                                s for s in all_swap_segments
                                if _skey9(s) not in _rm_skeys9]
                        if _rm_vkeys9 and all_swap_vias:
                            _matched_vkeys9.update(
                                _vkey9(v) for v in all_swap_vias
                                if _vkey9(v) in _rm_vkeys9)
                            all_swap_vias[:] = [
                                v for v in all_swap_vias
                                if _vkey9(v) not in _rm_vkeys9]
                        # Channel 3: mirror EVERY removal into pcb_data.
                        pcb_data.segments[:] = [
                            s for s in pcb_data.segments
                            if _skey9(s) not in _rm_skeys9]
                        pcb_data.vias[:] = [
                            v for v in pcb_data.vias
                            if _vkey9(v) not in _rm_vkeys9]
                    # Channel 2: only removals NOT matched to this-run copper
                    # ride the remove channels (the applier removes them from
                    # the live board before adding).
                    _ride_s9 = [s for s in _ors9
                                if _skey9(s) not in _matched_skeys9]
                    _ride_v9 = [v for v in _orv9
                                if _vkey9(v) not in _matched_vkeys9]
                    if _ride_s9 or _ride_v9:
                        results_data.setdefault(
                            'segments_to_remove', []).extend(_ride_s9)
                        results_data.setdefault(
                            'vias_to_remove', []).extend(_ride_v9)
                    if _os9 or _ov9:
                        results_data.setdefault('results', []).append({
                            'net_name': '(plane finalize oracle)',
                            'success': True,
                            'new_segments': _os9, 'new_vias': _ov9})
                        # Also into pcb_data, so the final reconciliation
                        # below prices this copper as a real obstacle and
                        # sees the nets it just completed.
                        pcb_data.segments.extend(_os9)
                        pcb_data.vias.extend(_ov9)
                    print(f"  Plane finalize oracle (GUI): +{len(_os9)} "
                          f"seg(s) +{len(_ov9)} via(s), -{len(_ors9)} seg(s) "
                          f"-{len(_orv9)} via(s) merged into results "
                          f"({len(_ors9) - len(_ride_s9)} seg / "
                          f"{len(_orv9) - len(_ride_v9)} via removal(s) "
                          f"matched this run's own copper)")
                try:
                    import json as _json9
                    print('JSON_ORACLE: ' + _json9.dumps(
                        {k: v for k, v in _orc.items()
                         if k not in ('new_segments', 'new_vias')}))
                except Exception:
                    pass
                # Custody (the reason this runs IN-RUN): links the oracle's
                # own router leaves flagged after 3 rounds failed WITHOUT
                # rip authority -- its link-router cannot rip a signal net
                # sitting on the only corridor. Their nets MERGE into the
                # final reconciliation below (order swap: finalize first,
                # ONE sub-run for failed signal nets + stubborn plane nets
                # -- saves a whole parse + base-build self-invocation, and
                # the reconcile then retries signals against the WELDED
                # planes, pours-first-consistent).
                _rl9 = _orc.get('remaining_links')
                # `remaining` counts links that no copper can ever join
                # (cross-board) as well, so gate custody on the FILTERED
                # detail when we have it -- otherwise a multi-outline board
                # pays a doomed reconcile (with rip authority) every run.
                # remaining_links is None ONLY when the link detail was never
                # obtained (source failure / cancel), where `remaining` may
                # be a stale count: fall back to all zone nets there, but
                # never on an empty list, which honestly means "none left".
                _has_custody = (_rl9 is None and _orc.get('remaining', 0) > 0) \
                    or bool(_rl9)
                if _orc.get('available') and _has_custody:
                    # Custody scope = exactly the nets with remaining links
                    # (ux pf7: the 6 stubborn links were ALL GND; putting
                    # every zone net in custody re-touched gate-connected
                    # +3V3 and broke it).
                    _custody_nets9 = (sorted({l[0] for l in _rl9})
                                      if _rl9 else list(_zna))
                    # Zone nets KiCad verified COMPLETE are hands-off for
                    # the reconcile: the main run's failure buckets are
                    # STALE after the finalize fixes nets.
                    _zone_complete9 = set(_zna) - set(_custody_nets9)
                    print(f"  Plane finalize: {_orc['remaining']} oracle "
                          f"link(s) unroutable without rip authority on "
                          f"{', '.join(_custody_nets9)} -- joining the "
                          f"final reconciliation")
                else:
                    # Oracle verdict: every zone net complete -- none of
                    # them may be re-touched by the stale failure buckets.
                    if _orc.get('available'):
                        _zone_complete9 = set(_zna)
        except Exception as _e:
            # The failure must name its blast radius: JSON_SUMMARY was
            # already printed BEFORE the finalize, so a grader reading it
            # sees the pre-finalize tally -- nets the raster model
            # over-credits (castor class) ship open with no failure record
            # unless the operator sees THIS line. (Review finding F3.)
            _unverified9 = sorted({n for n, _l in (locals().get('_zpairs_all')
                                                   or [])})
            print(f"{RED}  plane finalize pass FAILED: {_e}\n"
                  f"  Zone net(s) left UNVERIFIED by the kicad-oracle: "
                  f"{', '.join(_unverified9) if _unverified9 else '(none in scope)'} "
                  f"-- the printed JSON_SUMMARY predates the finalize and "
                  f"may over-credit these nets; re-verify with "
                  f"check_connected/check_drc before trusting this board."
                  f"{RESET}")
        finally:
            _finalize_depth(-1)
            # Drop the GUI oracle's staging files (locals() guard: the names
            # only exist once the finalize body got that far).
            for _p9 in (locals().get('_orc_tmp9') or []):
                try:
                    os.unlink(_p9)
                except OSError:
                    pass

    if (final_reconcile and not skip_routing and not _ckpt_stop
            and (output_file or return_results)
            and (failed_single or failed_multipoint or _custody_nets9)):
        # #562 order swap: stubborn-oracle-link plane nets (custody) merge
        # into THIS sub-run instead of a second self-invocation -- one
        # parse/base-build serves both, and signal retries now run against
        # the welded planes.
        _rec_names = list(dict.fromkeys(
            n for n in (failed_single
                        + [m['net_name'] for m in failed_multipoint]
                        + _custody_nets9)
            if n not in _zone_complete9))
        print(f"\nFinal reconciliation: retrying {len(_rec_names)} "
              f"incomplete/custody net(s) against the finished board: "
              f"{', '.join(_rec_names)}")
        try:
            _rk = dict(_reconcile_kwargs)
            # force_reroute must NOT forward: the strip already happened in
            # THIS run; a forwarded flag would re-strip the retried nets'
            # partial copper (and thrash on a second failure).
            _rk.update(final_reconcile=False, skip_routing=False,
                       force_reroute=False)
            # #527 follow-up: the inner run forwards the SAME progress
            # callback, so its routing/rescue/cleanup messages were pixel-
            # identical to the first pass's and the GUI looked like it ran
            # the whole process twice. Prefix them.
            _outer_pcb = _rk.get('progress_callback')
            if _outer_pcb:
                _rk['progress_callback'] = (
                    lambda c, t, m, _o=_outer_pcb: _o(
                        c, t, f"Final reconcile: {m}"))
            # Rip-authority escalation (#103 self-applied): nets that died with
            # 'no rippable blockers found' were boxed by PRE-EXISTING copper
            # this run may not touch, and the router itself printed the
            # --rip-existing-nets retry it wanted. Take that advice in-run:
            # grant the reconciliation rip authority over exactly the
            # frontier-attributed blocker names recorded in the failed nets'
            # histories (capped; rip authority is permission, not compulsion
            # -- the rip-up ladder only fires where a route is actually
            # blocked, and ripped pre-existing nets go through the standard
            # reroute-or-restore custody). Existing patterns are kept.
            _rec_ids = {nid for nid, net in pcb_data.nets.items()
                        if net.name in set(_rec_names)}
            _hinted = []
            for _nid in _rec_ids:
                for _ev in (state.net_history.get(_nid) or []):
                    if _ev.get('event') == 'preexisting_blockers':
                        for _bn in (_ev.get('details') or {}).get('blockers') or []:
                            if _bn not in _hinted:
                                _hinted.append(_bn)
            _RIP_ESCALATION_CAP = 12
            # Auto-granted rip authority must respect the caller's own net
            # filter: a net excluded by pattern ('!GND' while planes route
            # in a later step) is excluded BY PLAN, and ripping its stubs
            # here reroutes the whole net as track copper in a step that was
            # told not to touch it (ottercast: 52 dogbone stubs became a
            # 757-segment GND web). Explicit --rip-existing-nets from the
            # operator is honored as given; only the escalation filters.
            if _hinted and net_names:
                from net_queries import matches_net_filter as _mnf
                _hinted = [_bn for _bn in _hinted if _mnf(_bn, net_names)]
            if _hinted and '*' not in (rip_existing_nets or []):
                _hinted = _hinted[:_RIP_ESCALATION_CAP]
                _rk['rip_existing_nets'] = list(dict.fromkeys(
                    (rip_existing_nets or []) + _hinted))
                print(f"  Reconciliation rip authority (#103): "
                      f"--rip-existing-nets over hinted blockers "
                      f"{', '.join(_hinted)}")
            # #513 item 5: the reconciliation re-routes each incomplete net at
            # the forwarded kwargs' width resolution -- which silently dropped
            # a ripped power net's 1.5mm width to the 0.25 default when this
            # invocation carried no --power-nets (nascom VCC). Preserve each
            # reconciled net's ORIGINAL dominant width (from the pre-routing
            # input snapshot) via the power-width channel; an explicit
            # --power-nets entry for the net still wins.
            from routing_common import dominant_net_widths as _dnw5
            _orig_w5 = _dnw5(_s for _lst in _orig_seg_by_net.values()
                             for _s in _lst)
            _pn5 = list(_rk.get('power_nets') or [])
            _pw5 = list(_rk.get('power_nets_widths') or [])
            if len(_pn5) == len(_pw5):
                from net_queries import matches_net_filter as _mnf5
                _tw5 = _rk.get('track_width') or config.track_width
                for _nid5 in _rec_ids:
                    _w5 = _orig_w5.get(_nid5, 0.0)
                    _nm5 = (pcb_data.nets[_nid5].name
                            if _nid5 in pcb_data.nets else None)
                    if not _nm5 or _w5 <= (_tw5 or 0.0) + 1e-6:
                        continue
                    if _pn5 and _mnf5(_nm5, _pn5):
                        continue  # explicit power width wins
                    _pn5.append(_nm5)
                    _pw5.append(_w5)
                    print(f"  Preserving routed width {_w5}mm for reconciled "
                          f"net {_nm5} (original copper was wider than this "
                          f"run's {_tw5}mm default)")
                if _pn5:
                    _rk['power_nets'] = _pn5
                    _rk['power_nets_widths'] = _pw5
            if return_results:
                # GUI-parity reconciliation (gap-closure): re-invoke against
                # the SAME in-memory board (the copper this run just
                # committed lives in pcb_data, not in any file) and merge
                # the sub-run's results into ours. An inner strip that
                # targets copper THIS run emitted must instead drop it from
                # our write-lists (the GUI applier REMOVES FIRST and adds
                # after -- swig_gui runs the remove channels before the add
                # loop -- so a strip of a not-yet-added segment would no-op
                # against the live board and the deleted copper would ship).
                #
                # SNAP FIRST. The CLI branch below reconciles against the
                # WRITTEN file, whose coordinates are 100% on KiCad's integer-nm
                # grid because the writer quantises on the way out. The router
                # works in float mm, so this in-memory board is NOT: ~20% of its
                # coordinates sit up to 0.49 nm off-grid (eth_tap step 11:
                # 2813/13692). Reconciling against an un-snapped board means the
                # two fronts retry against DIFFERENT boards, so `return_results`
                # -- which should only decide whether results are RETURNED --
                # changed what got ROUTED: 3423 (CLI) vs 3428 (GUI) segments on
                # identical input and identical kwargs. Snapping here makes this
                # board bit-identical to the file the CLI would have re-parsed.
                # Geometrically a no-op (max deviation 0.4878 nm < the 0.5 nm
                # rounding threshold, so every value lands on the same integer
                # nm the writer emits).
                from kicad_parser import snap_pcb_data_to_iu_grid
                _snapped = snap_pcb_data_to_iu_grid(pcb_data)
                if _snapped:
                    print(f"  Reconciliation: snapped {_snapped} in-memory "
                          f"coordinate(s) onto the nm grid (CLI-file parity).")
                _rk.update(return_results=True, pcb_data=pcb_data)
                _rok, _rfail, _rt, _rdata = batch_route(
                    input_file, "", _rec_names, **_rk)
                _our_new_segs = set()
                for _r in results_data.get('results', []):
                    for _s in (_r.get('new_segments') or []):
                        _our_new_segs.add(id(_s))
                _inner_strips = _rdata.get('segments_to_remove') or []
                _strip_ours = {id(_s) for _s in _inner_strips
                               if id(_s) in _our_new_segs}
                if _strip_ours:
                    for _r in results_data.get('results', []):
                        _r['new_segments'] = [
                            _s for _s in (_r.get('new_segments') or [])
                            if id(_s) not in _strip_ours]
                results_data.setdefault('results', []).extend(
                    _rdata.get('results', []))
                results_data.setdefault('segments_to_remove', []).extend(
                    _s for _s in _inner_strips if id(_s) not in _strip_ours)
                # #484 H2: mirror the segment "ours" de-dup for vias -- an
                # inner strip naming a via THIS run emitted must drop it from
                # our write-lists, not ride vias_to_remove (the GUI applier
                # removes before adding, so a positional collision could
                # leave the emitted via shipped un-removed).
                _our_new_vias = set()
                for _r in results_data.get('results', []):
                    for _v in (_r.get('new_vias') or []):
                        _our_new_vias.add(id(_v))
                _inner_vstrips = _rdata.get('vias_to_remove') or []
                _vstrip_ours = {id(_v) for _v in _inner_vstrips
                                if id(_v) in _our_new_vias}
                if _vstrip_ours:
                    for _r in results_data.get('results', []):
                        _r['new_vias'] = [
                            _v for _v in (_r.get('new_vias') or [])
                            if id(_v) not in _vstrip_ours]
                if _inner_vstrips:
                    results_data.setdefault('vias_to_remove', []).extend(
                        _v for _v in _inner_vstrips
                        if id(_v) not in _vstrip_ours)
                # #484 H2: pad_swaps / single_ended_target_swap_info were
                # silently dropped -- a target/polarity swap performed by the
                # reconciliation sub-run then never reached the GUI applier,
                # and the reconciled net's stub pointed at the OLD net.
                for _key in ('all_swap_vias', 'all_swap_segments',
                             'all_segment_modifications', 'pad_swaps',
                             'single_ended_target_swap_info',
                             'exclusion_zone_lines', 'boundary_debug_labels'):
                    if _rdata.get(_key):
                        results_data.setdefault(_key, []).extend(_rdata[_key])
                # SNAP AGAIN. The sub-run above routed in float mm too, so the
                # copper it just merged is back off-grid (8 coordinates on
                # eth_tap step 11) even though we snapped before invoking it.
                # The CLI's equivalent copper goes through the writer and lands
                # grid-exact, so without this the fork reopens on exactly the
                # nets the reconcile pass touched. Snap pcb_data AND the
                # results the GUI applier will read -- they are usually the
                # same Segment objects, but not contractually.
                snap_pcb_data_to_iu_grid(pcb_data)
                for _r in results_data.get('results', []):
                    _rsnap = type('_P', (), {})()
                    _rsnap.segments = _r.get('new_segments') or []
                    _rsnap.vias = _r.get('new_vias') or []
                    snap_pcb_data_to_iu_grid(_rsnap)
            else:
                _rk.update(return_results=False)
                _rok, _rfail, _rt = batch_route(
                    output_file, output_file, _rec_names, **_rk)
            print("Note: the JSON_SUMMARY above covers only the "
                  "reconciliation subset; the run's full tally is the "
                  "earlier JSON_SUMMARY plus these recoveries.")
            if _rok:
                successful += _rok
                failed = max(0, failed - _rok)
        except Exception as _e:
            print(f"{RED}  final reconciliation pass failed: {_e}{RESET}")



    # Per-net story dump (KICAD_NET_STORY=1): the complete journey of every
    # net -- bus membership, ordering, failures with named blockers, rips,
    # rescues, Phase-3 tap order, costs -- assembled from state.
    from net_story import net_story_enabled, dump_net_story
    if net_story_enabled():
        try:
            dump_net_story(state, output_file or input_file)
        except Exception as _e:
            print(f"  net story dump failed: {_e}")

    # Route trace dump (KICAD_ROUTE_TRACE=1): the per-copper add/rip/restore
    # timeline recorded at the choke points, for animate_route.py (#482).
    from route_trace import dump_trace as _dump_route_trace
    _dump_route_trace(state.pcb_data, output_file)

    # KICAD_DUP_TRAP=1: final verdict. Says whether the traps SURVIVED the
    # run -- a rebind swaps in a plain list and disarms them, so a bare
    # "0 hits" is only meaningful once this confirms they were still armed.
    from dup_trap import verify as _verify_dup_trap
    _verify_dup_trap(pcb_data)

    if return_results:
        return successful, failed, total_time, results_data
    return successful, failed, total_time

if __name__ == "__main__":
    import argparse
    # Windows consoles default to cp1252, which can't encode the non-ASCII glyphs
    # some log lines use (arrows in bus order, Ohm in impedance, the fab-floor
    # warning sign); reconfigure stdout/stderr to UTF-8 so a print never crashes
    # the run (issue #152).
    from console_encoding import enable_utf8_console
    enable_utf8_console()
    from redo_record import record_invocation
    record_invocation()  # stress-test redo manifest (#132); no-op unless REDO_MANIFEST set

    parser = argparse.ArgumentParser(
        description="Batch PCB Router - Routes single-ended nets using Rust-accelerated A*. For differential pairs, use route_diff.py.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Wildcard patterns supported:
  "Net-(U2A-DATA_*)"  - matches Net-(U2A-DATA_0), Net-(U2A-DATA_1), etc.
  "Net-(*CLK*)"       - matches any net containing CLK

Examples:
  python route.py fanout_starting_point.kicad_pcb routed.kicad_pcb "Net-(U2A-DATA_*)"
  python route.py input.kicad_pcb output.kicad_pcb "Net-(U2A-DATA_*)" --ordering mps

For differential pair routing, use route_diff.py:
  python route_diff.py input.kicad_pcb output.kicad_pcb --nets "*lvds*"
"""
    )
    parser.add_argument("input_file", help="Input KiCad PCB file")
    parser.add_argument("output_file", nargs="?", help="Output KiCad PCB file (default: input_routed.kicad_pcb)")
    parser.add_argument("--output", metavar="FILE",
                        help="Output KiCad PCB file (named alias for the positional output_file)")
    parser.add_argument("net_patterns", nargs="*", help="Net names or wildcard patterns to route (default: '*' = all nets)")
    parser.add_argument("--nets", "-n", nargs="+", help="Net names or wildcard patterns to route (alternative to positional args)")
    parser.add_argument("--overwrite", "-O", action="store_true",
                        help="Overwrite input file instead of creating _routed copy")
    parser.add_argument("--component", "-C", help="Route all nets connected to this component (e.g., U1). Excludes GND/VCC/VDD unless net patterns also specified.")
    # Ordering and strategy options
    parser.add_argument("--ordering", "-o", choices=["inside_out", "mps", "original", "bus"],
                        default=defaults.DEFAULT_ORDERING_STRATEGY,
                        help="Net ordering strategy: mps (default, crossing conflicts), "
                             "inside_out, original, or bus (detected bus groups first, "
                             "members middle-out, rest by mps; ordering only -- "
                             "corridor attraction still needs --bus)")
    parser.add_argument("--direction", "-d", choices=["forward", "backward"],
                        default=None,
                        help="Direction search order for each net route")
    # #381 D9: accept the singular --no-bga-zone spelling too (the plane/fanout
    # scripts spell it singular); same nargs='*' dest -- additive, plural kept.
    parser.add_argument("--no-bga-zones", "--no-bga-zone", nargs="*", default=None,
                        help="Disable BGA exclusion zones. No args = disable all. With component refs (e.g., U1 U3) = disable only those.")
    parser.add_argument("--rip-existing-nets", nargs="+", default=None,
                        metavar="PATTERN",
                        help="Net name patterns of PRE-EXISTING routed nets that may be "
                             "ripped up and re-routed when they block a net being routed "
                             "(e.g. on a board routed by a previous run). Use '*' to allow "
                             "any non-plane net. Without this flag, committed tracks are "
                             "never ripped.")
    parser.add_argument("--force-reroute", action="store_true",
                        help="Rip and re-route from scratch every net selected by "
                             "--nets, even if already fully connected (#515's "
                             "manual recipe, automated; PR #533). Requires an "
                             "explicit --nets scope. Protected nets (length-matched "
                             "groups, routed diff pairs) are skipped unless named "
                             "EXACTLY in --nets; KiCad-locked copper is never "
                             "ripped; plane (zone-owning) nets are skipped -- use "
                             "route_planes for those. If the re-route fails "
                             "outright, the original copper is restored.")
    parser.add_argument("--layers", "-l", nargs="+",
                        default=None,
                        help="Routing layers to use (default: all of the board's "
                             "copper layers)")

    # Track and via geometry
    parser.add_argument("--track-width", type=float, default=None,
                        help="Track width in mm. Default: the board's Default net-class "
                             f"track_width (sibling .kicad_pro), else {defaults.TRACK_WIDTH}. "
                             "Ignored if --impedance is specified.")
    parser.add_argument("--impedance", type=float, default=None,
                        help="Target single-ended impedance in ohms (e.g., 50). Calculates track width per layer from board stackup.")
    parser.add_argument("--coplanar-gap", type=float, default=0.0,
                        help="Declare that impedance-controlled traces run through a "
                             "ground pour on their OWN layer, this far (mm, trace edge "
                             "to pour edge) from it. Outer layers then use the "
                             "coplanar-waveguide-over-ground model instead of microstrip, "
                             "which gives a NARROWER trace for the same target ohms. "
                             "The pour does not exist yet at route time, so this is a "
                             "DECLARATION: pour the plane layers with a matching "
                             "'route_planes --zone-clearance', then verify with "
                             "'check_impedance.py --coplanar-gap'. Requires --impedance.")
    parser.add_argument("--coplanar-nets", nargs="+", default=None, metavar="PATTERN",
                        help="Limit --coplanar-gap to these nets (fnmatch patterns). "
                             "Omitted: every net in this call is treated as coplanar. "
                             "Given: only matching nets get CPW widths; the rest stay "
                             "microstrip.")
    parser.add_argument("--clearance", type=float, default=None,
                        help="Copper clearance CEILING in mm. When given, every net class "
                             "(Default included) is capped at min(class, this). When OMITTED, "
                             "each net routes at its own net-class clearance (base = the board's "
                             f"Default class from the sibling .kicad_pro, else {defaults.CLEARANCE}). "
                             "Use --net-clearances <json> for explicit per-net values.")
    parser.add_argument("--via-size", type=float, default=None,
                        help="Via outer diameter in mm. Default: the board's Default net-class "
                             f"via_diameter (sibling .kicad_pro), else {defaults.VIA_SIZE}.")
    parser.add_argument("--via-drill", type=float, default=None,
                        help="Via drill size in mm. Default: the board's Default net-class "
                             f"via_drill (sibling .kicad_pro), else {defaults.VIA_DRILL}.")
    parser.add_argument("--net-clearances", metavar="JSON", default=None,
                        help="Explicit override for the cross-class clearance map: a JSON object "
                             "mapping net name -> that net's net-class clearance in mm. When OMITTED, "
                             "the map is AUTO-READ from the sibling .kicad_pro's non-Default "
                             "netclasses (all-Default boards -> empty -> inert). Every pre-placed AND "
                             "in-run via/pad/segment obstacle of a different class is priced at "
                             "max(this call's routing floor, that obstacle net's own clearance) so a "
                             "foreign higher-clearance net (POWER_HI 0.25 while routing a Default "
                             "0.15 group) is not under-blocked (cross-class via-via/DRC). The GUI "
                             "derives the same map from the board's live net classes.")

    # Power net routing options
    parser.add_argument("--power-nets", nargs="*", default=[],
                        help="Glob patterns for power nets (e.g., '*GND*' '*VCC*'). Must pair with --power-nets-widths.")
    parser.add_argument("--power-nets-widths", nargs="*", type=float, default=[],
                        help="Track widths in mm for each power-net pattern (must match --power-nets length)")
    parser.add_argument("--no-power-tap-neckdown", action="store_true",
                        help="Disable neck-down retry of failed power-net tap edges (issue #72): by default a "
                             "wide tap that cannot fit is re-routed at the layer's default width near the pad")
    parser.add_argument("--neckdown-length", type=float, default=defaults.NECKDOWN_LENGTH,
                        help="Length in mm of narrow track from the target pad on neck-down tap routes; the track "
                             "returns to the power width beyond this where clearance allows (default: 2.5)")
    parser.add_argument("--neckdown-taper-length", type=float, default=defaults.NECKDOWN_TAPER_LENGTH,
                        help="Length in mm of the stepped narrow-to-wide width taper on neck-down tap routes "
                             "(0 = abrupt width change, default: 0.5)")

    # Router algorithm parameters
    parser.add_argument("--grid-step", type=float, default=defaults.GRID_STEP,
                        help=f"Grid resolution in mm (default: {defaults.GRID_STEP})")
    parser.add_argument("--via-cost", type=int, default=defaults.VIA_COST,
                        help=f"Penalty for placing a via, in 0.1mm grid steps (default: {defaults.VIA_COST} = 5mm of path; mm-equivalent at any --grid-step)")
    parser.add_argument("--via-proximity-cost", type=int, default=defaults.VIA_PROXIMITY_COST,
                        help=f"Via cost multiplier in stub/BGA proximity zones (default: {defaults.VIA_PROXIMITY_COST}, 0=block vias)")
    parser.add_argument("--max-iterations", type=int, default=defaults.MAX_ITERATIONS,
                        help=f"Max A* iterations before giving up (default: {defaults.MAX_ITERATIONS})")
    parser.add_argument("--max-probe-iterations", type=int, default=5000,
                        help="Max iterations for quick probe phase per direction (default: 5000)")
    parser.add_argument("--heuristic-weight", type=float, default=defaults.HEURISTIC_WEIGHT,
                        help=f"A* heuristic weight, higher=faster but less optimal (default: {defaults.HEURISTIC_WEIGHT})")
    parser.add_argument("--turn-cost", type=int, default=defaults.TURN_COST,
                        help=f"Penalty for direction changes, encourages straighter paths (default: {defaults.TURN_COST})")
    parser.add_argument("--direction-preference-cost", type=int, default=defaults.DIRECTION_PREFERENCE_COST,
                        help=f"Penalty for non-preferred layer direction, 0=disabled (default: {defaults.DIRECTION_PREFERENCE_COST})")
    parser.add_argument("--bus", action="store_true",
                        help="Enable auto-detection and routing of bus groups (nets with clustered endpoints)")
    parser.add_argument("--bus-detection-radius", type=float, default=defaults.BUS_DETECTION_RADIUS,
                        help=f"Max endpoint distance to form bus in mm (default: {defaults.BUS_DETECTION_RADIUS})")
    parser.add_argument("--bus-attraction-radius", type=float, default=defaults.BUS_ATTRACTION_RADIUS,
                        help=f"Attraction radius from neighbor track in mm (default: {defaults.BUS_ATTRACTION_RADIUS})")
    parser.add_argument("--bus-attraction-bonus", type=int, default=defaults.BUS_ATTRACTION_BONUS,
                        help=f"Cost bonus for staying near neighbor track (default: {defaults.BUS_ATTRACTION_BONUS})")
    parser.add_argument("--bus-min-nets", type=int, default=defaults.BUS_MIN_NETS,
                        help=f"Minimum nets to form a bus group (default: {defaults.BUS_MIN_NETS})")
    parser.add_argument("--guide-corridor", action="store_true",
                        help="Steer routed nets along a guide polyline drawn on a User layer (issue #7)")
    parser.add_argument("--guide-corridor-layer", type=str, default=defaults.GUIDE_CORRIDOR_LAYER,
                        help=f"User layer the guide polyline is drawn on (default: {defaults.GUIDE_CORRIDOR_LAYER})")
    parser.add_argument("--guide-corridor-spacing", type=float, default=defaults.GUIDE_CORRIDOR_SPACING,
                        help=f"Max mm between waypoints; 0 = use only the drawn segment endpoints, "
                             f">0 subdivides long segments to follow curves more tightly (default: {defaults.GUIDE_CORRIDOR_SPACING})")
    parser.add_argument("--keepout", action="store_true",
                        help="Keep routed tracks out of one or more polygons drawn on a User layer (issue #27)")
    parser.add_argument("--keepout-layer", type=str, default=defaults.KEEPOUT_LAYER,
                        help=f"User layer the keepout polygons are drawn on (default: {defaults.KEEPOUT_LAYER})")
    parser.add_argument("--proximity-heuristic-factor", type=float, default=0.02,
                        help="Factor for proximity heuristic estimation (default: 0.02, higher=faster but may find suboptimal paths)")

    # Stub proximity penalty
    parser.add_argument("--stub-proximity-radius", type=float, default=defaults.STUB_PROXIMITY_RADIUS,
                        help=f"Radius around stubs to penalize routing in mm (default: {defaults.STUB_PROXIMITY_RADIUS})")
    parser.add_argument("--stub-proximity-cost", type=float, default=defaults.STUB_PROXIMITY_COST,
                        help=f"Cost penalty near stubs in mm equivalent (default: {defaults.STUB_PROXIMITY_COST})")

    # BGA proximity penalty
    parser.add_argument("--bga-proximity-radius", type=float, default=7.0,
                        help="Radius around BGA edges to penalize routing in mm (default: 7.0)")
    parser.add_argument("--bga-proximity-cost", type=float, default=0.2,
                        help="Cost penalty near BGA edges in mm equivalent (default: 0.2)")

    # Track proximity penalty (same layer only)
    parser.add_argument("--track-proximity-distance", type=float, default=defaults.TRACK_PROXIMITY_DISTANCE,
                        help=f"Radius around routed tracks in mm, same layer only (0 = disabled, default: {defaults.TRACK_PROXIMITY_DISTANCE})")
    parser.add_argument("--track-proximity-cost", type=float, default=defaults.TRACK_PROXIMITY_COST,
                        help=f"Cost penalty near routed tracks (0 = disabled, default: {defaults.TRACK_PROXIMITY_COST})")

    # Layer swap and target swap options
    parser.add_argument("--no-stub-layer-swap", action="store_true",
                        help="Disable stub layer switching optimization (enabled by default)")
    parser.add_argument("--no-crossing-layer-check", action="store_true",
                        help="Count crossings regardless of layer overlap (by default, only same-layer crossings count)")
    parser.add_argument("--can-swap-to-top-layer", action="store_true",
                        help="Allow swapping stubs to F.Cu (top layer). Off by default due to via clearance issues.")
    parser.add_argument("--swappable-nets", nargs="+",
                        help="Glob patterns for nets that can have targets swapped (e.g., '*DATA_*')")
    parser.add_argument("--schematic-dir", default=None,
                        help="Directory containing .kicad_sch files to update with pad swaps (default: no schematic update)")
    parser.add_argument("--crossing-penalty", type=float, default=1000.0,
                        help="Penalty for crossing assignments in target swap optimization (default: 1000.0)")
    parser.add_argument("--mps-reverse-rounds", action="store_true",
                        help="Reverse MPS round order: route most-conflicting groups first instead of least-conflicting")
    parser.add_argument("--mps-layer-swap", action="store_true",
                        help="Enable MPS-aware layer swaps to reduce crossing conflicts")
    parser.add_argument("--mps-segment-intersection", action="store_true",
                        help="Force MPS to use segment intersection for crossing detection (auto-enabled when no BGA chips)")
    parser.add_argument("--keep-input-copper", action="store_true",
                        help="Treat the input file's own copper as read-only: the post-route "
                             "cleanup passes (dead-end sweep, orphan islands, cycle/redundancy "
                             "prunes, graze re-bends) never remove or rewrite it, only this "
                             "run's new copper. For chained flows whose earlier stages author "
                             "copper (fanout escape stubs, hand-routed nets) that later stages "
                             "or checks must still see verbatim - including stubs of nets this "
                             "run FAILED to route. Default: off (issue #84 semantics: dead "
                             "input stubs on in-scope nets are swept).")

    # Length matching options
    parser.add_argument("--length-match-group", action="append", nargs="+", dest="length_match_groups",
                        help="Net patterns to length-match as a group (can be repeated). Use 'auto' for DDR4 auto-grouping")
    parser.add_argument("--length-match-tolerance", type=float, default=0.1,
                        help="Acceptable length variance within group in mm (default: 0.1)")
    parser.add_argument("--meander-amplitude", type=float, default=1.0,
                        help="Height of meander perpendicular to trace in mm (default: 1.0)")
    parser.add_argument("--meander-spacing", type=float, default=defaults.MEANDER_SPACING,
                        help="Centre-to-centre spacing of adjacent meander arms, in multiples of "
                             "the net's routed track width (default: 2.0 = 2W)")

    # Time matching options (alternative to length matching)
    parser.add_argument("--time-matching", action="store_true",
                        help="Match by propagation time instead of length (accounts for layer dielectric)")
    parser.add_argument("--time-match-tolerance", type=float, default=1.0,
                        help="Acceptable time variance in picoseconds (default: 1.0)")

    # Rip-up and retry options
    parser.add_argument("--max-ripup", type=int, default=defaults.MAX_RIPUP,
                        help=f"Maximum blockers to rip up at once during rip-up and retry (default: {defaults.MAX_RIPUP})")
    parser.add_argument("--ripup-abandon-metric",
                        choices=list(defaults.RIPUP_ABANDON_METRIC_CHOICES),
                        default=os.environ.get('KICAD_RIPUP_ABANDON_METRIC',
                                               defaults.RIPUP_ABANDON_METRIC),
                        help="How a Phase 3 tap rip-up decides keep-retry vs abandon "
                             "(see docs/rip-up-reroute.md). Env override: "
                             f"KICAD_RIPUP_ABANDON_METRIC (default: {defaults.RIPUP_ABANDON_METRIC})")
    parser.add_argument("--ripup-blocker-select",
                        choices=list(defaults.RIPUP_BLOCKER_SELECT_CHOICES),
                        default=os.environ.get('KICAD_RIPUP_BLOCKER_SELECT',
                                               defaults.RIPUP_BLOCKER_SELECT),
                        help="Blocker SELECTION algorithm for the rip-up ladder. "
                             "'count' = historical weighted cell "
                             "count; 'near-target' = endpoint-proximity first "
                             "(the true last-mile blocker hugs the failing pad "
                             "but has few cells); 'bidir' = boost nets blocking "
                             "BOTH search directions (genuine separating walls); "
                             "'mincut' = soft-cost probe on a map clone that "
                             "reads the actual crossing set (names the true "
                             "joint cut; falls back to count order when the "
                             "wall is static copper). Default: count.")
    parser.add_argument("--routing-clearance-margin", type=float, default=defaults.ROUTING_CLEARANCE_MARGIN,
                        help=f"Multiplier on track-via clearance ({defaults.ROUTING_CLEARANCE_MARGIN} = minimum DRC)")
    parser.add_argument("--hole-to-hole-clearance", type=float, default=None,
                        help="Minimum clearance between drill holes in mm. Default: the "
                             f"board's own min_hole_to_hole constraint, else {defaults.HOLE_TO_HOLE_CLEARANCE}.")
    parser.add_argument("--board-edge-clearance", type=float, default=None,
                        help="Clearance from board edge in mm. Default: the board's own "
                             f"min_copper_edge_clearance constraint, else {defaults.BOARD_EDGE_CLEARANCE}.")
    from fix_kicad_drc_settings import add_drc_fix_args
    add_drc_fix_args(parser)

    # Vertical alignment attraction options
    parser.add_argument("--vertical-attraction-radius", type=float, default=defaults.VERTICAL_ATTRACTION_RADIUS,
                        help="Radius in mm for cross-layer track attraction (0 = disabled, default: 1.0)")
    parser.add_argument("--vertical-attraction-cost", type=float, default=defaults.VERTICAL_ATTRACTION_COST,
                        help=f"Cost bonus for aligning with tracks on other layers (0 = disabled, default: {defaults.VERTICAL_ATTRACTION_COST})")

    # Ripped route avoidance options
    parser.add_argument("--ripped-route-avoidance-radius", type=float, default=defaults.RIPPED_ROUTE_AVOIDANCE_RADIUS,
                        help=f"Radius in mm around ripped route segments/vias for soft penalty (default: {defaults.RIPPED_ROUTE_AVOIDANCE_RADIUS})")
    parser.add_argument("--ripped-route-avoidance-cost", type=float, default=defaults.RIPPED_ROUTE_AVOIDANCE_COST,
                        help=f"Soft penalty cost for routing through ripped corridors (0 = disabled, default: {defaults.RIPPED_ROUTE_AVOIDANCE_COST})")

    # Layer preference options
    parser.add_argument("--layer-costs", nargs="+", type=float, default=[],
                        help="Per-layer cost multipliers (1.0-1000, default: F.Cu=1.0, others=3.0), "
                             "or any negative value (e.g. -1) = FORBIDDEN: the layer stays an obstacle "
                             "(its copper blocks vias) and through-vias may span it, but NO track copper "
                             "is placed on it. Order matches --layers. Example (route only F.Cu/B.Cu, "
                             "In1/In2 obstacle-only): --layers F.Cu In1.Cu In2.Cu B.Cu --layer-costs 1.0 -1 -1 3.0")

    # Debug options
    parser.add_argument("--debug-lines", action="store_true",
                        help="Output debug geometry on User.3 (connectors), User.4 (stub dirs), User.8 (simplified), User.9 (raw A*)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print detailed diagnostic output (setback checks, etc.)")
    parser.add_argument("--skip-routing", action="store_true",
                        help="Skip actual routing, only do swaps and write debug info")
    parser.add_argument("--debug-memory", action="store_true",
                        help="Print memory usage statistics at key points during routing")
    parser.add_argument("--add-teardrops", action="store_true",
                        help="Add teardrop settings to all pads and vias in output file")
    parser.add_argument("--stats", action="store_true",
                        help="Collect and print A* search statistics for debugging heuristic efficiency")

    from fab_tiers import (add_fab_tier_args, fab_tier_from_args, set_default_fab_tier,
                           enforce_fab_floors, count_copper_layers_in_file)
    add_fab_tier_args(parser)
    args = parser.parse_args()
    # #439: the PRESENCE of --clearance is the clamp switch. Given -> it is the
    # ceiling: non-Default classes are capped at min(class, --clearance) and the
    # output .kicad_pro clamps them to the routed floor. Omitted -> honor the
    # classes: the base clearance defaults to the board's OWN Default net-class
    # clearance, each non-Default net routes at its full class value (uncapped), and
    # the writeback preserves the classes. --hole-to-hole-clearance and
    # --board-edge-clearance work the same way (omitted -> the board's own
    # constraint minimum). Resolved here, before enforce_fab_floors and every
    # downstream use. Stashed on args for drc_fix_kwargs (the writeback clamp).
    from list_nets import (board_default_netclass_clearance, board_default_netclass_param,
                           board_constraint)
    # #435 companion: whether --track-width was EXPLICITLY set. If NOT, each net
    # routes at its OWN netclass track width engine-side (not the single board
    # Default-class width). --impedance derives width per layer, so it counts as
    # explicit here. Captured before the fill below overwrites the None.
    _tw_explicit = (args.track_width is not None) or (getattr(args, 'impedance', None) is not None)
    # track_width / via_size / via_drill: when omitted, default to the board's OWN
    # Default net-class value (else the routing_defaults constant), so a bare route
    # uses the board's own geometry -- parity with the GUI's per-control override.
    for _pname, _nckey, _fallback in (('track_width', 'track_width', defaults.TRACK_WIDTH),
                                      ('via_size', 'via_diameter', defaults.VIA_SIZE),
                                      ('via_drill', 'via_drill', defaults.VIA_DRILL)):
        if getattr(args, _pname) is None:
            _v = board_default_netclass_param(args.input_file, _nckey)
            setattr(args, _pname, _v if _v is not None else _fallback)
            print(f"--{_pname.replace('_', '-')} not given; using "
                  f"{'the board Default net-class' if _v is not None else 'the fallback'} "
                  f"{getattr(args, _pname)}mm.")
    # --clearance, when given, is a pure CEILING on EVERY class -- Default included,
    # nothing special. Each net routes at min(its class, ceiling): the base clearance
    # (Default-class nets) is min(Default class, ceiling), and non-Default classes are
    # capped at the ceiling in the map below. Omitted -> no ceiling: every net routes
    # at its own class (base = the board's Default class).
    _ceiling = args.clearance                       # None iff --clearance omitted
    args._clamp_netclasses = _ceiling is not None
    args._clearance_ceiling = _ceiling
    from fix_kicad_drc_settings import warn_if_missing_project_floor
    warn_if_missing_project_floor(args.input_file)  # #441: a dropped sibling .kicad_pro strands the DRC floor
    _dflt_clr = board_default_netclass_clearance(args.input_file)
    if _ceiling is None:
        args.clearance = _dflt_clr if _dflt_clr is not None else defaults.CLEARANCE
        print(f"--clearance not given; honoring net classes with base = "
              f"{'the board Default net-class' if _dflt_clr is not None else 'the fallback'} "
              f"clearance {args.clearance}mm.")
    else:
        # min(Default class, ceiling) so Default is capped like every other class.
        args.clearance = min(_dflt_clr, _ceiling) if _dflt_clr is not None else _ceiling
    if args.hole_to_hole_clearance is None:
        _h2h = board_constraint(args.input_file, 'min_hole_to_hole')
        args.hole_to_hole_clearance = _h2h if _h2h is not None else defaults.HOLE_TO_HOLE_CLEARANCE
        print(f"--hole-to-hole-clearance not given; using "
              f"{'the board min_hole_to_hole' if _h2h is not None else 'the fallback'} "
              f"{args.hole_to_hole_clearance}mm.")
    if args.board_edge_clearance is None:
        _edge = board_constraint(args.input_file, 'min_copper_edge_clearance')
        args.board_edge_clearance = _edge if _edge is not None else defaults.BOARD_EDGE_CLEARANCE
        print(f"--board-edge-clearance not given; using "
              f"{'the board min_copper_edge_clearance' if _edge is not None else 'the fallback'} "
              f"{args.board_edge_clearance}mm.")
    set_default_fab_tier(*fab_tier_from_args(args))
    _pinned_floors = enforce_fab_floors(
        count_copper_layers_in_file(args.input_file),
        track_width=getattr(args, 'track_width', None),
        clearance=getattr(args, 'clearance', None),
        via_size=getattr(args, 'via_size', None),
        via_drill=getattr(args, 'via_drill', None),
        hole_to_hole_clearance=getattr(args, 'hole_to_hole_clearance', None),
        board_edge_clearance=getattr(args, 'board_edge_clearance', None))
    # Below-floor params are pinned up to the fab floor (warned); apply the clamps.
    for _pname, _pfloor in _pinned_floors.items():
        setattr(args, _pname, _pfloor)

    # --output is a named alias for the positional output_file; reject giving both differently.
    if args.output is not None:
        if args.output_file is not None and args.output_file != args.output:
            parser.error("specify the output path once: positional output_file OR --output, not both")
        args.output_file = args.output

    # Handle output file: use --overwrite, explicit output, or auto-generate with _routed suffix
    if args.output_file is None:
        if args.overwrite:
            args.output_file = args.input_file
        else:
            # Auto-generate output filename: input.kicad_pcb -> input_routed.kicad_pcb
            base, ext = os.path.splitext(args.input_file)
            args.output_file = base + '_routed' + ext
            print(f"Output file: {args.output_file}")

    # Load PCB to expand wildcards
    print(f"Loading {args.input_file} to expand net patterns...")
    pcb_data = parse_kicad_pcb(args.input_file)

    # Default --layers to ALL of the board's copper layers (issue #98: the old
    # F.Cu/B.Cu default silently routed 4-layer boards as 2-layer). The layer
    # cost defaults keep the documented bias (2-layer: F=1.0/B=3.0; 4+: 1.0).
    if args.layers is None:
        copper = pcb_data.board_info.copper_layers
        args.layers = list(copper) if copper else list(defaults.DEFAULT_LAYERS)
        if len(args.layers) > 2:
            print(f"Using all {len(args.layers)} copper layers: "
                  f"{' '.join(args.layers)} (override with --layers)")

    # Combine positional net_patterns and --nets argument
    all_patterns = list(args.net_patterns) if args.net_patterns else []
    if args.nets:
        all_patterns.extend(args.nets)

    # --force-reroute rips every selected net; without an explicit scope the
    # default-'*' below would silently select the WHOLE BOARD for rip+reroute.
    # (batch_route can't tell the difference -- main() always passes expanded
    # names -- so the guard lives here.) An explicit --nets '*' is honored:
    # the operator said so, and #521 protection still shields matched groups
    # (glob is not an override).
    if args.force_reroute and not all_patterns and not args.component:
        parser.error("--force-reroute requires an explicit net scope "
                     "(--nets, positional patterns, or --component): it rips "
                     "and re-routes every selected net from scratch.")

    # Default to "*" (all nets) if no patterns and no component specified
    if not all_patterns and not args.component:
        all_patterns = ["*"]

    # Get nets from patterns and/or component
    if all_patterns:
        net_names = expand_net_patterns(pcb_data, all_patterns)
    else:
        net_names = []  # Will be populated by component filter below

    # Filter by component if specified
    if args.component:
        component_nets = set()
        for net_id, pads in pcb_data.pads_by_net.items():
            for pad in pads:
                if pad.component_ref == args.component:
                    net_info = pcb_data.nets.get(net_id)
                    if net_info and net_info.name:
                        component_nets.add(net_info.name)
                    break
        if all_patterns:
            # Intersect with pattern-matched nets
            net_names = [n for n in net_names if n in component_nets]
        else:
            # Use all component nets (excluding power/ground and unconnected pins)
            exclude_patterns = POWER_NET_EXCLUSION_PATTERNS
            filtered = []
            for name in component_nets:
                excluded = False
                for pattern in exclude_patterns:
                    if pattern and fnmatch.fnmatch(name.upper(), pattern.upper()):
                        excluded = True
                        break
                if not excluded:
                    filtered.append(name)
            net_names = sorted(filtered)
        print(f"Filtered to {len(net_names)} nets on component {args.component}")

    if not net_names:
        print("No nets matched the given patterns!")
        sys.exit(1)

    print(f"Routing {len(net_names)} nets: {net_names[:5]}{'...' if len(net_names) > 5 else ''}")

    # Cross-class clearance map: resolve {net name -> clearance} to {net_id -> clearance} against
    # this board's nets so the obstacle-map builders price each pre-placed obstacle at its own
    # net-class clearance (KiCad's max(classA, classB)). None/absent -> empty map -> prior
    # behaviour. The GUI front builds the same map from live net classes (swig_gui).
    # #439: ALWAYS build the non-Default netclass map. When --clearance was GIVEN
    # (args._clamp_netclasses) it is the CEILING -- each class is capped at
    # min(class, --clearance), because stock classes are often aspirational (the
    # human-routed zynq itself violates its 0.2 class, routed ~0.1). When --clearance
    # was OMITTED the classes are honored in full (each net routes at its own class),
    # and the writeback preserves them. --net-clearances <json> overrides with
    # explicit per-net values, used as-is.
    _net_clearances_map = None
    if args.net_clearances:
        with open(args.net_clearances, encoding="utf-8") as _f:
            _name_to_clr = json.load(_f)
        _net_clearances_map = {}
        for _nid, _net in pcb_data.nets.items():
            if _net.name in _name_to_clr:
                _net_clearances_map[_nid] = float(_name_to_clr[_net.name])
        print(f"Loaded per-net clearances for {len(_net_clearances_map)}/{len(pcb_data.nets)} nets "
              f"from {args.net_clearances}")
    else:
        from list_nets import net_clearance_map_by_id
        _net_clearances_map = net_clearance_map_by_id(
            args.input_file, {_nid: _net.name for _nid, _net in pcb_data.nets.items()})
        if _net_clearances_map and args._clamp_netclasses:
            _net_clearances_map = {nid: min(clr, args._clearance_ceiling)
                                   for nid, clr in _net_clearances_map.items()}
        if _net_clearances_map:
            _classes = sorted({round(v, 4) for v in _net_clearances_map.values()})
            _mode = (f"capped at --clearance {args._clearance_ceiling}"
                     if args._clamp_netclasses
                     else "honored in full (--clearance omitted)")
            print(f"Netclass clearances for {len(_net_clearances_map)} net(s), {_mode} "
                  f"(mm: {_classes}); cross-class max(A,B) respected.")

    batch_route(args.input_file, args.output_file, net_names,
                direction_order=args.direction,
                ordering_strategy=args.ordering,
                disable_bga_zones=args.no_bga_zones,
                rip_existing_nets=args.rip_existing_nets,
                force_reroute=args.force_reroute,
                # RAW patterns (pre-expansion): the #521 protection override
                # must see what the operator TYPED, not the expanded names.
                net_name_patterns=all_patterns,
                layers=args.layers,
                track_width=args.track_width,
                track_width_from_class=not _tw_explicit,
                impedance=args.impedance,
                coplanar_gap=args.coplanar_gap,
                coplanar_nets=args.coplanar_nets,
                power_nets=args.power_nets,
                power_nets_widths=args.power_nets_widths,
                power_tap_neckdown=not args.no_power_tap_neckdown,
                neckdown_length=args.neckdown_length,
                neckdown_taper_length=args.neckdown_taper_length,
                clearance=args.clearance,
                net_clearances=_net_clearances_map,
                keep_input_copper=args.keep_input_copper,
                via_size=args.via_size,
                via_drill=args.via_drill,
                grid_step=args.grid_step,
                via_cost=args.via_cost,
                max_iterations=args.max_iterations,
                max_probe_iterations=args.max_probe_iterations,
                heuristic_weight=args.heuristic_weight,
                turn_cost=args.turn_cost,
                direction_preference_cost=args.direction_preference_cost,
                bus_enabled=args.bus,
                bus_detection_radius=args.bus_detection_radius,
                bus_attraction_radius=args.bus_attraction_radius,
                bus_attraction_bonus=args.bus_attraction_bonus,
                bus_min_nets=args.bus_min_nets,
                guide_corridor_enabled=args.guide_corridor,
                guide_corridor_layer=args.guide_corridor_layer,
                guide_corridor_spacing=args.guide_corridor_spacing,
                keepout_enabled=args.keepout,
                keepout_layer=args.keepout_layer,
                proximity_heuristic_factor=args.proximity_heuristic_factor,
                stub_proximity_radius=args.stub_proximity_radius,
                stub_proximity_cost=args.stub_proximity_cost,
                via_proximity_cost=args.via_proximity_cost,
                bga_proximity_radius=args.bga_proximity_radius,
                bga_proximity_cost=args.bga_proximity_cost,
                track_proximity_distance=args.track_proximity_distance,
                track_proximity_cost=args.track_proximity_cost,
                debug_lines=args.debug_lines,
                verbose=args.verbose,
                max_rip_up_count=args.max_ripup,
                ripup_abandon_metric=args.ripup_abandon_metric,
                ripup_blocker_select=args.ripup_blocker_select,
                enable_layer_switch=not args.no_stub_layer_swap,
                crossing_layer_check=not args.no_crossing_layer_check,
                can_swap_to_top_layer=args.can_swap_to_top_layer,
                swappable_net_patterns=args.swappable_nets,
                crossing_penalty=args.crossing_penalty,
                skip_routing=args.skip_routing,
                routing_clearance_margin=args.routing_clearance_margin,
                hole_to_hole_clearance=args.hole_to_hole_clearance,
                board_edge_clearance=args.board_edge_clearance,
                vertical_attraction_radius=args.vertical_attraction_radius,
                vertical_attraction_cost=args.vertical_attraction_cost,
                ripped_route_avoidance_radius=args.ripped_route_avoidance_radius,
                ripped_route_avoidance_cost=args.ripped_route_avoidance_cost,
                length_match_groups=args.length_match_groups,
                length_match_tolerance=args.length_match_tolerance,
                meander_amplitude=args.meander_amplitude,
                meander_spacing=args.meander_spacing,
                time_matching=args.time_matching,
                time_match_tolerance=args.time_match_tolerance,
                debug_memory=args.debug_memory,
                mps_reverse_rounds=args.mps_reverse_rounds,
                mps_layer_swap=args.mps_layer_swap,
                mps_segment_intersection=args.mps_segment_intersection,
                schematic_dir=args.schematic_dir,
                layer_costs=args.layer_costs,
                add_teardrops=args.add_teardrops,
                collect_stats=args.stats)

    # Make the written project's KiCad DRC constraints consistent with the
    # clearances/sizes we just routed to, so a manual DRC only flags genuine
    # problems instead of stock-default noise (issue #160). Only edits the
    # .kicad_pro, never the .kicad_pcb, so the board's KiCad version is preserved.
    if not args.no_fix_drc_settings and not args.skip_routing \
            and args.output_file and os.path.isfile(args.output_file):
        try:
            import clearance_ledger
            eff_clearance = clearance_ledger.effective(args.clearance)
            if eff_clearance < args.clearance:
                print(f"  Min clearance used: {eff_clearance:.4g} mm "
                      f"(below nominal {args.clearance:.4g}) - grading at this floor")
            from fix_kicad_drc_settings import fix_project_for_output, drc_fix_kwargs
            fix_project_for_output(
                args.output_file, input_pcb=args.input_file,
                clearance=eff_clearance, hole_to_hole=args.hole_to_hole_clearance,
                edge_clearance=args.board_edge_clearance, track_width=args.track_width,
                via_diameter=args.via_size, via_drill=args.via_drill,
                **drc_fix_kwargs(args))
        except Exception as e:
            print(f"  (skipped DRC-settings fix: {e})")
        # #521: record this step's protection-worthy nets (matched groups) and
        # impedance declarations in the output project so later chain steps
        # refuse to rip the former and redo the latter at the same widths.
        try:
            from protected_nets import (consume_protection_candidates,
                                        consume_impedance_specs,
                                        persist_protected_nets,
                                        persist_impedance_specs, pro_path_for_board)
            _pro = pro_path_for_board(args.output_file)
            persist_protected_nets(_pro, consume_protection_candidates())
            persist_impedance_specs(_pro, consume_impedance_specs())
        except Exception as e:
            print(f"  (skipped protected-nets record: {e})")
