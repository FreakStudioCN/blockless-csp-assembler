"""A fresh clone must be able to run the suite (issue #457 item 3).

Two invariants, both of which were violated:

1. **Every board a test consumes must be obtainable.** Several tests read boards
   that another test produces as a side effect and that are gitignored, so on a
   clean tree they did not exist -- and `tests/run_all.py` globs alphabetically,
   which puts every consumer BEFORE its producer, so they could never appear in
   any order either. Each consumer had grown its own `if not exists: SKIP`,
   turning a missing fixture into silent non-coverage (one of them reported an
   outright FAILURE for it). `tests/fixture_boards.py` builds them on demand from
   the TRACKED roots instead.

2. **A tracked `.kicad_pro` / `.kicad_prl` must have a tracked `.kicad_pcb`.**
   Six project files were committed for boards that are generated and ignored.
   Per CLAUDE.md #441 the sibling project carries the DRC floor and must travel
   WITH its board; a tracked project for an untracked board is stale by
   construction, and regenerating the board rewrites it -- so merely running the
   suite left modified tracked files in the working tree.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fixture_boards
from fixture_boards import _RECIPES, ROOT, ensure

SIBLINGS = ('.kicad_pro', '.kicad_prl')


def _tracked():
    r = subprocess.run(['git', 'ls-files', 'kicad_files'],
                       capture_output=True, text=True, cwd=ROOT)
    return set(r.stdout.split())


def test_every_recipe_roots_in_a_tracked_board():
    """A fixture is only reproducible if its chain bottoms out in git."""
    tracked = _tracked()
    for name in _RECIPES:
        seen = []
        cur = name
        while cur in _RECIPES:
            seen.append(cur)
            assert len(seen) < 20, f"recipe cycle: {seen}"
            cur = _RECIPES[cur][0]
        assert f'kicad_files/{cur}' in tracked, (
            f"{name} builds from {cur}, which is NOT tracked -- the chain "
            f"cannot be reproduced on a fresh clone")
        print(f"  PASS: {name} <- {' <- '.join(reversed(seen[1:] + [cur]))}")


def test_no_tracked_project_file_without_its_board():
    """The #441 sibling rule, as a repository invariant."""
    tracked = _tracked()
    orphans = []
    for p in sorted(tracked):
        stem, ext = os.path.splitext(p)
        if ext in SIBLINGS and f'{stem}.kicad_pcb' not in tracked:
            orphans.append(p)
    assert not orphans, (
        "tracked project file(s) whose .kicad_pcb is NOT tracked:\n  "
        + "\n  ".join(orphans)
        + "\nThe project carries the DRC floor and must travel with its board. "
          "For a GENERATED board, the project is generated too -- untrack it "
          "(git rm --cached), do not commit it.")


def test_consumers_do_not_silently_skip_a_missing_fixture():
    """The band-aid this replaced: `if not os.path.exists(board): SKIP`.

    A consumer that skips its own fixture reports success while testing nothing,
    which is how these stayed broken. Each of these files must reach the fixture
    through fixture_boards instead.
    """
    consumers = {
        'test_493_interpreter_independence.py': 'fanout_starting_point.kicad_pcb',
        'test_506_507_plan_movie_tools.py': 'fanout_output1.kicad_pcb',
        'test_dru_layer_clearance_e2e.py': 'fanout_starting_point.kicad_pcb',
        'test_fanout_track_width_floor.py': 'interf_u_plane.kicad_pcb',
        'test_pad_global_nm_grid.py': 'fanout_starting_point.kicad_pcb',
    }
    here = os.path.dirname(os.path.abspath(__file__))
    for fname, board in consumers.items():
        src = open(os.path.join(here, fname), encoding='utf-8').read()
        assert 'fixture_boards' in src, (
            f"{fname} consumes {board} but does not go through fixture_boards; "
            f"on a fresh clone it will skip or fail")
        stem = board[:-len('.kicad_pcb')]
        for bad in (f"exists(os.path.join(ROOT, 'kicad_files', '{board}'))",
                    f'"{stem}"' + ' not present'):
            assert bad not in src, f"{fname} still has a skip band-aid: {bad}"
        print(f"  PASS: {fname} -> fixture_boards ({board})")


def test_fixtures_build_from_a_clean_state():
    """End to end: every fixture is producible right now."""
    for name in _RECIPES:
        path = ensure(name)
        assert os.path.exists(path), f"{name} not produced"
        assert os.path.getsize(path) > 1000, f"{name} looks truncated"
    print(f"  PASS: all {len(_RECIPES)} fixtures present/buildable")


def test_building_a_fixture_leaves_no_tracked_file_modified():
    """Running the suite must not dirty the working tree."""
    r = subprocess.run(['git', 'status', '--porcelain', 'kicad_files'],
                       capture_output=True, text=True, cwd=ROOT)
    dirty = [ln for ln in r.stdout.splitlines()
             if ln[:2] not in ('??',) and not ln[:2].strip() == 'D']
    dirty = [ln for ln in dirty if ' M ' in ln[:3] or ln.startswith(' M')
             or ln.startswith('M ')]
    assert not dirty, (
        "building fixtures modified tracked file(s):\n  " + "\n  ".join(dirty))
    print("  PASS: working tree unmodified by fixture builds")


TESTS = [
    test_every_recipe_roots_in_a_tracked_board,
    test_no_tracked_project_file_without_its_board,
    test_consumers_do_not_silently_skip_a_missing_fixture,
    test_fixtures_build_from_a_clean_state,
    test_building_a_fixture_leaves_no_tracked_file_modified,
]


if __name__ == '__main__':
    for t in TESTS:
        print(f"--- {t.__name__}")
        t()
    print("ALL PASS")
