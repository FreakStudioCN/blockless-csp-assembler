#!/usr/bin/env python3
"""Guard the plane engines' in-memory batch_route sub-calls against the
forwarding-gap class (#513 item 5, 8920cb0, #539).

batch_route's FUNCTION path does not auto-read the board's min_hole_to_hole
(only route.py's main() does), so any plane-engine sub-call that omits
hole_to_hole_clearance silently routes at the 0.2 default. On a board whose
rule is tighter (muzy_zynq2: 0.25) the sub-call's vias land legally-at-0.2 /
illegally-at-the-rule next to other passes' vias -- the #539 drill grazes.
Three call sites have now been fixed for exactly this omission (the two
reconnects in 8920cb0, the guaranteed-join gate in #539), so pin ALL of them:
every batch_route( call in the plane engines must forward
hole_to_hole_clearance and net_clearances explicitly.

(board_edge_clearance is deliberately NOT required: batch_route resolves the
effective edge rule itself from input_file -- route.py:effective_board_edge_
clearance -- so omitting it is inert.)
"""
import ast
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Engine files whose batch_route sub-calls must forward the step's resolved
# clearances. route.py itself is excluded (it IS batch_route's home; its
# internal recursion forwards **kwargs wholesale).
ENGINE_FILES = [
    'repair_planes.py',
    'route_planes.py',
]

REQUIRED_KWARGS = ('hole_to_hole_clearance', 'net_clearances')


def _batch_route_calls(path):
    with open(path, encoding='utf-8') as f:
        tree = ast.parse(f.read(), filename=path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute) else None)
            if name == 'batch_route':
                yield node


class TestPlaneSubrouteForwarding(unittest.TestCase):
    def test_batch_route_subcalls_forward_clearances(self):
        missing = []
        found_any = False
        for fname in ENGINE_FILES:
            path = os.path.join(REPO, fname)
            for call in _batch_route_calls(path):
                found_any = True
                kwargs = {kw.arg for kw in call.keywords if kw.arg}
                has_star_star = any(kw.arg is None for kw in call.keywords)
                if has_star_star:
                    continue  # wholesale forwarding is fine
                for req in REQUIRED_KWARGS:
                    if req not in kwargs:
                        missing.append(f"{fname}:{call.lineno} omits {req}=")
        self.assertTrue(found_any,
                        "no batch_route( calls found -- scanner is broken "
                        "or the call sites moved; update ENGINE_FILES")
        self.assertFalse(
            missing,
            "plane-engine batch_route sub-call(s) missing explicit "
            "forwarding (the #539 gap class -- batch_route's function path "
            "defaults hole_to_hole_clearance to 0.2 and never reads the "
            "board rule):\n  " + "\n  ".join(missing))


if __name__ == '__main__':
    unittest.main()
