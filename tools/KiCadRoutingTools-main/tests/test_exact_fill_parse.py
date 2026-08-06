"""kicad_exact_fill.parse_filled_islands: saved-board fill extraction.

Covers the three zone net-token dialects (KiCad 10 names zones' nets BY
NAME with no net table -- the #344 class; KiCad <=9 carries net_name; plain
numeric (net N) resolves through the file's net table) and the
one-filled_polygon-per-island invariant the strap logic relies on.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from kicad_exact_fill import (parse_filled_islands, point_in_poly,  # noqa: E402
                              sample_poly_edges)

TEXT = '''(kicad_pcb
  (net 0 "") (net 5 "GND")
  (footprint "x" (pad "1" smd rect (zone_connect 2)))
  (zone (net "Earth") (layer "B.Cu")
    (polygon (pts (xy 0 0) (xy 50 0) (xy 50 50) (xy 0 50)))
    (filled_polygon (layer "B.Cu")
      (pts (xy 1 1) (xy 20 1) (xy 20 20) (xy 1 20)))
    (filled_polygon (layer "B.Cu")
      (pts (xy 30 30) (xy 45 30) (xy 45 45) (xy 30 45)))
  )
  (zone (net 5) (net_name "GND") (layer "In1.Cu")
    (filled_polygon (layer "In1.Cu") (pts (xy 2 2) (xy 8 2) (xy 8 8) (xy 2 8)))
  )
  (zone (net 5) (layer "In2.Cu")
    (filled_polygon (layer "In2.Cu") (pts (xy 3 3) (xy 9 3) (xy 9 9) (xy 3 9)))
  )
)'''


def main():
    m = parse_filled_islands(TEXT)
    assert set(m) == {('Earth', 'B.Cu'), ('GND', 'In1.Cu'),
                      ('GND', 'In2.Cu')}, sorted(m)
    assert len(m[('Earth', 'B.Cu')]) == 2  # one filled_polygon per island
    assert len(m[('GND', 'In1.Cu')]) == 1
    assert len(m[('GND', 'In2.Cu')]) == 1  # numeric net via net table

    isl = m[('Earth', 'B.Cu')][0]
    assert point_in_poly(10, 10, isl)
    assert not point_in_poly(25, 25, isl)
    assert len(sample_poly_edges(isl)) > 100

    print("PASS: exact-fill island parsing (3 net-token dialects)")


if __name__ == '__main__':
    main()
