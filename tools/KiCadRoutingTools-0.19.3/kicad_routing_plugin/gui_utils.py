"""
KiCad Routing Tools - GUI Utilities

Shared utilities for the plugin GUI.
"""


class StdoutRedirector:
    """Redirects stdout to a callback function while preserving original output."""

    def __init__(self, callback, original_stdout):
        self.callback = callback
        self.original = original_stdout

    def write(self, text):
        if text:
            # Write to original stdout. Guard against a non-ASCII glyph (arrows,
            # Ω, ...) that a cp1252 Windows console can't encode, so a log line
            # never crashes the run (issue #152, GUI analog of route.py's UTF-8
            # reconfigure).
            if self.original:
                try:
                    self.original.write(text)
                except UnicodeEncodeError:
                    enc = getattr(self.original, 'encoding', None) or 'ascii'
                    self.original.write(text.encode(enc, errors='replace').decode(enc, errors='replace'))
            # Also send to callback (the wx log control handles Unicode fine)
            self.callback(text)

    def flush(self):
        if self.original:
            self.original.flush()


def apply_via_protection(pcb_via, tenting_attrs):
    """Put a Via's parsed protection spec back onto a pcbnew PCB_VIA (#489 §8).

    GUI counterpart of the writer's tenting round-trip: without it, a via the GUI
    RE-PLACES (rip-up/reroute, sub-grid nudge, tap relocation) comes back with the
    board default and silently loses its own tenting/plugging/filling -- the loss
    the CLI side now avoids. A via with NO spec is deliberately left alone:
    pcbnew's *_MODE_FROM_BOARD already means "inherit the board setting".

    Best-effort; does nothing on a KiCad without these accessors. Returns True
    when something was applied.
    """
    if not tenting_attrs:
        return False
    try:
        import pcbnew
    except ImportError:
        return False
    import re

    def _sides(inner):
        """'(front no) (back yes)' -> {'front': False, 'back': True}"""
        return {m.group(1): (m.group(2) == 'yes')
                for m in re.finditer(r'\((front|back)\s+(yes|no)\)', inner or '')}

    applied = False
    for token, front_set, back_set, yes_name, no_name in (
            ('tenting', 'SetFrontTentingMode', 'SetBackTentingMode',
             'TENTING_MODE_TENTED', 'TENTING_MODE_NOT_TENTED'),
            ('covering', 'SetFrontCoveringMode', 'SetBackCoveringMode',
             'COVERING_MODE_COVERED', 'COVERING_MODE_NOT_COVERED'),
            ('plugging', 'SetFrontPluggingMode', 'SetBackPluggingMode',
             'PLUGGING_MODE_PLUGGED', 'PLUGGING_MODE_NOT_PLUGGED')):
        if token not in tenting_attrs:
            continue
        yes_const = getattr(pcbnew, yes_name, None)
        no_const = getattr(pcbnew, no_name, None)
        if yes_const is None or no_const is None:
            continue
        sides = _sides(tenting_attrs[token])
        for side, setter_name in (('front', front_set), ('back', back_set)):
            if side not in sides:
                continue
            setter = getattr(pcb_via, setter_name, None)
            if setter is None:
                continue
            try:
                setter(yes_const if sides[side] else no_const)
                applied = True
            except Exception:
                pass

    for token, setter_name, yes_name, no_name in (
            ('capping', 'SetCappingMode', 'CAPPING_MODE_CAPPED', 'CAPPING_MODE_NOT_CAPPED'),
            ('filling', 'SetFillingMode', 'FILLING_MODE_FILLED', 'FILLING_MODE_NOT_FILLED')):
        if token not in tenting_attrs:
            continue
        yes_const = getattr(pcbnew, yes_name, None)
        no_const = getattr(pcbnew, no_name, None)
        setter = getattr(pcb_via, setter_name, None)
        if yes_const is None or no_const is None or setter is None:
            continue
        value = (tenting_attrs[token] or '').strip().lower()
        if value not in ('yes', 'no'):
            continue
        try:
            setter(yes_const if value == 'yes' else no_const)
            applied = True
        except Exception:
            pass

    return applied


def apply_teardrops_to_board(board):
    """Enable teardrops on every pad and via of the live board (#489 §9).

    GUI counterpart of the writers' `add_teardrops_to_pads` /
    `add_teardrops_to_vias`: the CLI applies teardrops to the output FILE, while
    the GUI applies copper straight into pcbnew, so without this the "Add
    teardrops" checkbox did nothing on the tabs that apply in memory (fanout, and
    the planes tabs, whose config key was read but never supplied).

    Matches the writers' scope -- all pads and all vias, not just the ones this
    run added -- so the two fronts produce the same board. Best-effort; returns
    (pads_changed, vias_changed), (0, 0) on a KiCad without the accessors.
    """
    try:
        import pcbnew
    except ImportError:
        return (0, 0)

    pads = vias = 0
    try:
        for fp in board.GetFootprints():
            for pad in fp.Pads():
                if not pad.GetTeardropsEnabled():
                    pad.SetTeardropsEnabled(True)
                    pads += 1
        for track in board.GetTracks():
            if track.GetClass() != 'PCB_VIA':
                continue
            if not track.GetTeardropsEnabled():
                track.SetTeardropsEnabled(True)
                vias += 1
    except AttributeError as e:
        print(f"(teardrops skipped: this KiCad has no teardrop API: {e})")
        return (0, 0)
    if pads or vias:
        print(f"Teardrops enabled on {pads} pad(s) and {vias} via(s)")
    return (pads, vias)


def refill_all_zones(board):
    """Re-fill EVERY copper zone on the board so plane pours pull back around
    copper added after they were first filled (#362).

    The plane tab fills only the zones it just created; a signal routed in a
    LATER plan step (e.g. +1V1 after the GND/+3V3 planes exist) then leaves the
    plane fill STALE -- no antipad around the new track/via -- which KiCad DRC
    flags as clearance / shorting violations on the saved board (the CLI board
    is graded with kicad-cli --refill-zones, so it never shows these). Call this
    after any apply that adds copper while filled zones exist. Best-effort.
    Returns the number of zones refilled (0 if none / on error)."""
    try:
        import pcbnew
        zones = list(board.Zones())
        if not zones:
            return 0
        pcbnew.ZONE_FILLER(board).Fill(zones)
        board.BuildConnectivity()
        return len(zones)
    except Exception as e:
        print(f"(zone refill skipped: {e})")
        return 0


def sync_footprint_positions_from_board(board, pcb_data):
    """Refresh footprint and pad POSITIONS in pcb_data from the live pcbnew board
    (#362).

    pcb_data is parsed once when the dialog opens and then reused across plan
    steps. optimize_caps (and manual edits) relocate footprints on the board via
    SetPosition, but the cached pcb_data keeps their load-time positions -- so a
    LATER signal/diff route step routes around where a cap USED to be, and the
    moved cap's pad then shorts the fresh copper (rp2350: +1V1 vias grazing moved
    decoupling caps C18/C22/C23/C24).

    Re-reads each footprint's x/y/rotation and its pads' absolute positions.
    Pad objects are shared with pcb_data.pads_by_net and net.pads, so updating
    them here updates every view the router consults. Best-effort; never raises.
    Returns the number of footprints synced (0 on error).

    Pads are matched to pcb_data by ITERATION ORDER, not pad number: a single
    footprint routinely carries many pads sharing one number (U6 has 11 pads
    numbered "61" for its GND array, plus 4 blank-numbered pads). Matching by
    number collapses them all onto the first pad's position, deleting the copper
    obstacle everywhere else and letting the router short straight through it.
    build_pcb_data_from_board iterates fp.Pads() once with no skips, so order is
    a faithful 1:1 map. The position math below mirrors that function's
    copper-offset fold (#324/#325) so a no-op sync is a true no-op."""
    try:
        import math
        import pcbnew
        n = 0
        for bfp in board.GetFootprints():
            ref = bfp.GetReference()
            pd_fp = pcb_data.footprints.get(ref)
            if pd_fp is None:
                continue
            fpos = bfp.GetPosition()
            pd_fp.x = pcbnew.ToMM(fpos.x)
            pd_fp.y = pcbnew.ToMM(fpos.y)
            try:
                pd_fp.rotation = bfp.GetOrientationDegrees()
            except Exception:
                pass
            bpads = list(bfp.Pads())
            if len(bpads) != len(pd_fp.pads):
                # Order alignment can't be trusted if the counts disagree
                # (should never happen from a live board); skip pads for safety.
                n += 1
                continue
            for pd_pad, bp in zip(pd_fp.pads, bpads):
                ppos = bp.GetPosition()
                gx = pcbnew.ToMM(ppos.x)
                gy = pcbnew.ToMM(ppos.y)
                # Fold the rotated copper offset into the position, exactly as
                # build_pcb_data_from_board does, so global_x/global_y stays the
                # copper center for offset pads.
                try:
                    off = bp.GetOffset()
                    if off.x or off.y:
                        oa = math.radians(-bp.GetOrientationDegrees())
                        ox, oy = pcbnew.ToMM(off.x), pcbnew.ToMM(off.y)
                        gx += ox * math.cos(oa) - oy * math.sin(oa)
                        gy += ox * math.sin(oa) + oy * math.cos(oa)
                        if bp.GetDrillSize().x:
                            pd_pad.hole_x = pcbnew.ToMM(ppos.x)
                            pd_pad.hole_y = pcbnew.ToMM(ppos.y)
                except Exception:
                    pass
                pd_pad.global_x = gx
                pd_pad.global_y = gy
            n += 1
        return n
    except Exception as e:
        print(f"Warning: Error syncing footprint positions from board: {e}")
        return 0


def move_copper_graphics_to_silkscreen_board(board):
    """Move copper graphic shapes (logos / artwork drawn as polys, lines, arcs,
    circles, rects, curves) from F.Cu/B.Cu to the matching silkscreen layer on the
    live pcbnew board.

    Mirrors kicad_writer.move_copper_graphics_to_silkscreen so the GUI matches CLI
    output (issue #146): a net-less copper graphic is not modelled as a router
    obstacle, so plane pours and routed copper run straight over it and short
    against it. Relocating it to silkscreen preserves it visually while taking it
    out of copper.

    Walks both board-level drawings AND footprint graphics - a copper logo is
    frequently a footprint fp_poly (e.g. the orangecrab OSHW logo). Footprint text
    and board text are handled separately by the copper-text mover, so only
    PCB_SHAPE items are touched here. Returns the number of shapes moved.
    """
    import pcbnew

    moved = 0

    def _relocate(item):
        nonlocal moved
        # PCB_SHAPE covers gr_line/poly/arc/circle/rect/curve, and footprint
        # graphics (unified onto PCB_SHAPE in modern KiCad).
        if not isinstance(item, pcbnew.PCB_SHAPE):
            return
        # #337: a NET-TIED copper graphic is functional copper the router models
        # as an immutable obstacle -- LEAVE it (moving it deletes a real
        # connection). Only net-less decoration (a copper logo) is relocated.
        # Mirrors the net guard in kicad_writer.move_copper_graphics_to_silkscreen.
        try:
            if item.GetNetCode() > 0:
                return
        except Exception:
            pass  # older PCB_SHAPE without a net accessor: treat as net-less
        layer = item.GetLayer()
        if layer == pcbnew.F_Cu:
            item.SetLayer(pcbnew.F_SilkS)
            moved += 1
        elif layer == pcbnew.B_Cu:
            item.SetLayer(pcbnew.B_SilkS)
            moved += 1

    for drawing in board.GetDrawings():
        _relocate(drawing)
    for footprint in board.GetFootprints():
        for item in footprint.GraphicalItems():
            _relocate(item)
    return moved


def board_minima_from_live(board):
    """The GUI twin of fix_kicad_drc_settings.scan_board_minima(), read from the
    LIVE pcbnew board instead of re-parsing the board file.

    Same five keys, same meaning: the smallest track width / via diameter / via
    drill / via annular ring / through-hole diameter actually present, so KiCad's
    min-size rules can be floored at or below the board's own copper.

    Why not just call scan_board_minima: it runs parse_kicad_pcb over the whole
    file, allocating thousands of GC-tracked objects. The GUI calls it from
    _write_drc_floors, which runs inside a wx TIMER dispatch, and that allocation
    burst triggers an automatic gen0 collection mid-dispatch which walks a dict
    holding a stale pointer and segfaults (visit_decref -> dict_traverse ->
    collect, faulting on a read-only page). Measured at 3-7 crashes in 10 runs.
    The dangling reference is in a C extension, not here; the tractable fix is to
    stop doing a full board re-parse in that context when the live board -- which
    the GUI already holds -- has every number we need.

    Best-effort: returns {} on error, which makes compute_targets() fall back to
    its param-only behaviour exactly as an unparseable board would.
    """
    try:
        import pcbnew
        widths, via_sizes, via_drills, annular, holes = [], [], [], [], []
        for t in board.GetTracks():
            try:
                if t.Type() == pcbnew.PCB_VIA_T:
                    # GetFrontWidth() first: a bare PCB_VIA::GetWidth() trips a
                    # wxASSERT for every via on KiCad 10 (see
                    # update_live_drc_floors).
                    try:
                        w = pcbnew.ToMM(t.GetFrontWidth())
                    except Exception:
                        w = pcbnew.ToMM(t.GetWidth())
                    d = pcbnew.ToMM(t.GetDrillValue())
                    if w:
                        via_sizes.append(w)
                    if d:
                        via_drills.append(d)
                        holes.append(d)
                    if w and d and w > d:
                        annular.append((w - d) / 2.0)
                else:
                    w = pcbnew.ToMM(t.GetWidth())
                    if w:
                        widths.append(w)
            except Exception:
                continue
        for fp in board.GetFootprints():
            for pad in fp.Pads():
                try:
                    ds = pad.GetDrillSize()
                    d = pcbnew.ToMM(max(ds.x, ds.y))
                    if d:
                        holes.append(d)
                except Exception:
                    continue
        out = {}
        if widths:
            out['min_track_width'] = min(widths)
        if via_sizes:
            out['min_via_diameter'] = min(via_sizes)
        if via_drills:
            out['min_via_drill'] = min(via_drills)
        if annular:
            out['min_via_annular_width'] = min(annular)
        if holes:
            out['min_through_hole_diameter'] = min(holes)
        return out
    except Exception as e:
        print(f"(live board minima scan skipped: {e})")
        return {}


def default_netclass(board):
    """The board's Default NETCLASS, or None.

    `board.GetNetClasses()` returns an EMPTY map on KiCad 10 -- the Default
    class moved to `GetDesignSettings().m_NetSettings.GetDefaultNetclass()`.
    Every `for name, nc in board.GetNetClasses().items(): if name == 'Default'`
    loop therefore iterated ZERO times, and because they all sit inside
    `except Exception: pass`, the entire netclass half of the GUI's DRC-floor
    writeback was a silent no-op. That is why a GUI plan run left the Default
    class at stock values (eth_tap: 0.125 / via 0.5-0.25) while the CLI chain
    tightened it per step (0.0889 / via 0.25-0.15) -- and every later step
    resolves its geometry from that class, so the two fronts routed with
    different parameters.

    Tries the KiCad 10 accessor first, then the legacy map."""
    try:
        ns = board.GetDesignSettings().m_NetSettings
        nc = ns.GetDefaultNetclass()
        if nc is not None:
            return nc
    except Exception:
        pass
    try:
        for _name, _nc in board.GetNetClasses().items():
            if _name == 'Default':
                return _nc
    except Exception:
        pass
    return None


def update_live_drc_floors(board, *, clearance=None, track_width=None,
                           via_size=None, via_drill=None,
                           hole_to_hole=None, edge_clearance=None,
                           log=None):
    """Per-step live DRC floor update -- the GUI twin of the CLI's
    per-step fix_project_for_output (#160). KiCad holds project settings in
    memory, so only the design-settings API affects a DRC run right after a
    step. Two rules, both mirroring the CLI:

    * values only ever RELAX downward (min-merge, like the clearance
      ledger);
    * constraints are clamped to the board's ACTUAL minima (the CLI's
      scan_board_minima step): fine-pitch taps route below the step's
      nominal width/via floor, and a floor of 0.127 still flags a
      legitimate 0.089 tap -- 48 of the 51 'violations' in Andy's bitaxe
      DRC.rpt were this class.

    Best-effort: never raises."""
    # #521: manual (non-plan) runs consume the engine-noted protection
    # candidates here -- the per-step floor update is the GUI's step boundary.
    # Written to the sibling .kicad_pro JSON like the CLI writeback; KiCad may
    # overwrite it if the user saves the project from KiCad afterwards (same
    # caveat as the plan executor's best-effort persistence).
    try:
        from protected_nets import (consume_protection_candidates,
                                    consume_impedance_specs,
                                    persist_protected_nets,
                                    persist_impedance_specs, pro_path_for_board)
        _bf = board.GetFileName() if board is not None else ""
        if _bf:
            _pro = pro_path_for_board(_bf)
            persist_protected_nets(_pro, consume_protection_candidates())
            persist_impedance_specs(_pro, consume_impedance_specs())
    except Exception:
        pass
    try:
        import pcbnew
        # mm_to_iu, NOT pcbnew.FromMM: FromMM TRUNCATES (#493). Measured on this
        # KiCad: FromMM(1.001) = 1000999 where the correct value is 1001000 --
        # 24 of 2010 swept floor values are 1 nm low, all >= 1mm. Today's floors
        # are sub-millimetre so nothing on the current corpus is affected, but a
        # board declaring e.g. a 1.27mm (50 mil) edge clearance would have its
        # DRC floor stamped 1 nm below what was routed, which is exactly the
        # phantom-violation class #493 fixed elsewhere.
        from kicad_parser import mm_to_iu
        bds = board.GetDesignSettings()

        # Actual board minima (copper tracks/vias only).
        min_w = min_via = min_drill = min_ann = None
        for t in board.GetTracks():
            try:
                if t.Type() == pcbnew.PCB_VIA_T:
                    # GetFrontWidth() FIRST. On KiCad 10 a bare
                    # PCB_VIA::GetWidth() trips a wxASSERT ("GetWidth called
                    # without a layer argument") for EVERY via on the board. It
                    # does not raise, so the old order hit the assert path
                    # hundreds of times per call and then fell through to the
                    # same answer -- pure noise in every headless log.
                    # NOT the segfault: with this reversed the asserts are gone
                    # (verified 0 in the log) and a 1-step replay still crashed
                    # 4 runs in 6. Tidy-up only; see the open crash note.
                    try:
                        w = t.GetFrontWidth()
                    except Exception:
                        w = t.GetWidth()
                    d = t.GetDrillValue()
                    min_via = w if min_via is None else min(min_via, w)
                    min_drill = d if min_drill is None else min(min_drill, d)
                    ann = (w - d) // 2
                    min_ann = ann if min_ann is None else min(min_ann, ann)
                else:
                    w = t.GetWidth()
                    min_w = w if min_w is None else min(min_w, w)
            except Exception:
                continue

        def lower(attr, mm, board_min_iu=None):
            if mm is None and board_min_iu is None:
                return
            iu = mm_to_iu(float(mm)) if mm is not None else None
            if board_min_iu is not None:
                iu = board_min_iu if iu is None else min(iu, board_min_iu)
            if getattr(bds, attr) > iu:
                setattr(bds, attr, iu)
        # #498 parity with fix_project_for_output: cap the board's ABSOLUTE
        # min-clearance floor at the smallest .kicad_dru layer rule -- the
        # floor outranks custom rules, so leaving it above a relaxing rule
        # re-manufactures phantom violations on that rule's layer.
        _clr_floor = clearance
        try:
            from kicad_dru import min_rule_clearance
            _dru_min = min_rule_clearance(board.GetFileName() or "")
            if _dru_min is not None and _clr_floor is not None \
                    and _dru_min < float(_clr_floor):
                _clr_floor = _dru_min
        except Exception:
            pass
        lower('m_MinClearance', _clr_floor)
        lower('m_TrackMinWidth', track_width, min_w)
        lower('m_ViasMinSize', via_size, min_via)
        lower('m_MinThroughDrill', via_drill, min_drill)
        lower('m_ViasMinAnnularWidth',
              (float(via_size) - float(via_drill)) / 2
              if via_size and via_drill else None, min_ann)
        lower('m_HoleToHoleMin', hole_to_hole)
        # CLI parity (#338/#441): 0.0 edge clearance means "not enforced this
        # step", NOT "lower the rule to zero". Unlike every other floor here,
        # copper-to-edge is PINNED UP to the fab minimum (0.20 mm): a board
        # declaring a sub-fab (or 0) edge rule would otherwise grade clean while
        # copper runs to the milled edge. Raise to max(routed edge, fab floor);
        # a board rule already above the fab floor (e.g. 0.5) is preserved.
        from fix_kicad_drc_settings import fab_edge_floor
        _edge_pin = max(edge_clearance or 0.0, fab_edge_floor())
        if _edge_pin > 0:
            _pin_iu = mm_to_iu(float(_edge_pin))
            if getattr(bds, 'm_CopperEdgeClearance', 0) < _pin_iu:
                bds.m_CopperEdgeClearance = _pin_iu
        try:
            _nc = default_netclass(board)
            if _nc is not None:
                def _nc_lower(get, set_, mm, board_min_iu=None):
                    if mm is None and board_min_iu is None:
                        return
                    iu = mm_to_iu(float(mm)) if mm is not None else None
                    if board_min_iu is not None:
                        iu = board_min_iu if iu is None else min(iu, board_min_iu)
                    if get() > iu:
                        set_(iu)
                _nc_lower(_nc.GetClearance, _nc.SetClearance, clearance)
                _nc_lower(_nc.GetTrackWidth, _nc.SetTrackWidth,
                          track_width, min_w)
                _nc_lower(_nc.GetViaDiameter, _nc.SetViaDiameter,
                          via_size, min_via)
                _nc_lower(_nc.GetViaDrill, _nc.SetViaDrill,
                          via_drill, min_drill)
        except Exception:
            pass
        if log:
            log("Live DRC floors relaxed to this step's routed values "
                "(clamped to actual board minima)\n")
    except Exception as e:
        if log:
            log(f"(live DRC floor update skipped: {e})\n")
