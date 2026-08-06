"""Write-list vs pcb_data reconciliation shared by both plane engines (#508).

The plane engines keep two parallel representations of this run's copper:
``pcb_data`` (what the router reasons about) and the write list
(``all_new_segments``/``all_new_vias`` dicts, written to the file or returned
to the GUI). A pass that removes copper from one without the other ships
either stale copper (a possible different-net SHORT: #463 spartan6_6layer)
or a phantom disconnect.

Two reconciliation mechanisms close the gap after an in-memory ripped-net
reconnect (``batch_route(return_results=True, pcb_data=...)``):

- ``consume_inner_strips``: the inner batch_route's cleanup removes
  superseded/orphaned copper from pcb_data and reports it in
  ``segments_to_remove``/``vias_to_remove``. Dropping that channel ships
  copper the board no longer has.
- ``drop_withdrawn_partial_restores``: a partial restore's kept-set is
  emitted into the write list BEFORE the reconnect runs; the reconnect may
  re-route the same net and delete that copper from pcb_data.

Both were built (and corpus-proven) in route_disconnected_planes; #508
finding 1 is that route_planes' GUI reconnect had NEITHER. They live here so
each engine drives the SAME function the tests pin (the parity-gate lesson:
a hand-mirrored copy silently drifts).
"""
from typing import Dict, List


def drop_withdrawn_partial_restores(emitted_segs, emitted_vias,
                                    all_new_segments, all_new_vias, pcb_data):
    """Drop partial-restore copper the ripped-net reconnect has since withdrawn.

    A partial restore's kept-set is emitted into the write list BEFORE the
    reconnect runs, and unconditionally -- but the same net is queued as a
    reconnect casualty, so the reconnect may RE-ROUTE it and delete that copper
    from pcb_data. The write list still carried it, so the OUTPUT shipped
    copper present in no in-memory state (#463 spartan6_6layer: /RAM/DDR-LDM's
    In3.Cu run written after the reconnect moved it to In1.Cu and gave the
    corridor to /RAM/DDR-D11 -- a 0.220mm collinear overlap, a hard short).

    pcb_data is authoritative once the reconnect and its restore-on-failure
    custody have run. Pure write-list filter: no routing, no obstacle work, no
    persistent structures, and indexed on the emitted nets only (a whole-board
    index would be ~all copper for a handful of lookups).

    Matching is by IDENTITY, not value: an equal-looking dict may be legitimate
    copper from another pass. `stale_*` holds the references across the filter,
    so the id() keys cannot be recycled underneath us.

    Mutates all_new_segments/all_new_vias in place; returns
    (n_segments_dropped, n_vias_dropped, sorted net names).
    """
    if not emitted_segs and not emitted_vias:
        return 0, 0, []
    pn = ({d['net_id'] for d in emitted_segs}
          | {d['net_id'] for d in emitted_vias})
    live_s = {(s.net_id, round(s.start_x, 3), round(s.start_y, 3),
               round(s.end_x, 3), round(s.end_y, 3), s.layer)
              for s in pcb_data.segments if s.net_id in pn}
    live_v = {(v.net_id, round(v.x, 3), round(v.y, 3))
              for v in pcb_data.vias if v.net_id in pn}
    stale_s = [d for d in emitted_segs
               if (d['net_id'], round(d['start'][0], 3), round(d['start'][1], 3),
                   round(d['end'][0], 3), round(d['end'][1], 3),
                   d['layer']) not in live_s]
    stale_v = [d for d in emitted_vias
               if (d['net_id'], round(d['x'], 3), round(d['y'], 3)) not in live_v]
    if not stale_s and not stale_v:
        return 0, 0, []
    sid = {id(d) for d in stale_s}
    vid = {id(d) for d in stale_v}
    all_new_segments[:] = [d for d in all_new_segments if id(d) not in sid]
    all_new_vias[:] = [d for d in all_new_vias if id(d) not in vid]
    names = sorted({(pcb_data.nets[d['net_id']].name
                     if d['net_id'] in pcb_data.nets else str(d['net_id']))
                    for d in stale_s + stale_v})
    return len(stale_s), len(stale_v), names


def consume_inner_strips(rdata, all_new_segments, all_new_vias, pcb_data,
                         file_strip_segments, file_strip_vias, label):
    """Board == write model (#XTAL_O zombie class): an in-memory batch_route's
    cleanup removes superseded/orphaned copper from pcb_data AND reports it in
    segments_to_remove/vias_to_remove -- dropping that channel ships copper the
    board no longer has (the write-list still carries it via the
    partial-restore emissions). Remove matching write-list entries; casualty
    nets' input-file copper is already wholly excluded by the writer/applier,
    so coordinate matching over the emissions is the complete consumption.

    Input copper of NON-excluded nets (#484): removing it from the write-list
    is not enough -- the writer re-emits input text, so these also go to the
    caller's per-segment strip channel (``file_strip_segments``/``_vias``,
    which the CLI writer and the GUI applier both honor).

    Returns the number of write-list pieces dropped.
    """
    rs = rdata.get('segments_to_remove') or []
    rv = rdata.get('vias_to_remove') or []
    if not rs and not rv:
        return 0
    file_strip_segments.extend(rs)
    file_strip_vias.extend(rv)
    skeys = set()
    for s in rs:
        a = (round(s.start_x, 3), round(s.start_y, 3))
        b = (round(s.end_x, 3), round(s.end_y, 3))
        skeys.add((a, b, s.layer, s.net_id))
        skeys.add((b, a, s.layer, s.net_id))
    vkeys = {(round(v.x, 3), round(v.y, 3), v.net_id) for v in rv}
    n0 = len(all_new_segments) + len(all_new_vias)
    if skeys:
        all_new_segments[:] = [
            d for d in all_new_segments
            if ((round(d['start'][0], 3), round(d['start'][1], 3)),
                (round(d['end'][0], 3), round(d['end'][1], 3)),
                d['layer'], d['net_id']) not in skeys]
    if vkeys:
        all_new_vias[:] = [
            d for d in all_new_vias
            if (round(d['x'], 3), round(d['y'], 3),
                d['net_id']) not in vkeys]
    # mirror out of pcb_data too (inner cleanup already removed its own
    # objects; kept-piece duplicates re-added via partial_restores may remain)
    pcb_data.segments[:] = [
        s for s in pcb_data.segments
        if ((round(s.start_x, 3), round(s.start_y, 3)),
            (round(s.end_x, 3), round(s.end_y, 3)),
            s.layer, s.net_id) not in skeys]
    pcb_data.vias[:] = [
        v for v in pcb_data.vias
        if (round(v.x, 3), round(v.y, 3), v.net_id) not in vkeys]
    n1 = len(all_new_segments) + len(all_new_vias)
    if n0 != n1:
        print(f"  {label}: consumed inner strip channel -- dropped "
              f"{n0 - n1} superseded write-list piece(s)")
    return n0 - n1
