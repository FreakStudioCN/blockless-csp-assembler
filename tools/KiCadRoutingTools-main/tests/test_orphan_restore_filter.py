"""drop_orphan_restore_pieces: partial restores must not strand copper.

The XTAL_O debris class: a partial restore's collision filter drops pieces
one by one; survivors whose only bridges were dropped become pad-less
islands that nothing downstream can see (pads-only connectivity) or remove
(input-copper guards) -- KiCad then demands a ratsnest link forever. The
filter keeps only pieces that reach a pad or existing board copper of the
net through other kept pieces.
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from pcb_modification import drop_orphan_restore_pieces  # noqa: E402


def seg(x1, y1, x2, y2, layer='F.Cu', w=0.15, net=7):
    return SimpleNamespace(start_x=x1, start_y=y1, end_x=x2, end_y=y2,
                           width=w, layer=layer, net_id=net)


def via(x, y, size=0.5, net=7):
    return SimpleNamespace(x=x, y=y, size=size, drill=0.3, net_id=net,
                           layers=['F.Cu', 'B.Cu'])


def pad(x, y, sx=1.0, sy=1.0, net=7):
    return SimpleNamespace(global_x=x, global_y=y, size_x=sx, size_y=sy,
                           net_id=net)


def board(pads=(), segs=(), vias=()):
    return SimpleNamespace(pads_by_net={7: list(pads)},
                           segments=list(segs), vias=list(vias))


def main():
    # 1. Chain anchored at a pad: A(on pad) - B - C. All kept.
    a = seg(0, 0, 1, 0)
    b = seg(1, 0, 2, 0)
    c = seg(2, 0, 3, 0)
    ks, kv = [a, b, c], []
    n = drop_orphan_restore_pieces(ks, kv, 7, board(pads=[pad(0, 0)]))
    assert n == 0 and len(ks) == 3, (n, len(ks))

    # 2. Same chain, middle piece was collision-dropped before the call:
    #    C is now orphaned (A still reaches the pad) -- exactly XTAL_O.
    ks, kv = [a, c], []
    n = drop_orphan_restore_pieces(ks, kv, 7, board(pads=[pad(0, 0)]))
    assert n == 1 and ks == [a], (n, [f'{s.start_x}' for s in ks])

    # 3. Orphan rescued by existing BOARD copper of the net (trunk touch).
    trunk = seg(3, 0, 4, 0)
    ks, kv = [a, c], []
    n = drop_orphan_restore_pieces(ks, kv, 7,
                                   board(pads=[pad(0, 0)], segs=[trunk]))
    assert n == 0 and len(ks) == 2, (n, len(ks))

    # 4. Via bridges layers: F.Cu piece - via - B.Cu piece anchored at pad.
    f = seg(0, 0, 1, 0, layer='F.Cu')
    bk = seg(1, 0, 2, 0, layer='B.Cu')
    v = via(1, 0)
    ks, kv = [f, bk], [v]
    n = drop_orphan_restore_pieces(ks, kv, 7, board(pads=[pad(0, 0)]))
    assert n == 0 and len(ks) == 2 and len(kv) == 1

    # 5. Fully orphaned set (no pad, no trunk): everything dropped.
    ks, kv = [b, c], []
    n = drop_orphan_restore_pieces(ks, kv, 7, board(pads=[pad(10, 10)]))
    assert n == 2 and not ks, (n, len(ks))

    print("PASS: orphan-restore filter (XTAL_O debris class)")


if __name__ == '__main__':
    main()
