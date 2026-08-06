#!/usr/bin/env python3
"""make_movie / make_plan / run_plan: the CLI front doors (#506, #507).

Three small tools that expose what the stress harness already did:

  * ``make_movie.py``  -- a routing movie from a run dir, a board list, or one
    board (the shared core the GUI's "Routing Movie..." button also calls)
  * ``make_plan.py``   -- a GUI-loadable plan JSON from a recorded command chain
  * ``run_plan.py``    -- run that plan headless through the real plugin

What is checked here is everything that does NOT need wx/pcbnew: the manifest ->
plan conversion (including that it still matches ``manifest_to_plan.py`` exactly
after both were pointed at one shared function), input resolution, output-path
defaults, step-range parsing, and a real end-to-end movie render off two boards.
"""
import glob
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, 'tests', 'gui_parity', 'fixtures',
                       'sample_redo_commands.sh')

failures = []


def check(name, ok, detail=''):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail else ''))
    if not ok:
        failures.append(name)


def test_make_plan(tmp):
    print("1. make_plan: a recorded manifest -> a GUI-loadable plan")
    import make_plan

    rundir = os.path.join(tmp, 'myboard')
    os.makedirs(rundir)
    shutil.copy(FIXTURE, os.path.join(rundir, 'redo_commands.sh'))

    out, steps, _skipped = make_plan.make_plan(rundir)
    check("default output is <dir>/<dirname>_plan.json",
          os.path.basename(out) == 'myboard_plan.json', os.path.basename(out))
    check("the plan file parses", os.path.exists(out))
    plan = json.load(open(out))
    actions = [s['action'] for s in plan['steps']]
    check("every recorded routing command became a step",
          actions == ['fanout', 'route_diff', 'route', 'route_planes',
                      'repair_planes'], str(actions))
    check("params are carried from the CLI flags",
          plan['steps'][2]['params'].get('clearance') == 0.09
          and plan['steps'][2]['params'].get('max_ripup') == 10,
          str(plan['steps'][2].get('params')))
    check("the plan carries no private _files keys",
          all('_files' not in s for s in plan['steps']))
    check("a directory and its manifest resolve the same",
          make_plan.resolve_manifest(rundir)
          == make_plan.resolve_manifest(os.path.join(rundir, 'redo_commands.sh')))

    # The converter now has ONE implementation (manifest_to_plan.
    # plan_steps_from_manifest) shared by make_plan.py, manifest_to_plan.main()
    # and the parity harness. Prove the front doors agree.
    sys.path.insert(0, os.path.join(ROOT, 'tests', 'stress'))
    import manifest_to_plan as M
    direct = os.path.join(tmp, 'direct_plan.json')
    argv = sys.argv
    try:
        sys.argv = ['manifest_to_plan.py', os.path.join(rundir, 'redo_commands.sh'),
                    direct]
        M.main()
    finally:
        sys.argv = argv
    check("make_plan matches manifest_to_plan byte for byte",
          open(direct, 'rb').read() == open(out, 'rb').read())
    check("keep_files=True keeps the chain files the parity harness needs",
          all('_files' in s for s in M.plan_steps_from_manifest(
              os.path.join(rundir, 'redo_commands.sh'), keep_files=True)[0]))

    missing = os.path.join(tmp, 'no_manifest_here')
    os.makedirs(missing)
    try:
        make_plan.resolve_manifest(missing)
        check("a directory with no manifest is an error", False)
    except FileNotFoundError:
        check("a directory with no manifest is an error", True)


def test_run_plan_selection():
    print("2. run_plan: --steps selection")
    import run_plan

    check("no selection = every step",
          run_plan.parse_step_selection(None, 4) == [0, 1, 2, 3])
    check("ranges are 1-based and inclusive",
          run_plan.parse_step_selection('1,3-5', 6) == [0, 2, 3, 4])
    check("duplicates collapse, order is kept",
          run_plan.parse_step_selection('3,1,3', 3) == [2, 0])
    for spec in ('0', '7', '2-9'):
        try:
            run_plan.parse_step_selection(spec, 5)
            check(f"out-of-range '{spec}' is refused", False)
        except ValueError:
            check(f"out-of-range '{spec}' is refused", True)


def test_make_movie(tmp):
    print("3. make_movie: inputs, defaults, and a real render")
    import make_movie

    # These are produced by test_fanout_and_route.py, which run_all.py runs 54
    # files LATER, and they are gitignored -- so on a clean tree this check
    # reported a FAILURE for a missing fixture rather than testing make_movie.
    # Build them from the tracked root instead.
    from fixture_boards import ensure_many
    boards = ensure_many('fanout_output1.kicad_pcb', 'fanout_output2.kicad_pcb')

    rundir = os.path.join(tmp, 'run')
    os.makedirs(rundir)
    for i, b in enumerate(boards, 1):
        shutil.copy(b, os.path.join(rundir, f'step{i}_route.kicad_pcb'))

    steps, final = make_movie.resolve_inputs([rundir])
    check("a directory resolves its chain in stepN order",
          [os.path.basename(s[1]) for s in steps]
          == ['step1_route.kicad_pcb', 'step2_route.kicad_pcb'],
          str([os.path.basename(s[1]) for s in steps]))
    check("the last step board is the final board",
          os.path.basename(final) == 'step2_route.kicad_pcb')
    check("a run dir's movie defaults inside it",
          make_movie.default_output([rundir]) == os.path.join(rundir, 'routing.mp4'))
    check("a board list's movie defaults beside the last board",
          make_movie.default_output(boards).endswith(
              os.path.splitext(os.path.basename(boards[-1]))[0] + '_routing.mp4'))

    bsteps, bfinal = make_movie.resolve_inputs(boards)
    check("an explicit board list keeps the order given",
          [s[1] for s in bsteps] == [os.path.abspath(b) for b in boards]
          and bfinal == os.path.abspath(boards[-1]))
    try:
        make_movie.resolve_inputs([os.path.join(tmp, 'nope.kicad_pcb')])
        check("a missing board is an error", False)
    except FileNotFoundError:
        check("a missing board is an error", True)

    try:
        import PIL  # noqa: F401
    except ImportError:
        print("     (Pillow not installed - skipping the render)")
        return
    # .gif so the render needs no imageio/ffmpeg; make_movie reports the file it
    # actually wrote either way.
    out = make_movie.make_movie([rundir], out=os.path.join(tmp, 'movie.gif'),
                                size=200, quiet=True)
    check("the movie renders", bool(out) and os.path.exists(out), str(out))
    check("and is non-empty", bool(out) and os.path.getsize(out) > 0)

    single = make_movie.make_movie([boards[0]], out=os.path.join(tmp, 'one.gif'),
                                   size=200, quiet=True)
    check("a single board animates too", bool(single) and os.path.exists(single))


def main():
    tmp = tempfile.mkdtemp(prefix='plan_movie_tools_')
    try:
        test_make_plan(tmp)
        test_run_plan_selection()
        test_make_movie(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"FAILED ({len(failures)}): {failures}")
        return 1
    print("PASS: make_plan / run_plan / make_movie behave")
    return 0


if __name__ == '__main__':
    sys.exit(main())
