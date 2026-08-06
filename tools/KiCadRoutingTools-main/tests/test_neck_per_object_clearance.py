"""Tap neck-down must price each foreign object at the pair clearance KiCad
enforces (audit H7): max(step clearance, both nets' classes, the foreign
pad's resolved local override #326). The old flat `clearance` under-necked
against larger pad overrides / looser foreign classes, leaving grazes our
grading passed but KiCad flags.
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from route_planes import _neck_plane_segments  # noqa: E402


def tap(width=0.3):
    return {'start': (0.0, 0.0), 'end': (5.0, 0.0), 'width': width,
            'layer': 'F.Cu', 'net_id': 1}


def board(pads_by_net=None, vias=()):
    return SimpleNamespace(pads_by_net=pads_by_net or {},
                           vias=list(vias))


def foreign_pad(local_clearance=0.0):
    # Bottom edge at y=0.5: 0.5mm from the tap centerline.
    return SimpleNamespace(global_x=2.5, global_y=1.0, size_x=1.0, size_y=1.0,
                           shape='rect', rect_rotation=0.0, layers=['F.Cu'],
                           local_clearance=local_clearance)


def foreign_via():
    # 0.75mm from the centerline, r=0.3: clears flat 0.2 (0.75-0.3-0.2 =
    # 0.25 > half 0.15) but not a 0.35 pair clearance (0.10 < 0.15).
    return SimpleNamespace(x=2.5, y=-0.75, size=0.6, drill=0.3, net_id=3,
                           layers=['F.Cu', 'B.Cu'])


def main():
    # 1. Flat clearance 0.2: pad edge 0.5mm away clears a 0.3 tap -- no neck.
    segs = [tap()]
    _neck_plane_segments(segs, board({2: [foreign_pad()]}), 0.2, ['F.Cu'])
    assert segs[0]['width'] == 0.3, segs[0]['width']

    # 2. Same pad with a 0.4 local override: KiCad enforces 0.4, so the tap
    #    must neck to ~0.2 (2 * (0.5 - 0.4 - eps)).
    segs = [tap()]
    _neck_plane_segments(segs, board({2: [foreign_pad(local_clearance=0.4)]}),
                         0.2, ['F.Cu'])
    assert segs[0]['width'] < 0.21, segs[0]['width']
    assert segs[0]['width'] > 0.19, segs[0]['width']

    # 3. Foreign via whose net class (honor mode) is looser than the step
    #    clearance: pair = max(0.2, class 0.35) -> necks; flat 0.2 would not.
    segs = [tap()]
    _neck_plane_segments(segs, board(vias=[foreign_via()]), 0.2, ['F.Cu'])
    assert segs[0]['width'] == 0.3, segs[0]['width']
    segs = [tap()]
    _neck_plane_segments(segs, board(vias=[foreign_via()]), 0.2, ['F.Cu'],
                         net_clearances={3: 0.35})
    assert segs[0]['width'] < 0.3, segs[0]['width']

    # 4. Tap net's OWN class in the pair: class(tap)=0.35 against a plain
    #    foreign via -> same neck as case 3.
    segs = [tap()]
    _neck_plane_segments(segs, board(vias=[foreign_via()]), 0.2, ['F.Cu'],
                         net_clearances={1: 0.35})
    assert segs[0]['width'] < 0.3, segs[0]['width']

    print("PASS: neck-down prices per-object clearance (H7)")


if __name__ == '__main__':
    main()
