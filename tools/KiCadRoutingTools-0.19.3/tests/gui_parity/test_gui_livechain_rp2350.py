#!/usr/bin/env python3
"""GRADE-parity gate on a set11-class board (rp2350_fpga_eensy).

The copper-identity harness (test_gui_engine_parity.py) proved GUI-vs-CLI on a
route/planes/repair chain but measures copper-overlap %, which on a chaotic
rip-up router diverges even when both fronts grade clean (#362). The invariant
that actually matters is GRADE parity: the GUI must not introduce DRC the CLI
doesn't.

This gate chains the rp2350 PLANE sub-chain (create -> repair -> reconnect
route -> repair2) on ONE live pcbnew board -- exactly as the Claude-tab plan
executor does, in-memory across steps -- starting from the recorded CLI
pre-plane board, and asserts every stage grades 0 DRC like the CLI file chain.

MIGRATED off the shim harness (2026-07-26). The GUI leg used to bind real tab
methods onto plain shim objects and hand-build the engine config, which has a
structural blind spot: anything between a dialog CONTROL and the engine
argument never executes. That is not hypothetical -- the same shim style made
test_gui_engine_parity report a phantom 73-segment plane-tap "divergence" on
splitflap that does not exist in the real GUI (the shim never ran
_effective_track_width(), so it passed defaults.TRACK_WIDTH 0.3 where the real
dialog resolves the board's 0.127). Now it runs the REAL headless
swig_gui.RoutingDialog driven by the REAL ai_plan.PlanExecutor, via
replay_plan_vs_run.replay() -- the same machinery the corpus driver uses, which
only needs {'input_board': path}, so it works on a checked-in board.

It caught the swig_gui route-apply width-rounding bug (0.0762 -> 0.076 fab-floor
violations, 42 of them at the reconnect route step; #362). Per-step isolation
on CLI inputs did NOT catch it -- only chaining on a live board did, because
the bug rides the GUI's in-memory apply path.

The pre-plane input board (rp2350_fpga_eensy_prePlane.kicad_pcb, the recorded
step4b_retry) is checked into kicad_files/, so the gate is self-contained.
Needs KiCad's python (pcbnew); skips (exit 0) if pcbnew is absent.
Run: python3 tests/gui_parity/test_gui_livechain_rp2350.py
"""

# ---------------------------------------------------------------------------
# macOS: if this HANGS at ~0% CPU, it is NOT wx, machine load, or a deadlock.
#
# After any wx process here is killed (a pkill, a timeout, a crash), macOS
# decides the app "quit unexpectedly", and the NEXT headless launch stops inside
# NSApplication bootstrap showing the restore-windows alert you cannot see:
#     -[NSPersistentUIRestorer promptToIgnorePersistentState]
#         -> -[NSAlert runModal]
# Headless, nobody can click it, so it waits forever: process state SN accruing
# ~0.3s of CPU over many minutes, which reads exactly like a hang. This cost a
# full session of ".gui-parity-checked" markers recording "wx blocked, gate NOT
# RUN" -- the gates were fine the whole time.
#
#   diagnose:  sample <pid> 3 -mayDie | grep -E "NSAlert|PersistentUI"
#   fix:       defaults write -g ApplePersistenceIgnoreState -bool YES
#
# A sandboxed HOME does NOT help -- cfprefsd serves that pref per-user
# regardless of HOME. With the default set, test_gui_engine_parity.py runs ~90s.
# ---------------------------------------------------------------------------
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, 'tests', 'gui_parity'))
START_BOARD = os.path.join(REPO, 'kicad_files', 'rp2350_fpga_eensy_prePlane.kicad_pcb')

KICAD_PYTHONS = [
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3",
    "/usr/bin/python3",
    os.path.expandvars(r"C:\\Program Files\\KiCad\\bin\\python.exe"),
]


def _reexec_into_kicad():
    for cand in KICAD_PYTHONS:
        if cand != sys.executable and os.path.exists(cand):
            if subprocess.run([cand, '-c', 'import pcbnew'],
                              capture_output=True).returncode == 0:
                os.execv(cand, [cand, os.path.abspath(__file__)] + sys.argv[1:])
    print("SKIP: no python with pcbnew found")
    sys.exit(0)


def _grade(pcb, clr=0.09):
    r = subprocess.run(['python3', os.path.join(REPO, 'check_drc.py'), pcb,
                        '--clearance', str(clr), '--hole-to-hole-clearance', '0.2',
                        '--clearance-margin', '0.1'], capture_output=True, text=True)
    m = re.search(r'FOUND (\d+) DRC', r.stdout)
    return 0 if 'NO DRC' in r.stdout else (int(m.group(1)) if m else -1)


def _cli_chain(work):
    """Run the EQUIVALENT CLI file chain and grade each stage.

    #495: this gate used to grade only the GUI stages and then assert, in its
    failure message, that "the CLI file chain does not" introduce the DRC --
    without ever running the CLI. That let a CLI-side defect (8 track-through-
    NPTH-hole violations from the plane repair's oracle recheck) sit green here
    while the board it grades was demonstrably dirty. Measure both fronts.

    Runs under THIS interpreter (sys.executable, i.e. KiCad's python when the
    gate re-execs into it) so the comparison is not contaminated by the
    interpreter-dependent routing #493 fixed.
    """
    py = sys.executable
    b0 = os.path.join(work, 'cli_start.kicad_pcb')
    shutil.copy(START_BOARD, b0)
    layers = ['F.Cu', 'In1.Cu', 'In2.Cu', 'In3.Cu', 'In4.Cu', 'B.Cu']
    planes = os.path.join(work, 'cli_planes.kicad_pcb')
    repair = os.path.join(work, 'cli_repair.kicad_pcb')
    recon = os.path.join(work, 'cli_reconnect.kicad_pcb')
    final = os.path.join(work, 'cli_final.kicad_pcb')
    R = lambda s: os.path.join(REPO, s)
    # Mirrors the GUI stages below (same nets/params, and the same grid_step
    # 0.05 on the second repair that the GUI stage uses for runtime).
    steps = [
        ('create', planes, [py, '-X', 'utf8', R('route_planes.py'), b0, planes,
                            '--nets', 'GND', '+3V3',
                            '--plane-layers', 'In1.Cu', 'In4.Cu',
                            '--via-size', '0.45', '--via-drill', '0.2',
                            '--track-width', '0.09', '--clearance', '0.10',
                            '--hole-to-hole-clearance', '0.2', '--grid-step', '0.05',
                            '--power-nets', 'VIN', '--power-nets-widths', '0.3']),
        ('repair', repair, [py, '-X', 'utf8', R('route_disconnected_planes.py'),
                            planes, repair, '--nets', 'GND', '+3V3',
                            '--clearance', '0.09', '--via-size', '0.25',
                            '--via-drill', '0.15', '--track-width', '0.0762',
                            '--grid-step', '0.05', '--hole-to-hole-clearance', '0.2',
                            '--rip-blocker-nets',
                            '--power-nets', 'VIN', '--power-nets-widths', '0.3']),
        ('reconnect', recon, [py, '-X', 'utf8', R('route.py'), repair, recon,
                              '--nets', '+1V1', '/T8F49I2X/PIN.5',
                              '--layers'] + layers + [
                              '--no-bga-zones', '--clearance', '0.09',
                              '--track-width', '0.0762', '--via-size', '0.25',
                              '--via-drill', '0.15', '--hole-to-hole-clearance', '0.2',
                              '--grid-step', '0.025', '--max-ripup', '10',
                              '--max-iterations', '1000000']),
        ('final', final, [py, '-X', 'utf8', R('route_disconnected_planes.py'),
                          recon, final, '--nets', 'GND', '+3V3',
                          '--clearance', '0.09', '--via-size', '0.25',
                          '--via-drill', '0.15', '--track-width', '0.0762',
                          '--grid-step', '0.05', '--hole-to-hole-clearance', '0.2',
                          '--rip-blocker-nets',
                          '--power-nets', 'VIN', '--power-nets-widths', '0.3']),
    ]
    grades = {}
    for tag, out, cmd in steps:
        p = subprocess.run(cmd, capture_output=True, text=True, cwd=work)
        if not os.path.exists(out):
            print(f"  CLI stage {tag} produced no output (rc={p.returncode})")
            print((p.stdout or '')[-1500:])
            print((p.stderr or '')[-1500:])
            grades[tag] = -1
            break
        grades[tag] = _grade(out)
    return grades


# The GUI leg as a real Claude-tab PLAN -- the same JSON shape manifest_to_plan
# emits and the plan executor consumes. Mirrors _cli_chain() step for step.
# `grid_step` 0.05 on BOTH repairs: that stage is the #479 late-pinch guard (it
# re-checks that the reconnect route did not sever the pour), not a test of grid
# resolution, and halving the step quadruples the cell count for no extra
# coverage -- measured 354s at 0.025 vs 118s at 0.05 on this board.
PLANE_ASSIGNMENTS = [{'nets': ['GND'], 'layer': 'In1.Cu'},
                     {'nets': ['+3V3'], 'layer': 'In4.Cu'}]
_GP = {'power_nets': ['VIN'], 'power_nets_widths': [0.3],
       'hole_to_hole_clearance': 0.2}
STAGE_TAGS = ['create', 'repair', 'reconnect', 'final']
PLAN = [
    {'action': 'route_planes',
     'params': dict(via_size=0.45, via_drill=0.2, clearance=0.10,
                    track_width=0.09, grid_step=0.05, **_GP),
     'assignments': PLANE_ASSIGNMENTS},
    {'action': 'repair_planes',
     'params': dict(clearance=0.09, via_size=0.25, via_drill=0.15,
                    track_width=0.0762, grid_step=0.05,
                    rip_blocker_nets=True, **_GP),
     'assignments': PLANE_ASSIGNMENTS},
    {'action': 'route',
     'params': dict(clearance=0.09, track_width=0.0762, via_size=0.25,
                    via_drill=0.15, grid_step=0.025, max_ripup=10,
                    max_iterations=1000000, no_bga_zone=True,
                    hole_to_hole_clearance=0.2,
                    layers=['F.Cu', 'In1.Cu', 'In2.Cu', 'In3.Cu', 'In4.Cu', 'B.Cu']),
     'nets': ['+1V1', '/T8F49I2X/PIN.5']},
    {'action': 'repair_planes',
     'params': dict(clearance=0.09, via_size=0.25, via_drill=0.15,
                    track_width=0.0762, grid_step=0.05,
                    rip_blocker_nets=True, **_GP),
     'assignments': PLANE_ASSIGNMENTS},
]


def main():
    start_board = START_BOARD
    if not os.path.exists(start_board):
        print(f"SKIP: checked-in board not found at {start_board}")
        return 0

    # The REAL headless dialog + REAL PlanExecutor. replay() touches `info` only
    # for input_board, so the corpus driver works unchanged on a repo board.
    import replay_plan_vs_run as R

    work = tempfile.mkdtemp(prefix='rp2350_livechain_')
    print(f"running the GUI plan through the real dialog ({len(PLAN)} steps)...",
          flush=True)
    res = R.replay({'input_board': start_board}, PLAN, work, snapshots=True)
    if res.get('aborted'):
        print(f"FAIL: GUI plan aborted: {res['aborted']}")
        shutil.rmtree(work, ignore_errors=True)
        return 1
    if res.get('completed', 0) != len(PLAN):
        print(f"FAIL: GUI plan ran {res.get('completed')} of {len(PLAN)} steps.")
        shutil.rmtree(work, ignore_errors=True)
        return 1

    # replay() snapshots each completed step as gui_stepNN.kicad_pcb.
    stages = {}
    for i, tag in enumerate(STAGE_TAGS, 1):
        snap = os.path.join(work, f'gui_step{i:02d}.kicad_pcb')
        if not os.path.exists(snap):
            print(f"FAIL: no GUI snapshot for stage {tag}")
            shutil.rmtree(work, ignore_errors=True)
            return 1
        stages[tag] = _grade(snap)

    # #495: actually RUN the CLI chain instead of asserting it is clean.
    print("\nrunning the equivalent CLI file chain for comparison...", flush=True)
    cli = _cli_chain(work)

    print("\nrp2350 live-chain grade parity (DRC @ 0.09):")
    print(f"  {'stage':<12} {'GUI':>6} {'CLI':>6}")
    gui_bad, cli_bad = [], []
    for tag, n in stages.items():
        c = cli.get(tag, -1)
        print(f"  {tag:<12} {n:>6} {c:>6}   "
              f"[{'OK' if n == 0 else 'FAIL'}/{'OK' if c == 0 else 'FAIL'}]")
        if n != 0:
            gui_bad.append(tag)
        if c != 0:
            cli_bad.append(tag)
    shutil.rmtree(work, ignore_errors=True)

    rc = 0
    if cli_bad:
        # Measured, not assumed: a CLI-side defect fails this gate on its own
        # (#495 defect 1 -- the plane repair's oracle recheck shipped 8 GND
        # straps through J1's NPTH mounting hole at an unvalidated width).
        print(f"\nFAIL: the CLI file chain introduced DRC at stage(s) {cli_bad}.")
        rc = 1
    if gui_bad:
        extra = [t for t in gui_bad if stages[t] > cli.get(t, 0)]
        print(f"\nFAIL: GUI live-chain introduced DRC at stage(s) {gui_bad}"
              + (f"; worse than the CLI at {extra} (#362)." if extra else "."))
        rc = 1
    if rc:
        return rc
    print("\nPASS: GUI and CLI chains both grade clean at every stage.")
    return 0


if __name__ == "__main__":
    try:
        import pcbnew  # noqa: F401
    except ImportError:
        _reexec_into_kicad()
    sys.exit(main())
