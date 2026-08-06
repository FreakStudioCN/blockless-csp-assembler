"""The plan executor guarantees a route step runs after the last pour.

Two things make this load-bearing since #562:

  * the pour places NO taps (unconditionally, since #562 deleted the tap
    machinery and its kill switch), so plane pads
    are welded by the ROUTE step's pour-launch -- a plan that pours and then
    never routes connects nothing at all; and
  * every route step ends with the in-run plane finalize (repair engine +
    plane-copper cleanup + kicad-oracle verify), so a route step running
    last IS the late-pinch guard a trailing `repair_planes` step used to
    provide (#479: ch32v203's In1.Cu GND shipped severed behind an
    all-green chain).

So `parse_plan_result` appends ONE final `route` step whenever the plan does
not already end -- ignoring now-inert legacy `repair_planes` steps -- with
one. This pins that contract, including the legacy shapes: an old recorded
plan of `pour -> repair_planes` must be UPGRADED, not silently left inert.
No wx required (stubbed).
"""
import json
import os
import sys
import types

sys.modules.setdefault('wx', types.ModuleType('wx'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..',
                                'kicad_routing_plugin'))

from ai_plan import parse_plan_result  # noqa: E402

ASSIGN = [{'nets': ['GND'], 'layer': 'In1.Cu'}]
_fail = []


def check(label, cond):
    print(f"  {'PASS' if cond else 'FAIL'}: {label}")
    if not cond:
        _fail.append(label)


def parse(*steps):
    got, errs = parse_plan_result(json.dumps({'steps': [dict(s) for s in steps]}))
    assert got is not None, errs
    return got


def main():
    print("1. legacy plan that pours and then only 'repairs'")
    # Pre-#562 that trailing repair was both the weld and the verify. It is
    # now skipped as a no-op, so without an appended route this plan would
    # pour and do NOTHING -- every plane pad left unwelded.
    steps = parse({'action': 'route', 'nets': ['*', '!GND']},
                  {'action': 'route_planes', 'assignments': ASSIGN},
                  {'action': 'repair_planes', 'assignments': ASSIGN})
    check("a final route step is appended", steps[-1]['action'] == 'route')
    check("it routes ALL nets (plane nets included, #562)",
          steps[-1].get('nets') == ['*'])

    print("2. copper after the pour (the #479 late-pinch case)")
    steps = parse({'action': 'route_planes', 'assignments': ASSIGN},
                  {'action': 'route', 'nets': ['*'],
                   'params': {'clearance': 0.1, 'track_width': 0.127}},
                  {'action': 'fanout', 'component': 'U1'})
    check("a final route step is appended", steps[-1]['action'] == 'route')
    check("it reuses the chain's routing geometry",
          steps[-1]['params'].get('clearance') == 0.1
          and steps[-1]['params'].get('track_width') == 0.127)

    print("3. plan already ends with a route -> nothing appended")
    steps = parse({'action': 'route_planes', 'assignments': ASSIGN},
                  {'action': 'route', 'nets': ['*']})
    check("no extra step", len(steps) == 2)

    print("4. trailing legacy repair AFTER a route -> nothing appended")
    # The route's finalize already welded and verified; the inert repair
    # step must not trick the guard into a second full route.
    steps = parse({'action': 'route_planes', 'assignments': ASSIGN},
                  {'action': 'route', 'nets': ['*']},
                  {'action': 'repair_planes', 'assignments': ASSIGN})
    check("no extra step", len(steps) == 3)

    print("5. no pour at all -> no verify route appended")
    # NB parse_plan_result ALSO auto-inserts an optimize_caps step after a BGA
    # fanout (#130), so assert on this guard's own effect, not on the count.
    steps = parse({'action': 'route', 'nets': ['*']},
                  {'action': 'fanout', 'component': 'U1'})
    check("no plane-verify route appended (only the cap-opt insert)",
          sum(1 for s in steps if s['action'] == 'route') == 1
          and steps[-1]['action'] != 'route')

    print()
    if _fail:
        print(f"FAILED ({len(_fail)}): " + "; ".join(_fail))
        sys.exit(1)
    print("All checks passed")


if __name__ == '__main__':
    main()
