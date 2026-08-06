"""End-to-end meander demo chains (#501): SE trombone + diff centerline meanders.

Drives the REAL CLIs (route.py / route_diff.py) over two boards and asserts the
routed copper, not the logs:

  1. A synthetic 7-net board (generated here; the same generator that produced
     the ~/Downloads meander demo): two coupled diff pairs of different lengths
     (pair-to-pair centerline meanders) + three SE nets of different lengths
     (trombone group meanders). Asserts 2/2 coupled, 3/3 routed, DRC-clean,
     fully connected, and — the #501 regression — the SE meander riser pitch on
     copper is >= --meander-spacing x the track width (default 2W; also checked
     at an explicit non-default 3W to prove the flag reaches the geometry).
  2. kicad_files/lvds_converter_dualclk.kicad_pcb with --diff-pair-intra-match:
     asserts the intra-pair pass adds bumps and closes the P/N delta.

KiCad-10 format note (learned the hard way): a version >= 20250000 board has NO
top-level net table — nets exist only as (net "name") refs inside pads. A
top-level (net ...) block is parsed by KiCad as the legacy NUMERIC table and the
board refuses to load ("need a number for 'net number'"), while our parser
yields pcb.nets == {} for numeric declarations. Emit no table at all.
"""
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kicad_parser import parse_kicad_pcb  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NETS = ["DP_A_P", "DP_A_N", "DP_B_P", "DP_B_N", "DP_C_P", "DP_C_N",
        "SE1", "SE2", "SE3", "SE4"]

# (ref, x, y, net). DP_A is a straight 40mm pair; DP_B ~32mm (needs +8mm of
# centerline meanders to match); DP_C is a MULTIPOINT pair (3 terminals, two
# 20mm legs -- matched on its longest MST leg, #520); SE1/SE2/SE3 are
# 40/32/24mm (SE2+SE3 meander).
PADS = [
    ("P1", 10, 10, "DP_A_P"), ("P2", 10, 12, "DP_A_N"),
    ("P3", 50, 10, "DP_A_P"), ("P4", 50, 12, "DP_A_N"),
    ("P5", 14, 16, "DP_B_P"), ("P6", 14, 18, "DP_B_N"),
    ("P7", 46, 20, "DP_B_P"), ("P8", 46, 22, "DP_B_N"),
    ("P9", 10, 28, "SE1"), ("P10", 50, 28, "SE1"),
    ("P11", 14, 32, "SE2"), ("P12", 46, 32, "SE2"),
    ("P13", 18, 36, "SE3"), ("P14", 42, 36, "SE3"),
    ("P15", 10, 40, "DP_C_P"), ("P16", 10, 42, "DP_C_N"),
    ("P17", 30, 40, "DP_C_P"), ("P18", 30, 42, "DP_C_N"),
    ("P19", 50, 40, "DP_C_P"), ("P20", 50, 42, "DP_C_N"),
    ("P21", 10, 24, "SE4"), ("P22", 50, 24, "SE4"),   # spare target for rip tests
]


def write_synth_board(path):
    """Minimal KiCad-10 board: single-pad footprints + rectangular outline."""
    parts = ["""(kicad_pcb
\t(version 20260206)
\t(generator "pcbnew")
\t(generator_version "10.0")
\t(general
\t\t(thickness 1.6)
\t\t(legacy_teardrops no)
\t)
\t(paper "A4")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(2 "B.Cu" signal)
\t\t(1 "F.Mask" user)
\t\t(3 "B.Mask" user)
\t\t(5 "F.SilkS" user "F.Silkscreen")
\t\t(7 "B.SilkS" user "B.Silkscreen")
\t\t(25 "Edge.Cuts" user)
\t\t(35 "F.Fab" user)
\t\t(33 "B.Fab" user)
\t)
\t(setup
\t\t(stackup
\t\t\t(layer "F.Cu" (type "copper") (thickness 0.035))
\t\t\t(layer "dielectric 1" (type "core") (thickness 0.15) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02))
\t\t\t(layer "B.Cu" (type "copper") (thickness 0.035))
\t\t)
\t\t(pad_to_mask_clearance 0)
\t)"""]
    for ref, x, y, name in PADS:
        parts.append(f'''\t(footprint "DemoPad"
\t\t(layer "F.Cu")
\t\t(uuid "{uuid.uuid4()}")
\t\t(at {x} {y})
\t\t(property "Reference" "{ref}"
\t\t\t(at 0 -1.5 0)
\t\t\t(layer "F.SilkS")
\t\t\t(uuid "{uuid.uuid4()}")
\t\t\t(effects (font (size 1 1) (thickness 0.15)))
\t\t)
\t\t(property "Value" "DemoPad"
\t\t\t(at 0 1.5 0)
\t\t\t(layer "F.Fab")
\t\t\t(uuid "{uuid.uuid4()}")
\t\t\t(effects (font (size 1 1) (thickness 0.15)))
\t\t)
\t\t(attr smd)
\t\t(pad "1" smd circle
\t\t\t(at 0 0)
\t\t\t(size 0.8 0.8)
\t\t\t(layers "F.Cu" "F.Mask")
\t\t\t(net "{name}")
\t\t\t(uuid "{uuid.uuid4()}")
\t\t)
\t)''')
    for x1, y1, x2, y2 in [(5, 5, 65, 5), (65, 5, 65, 45), (65, 45, 5, 45), (5, 45, 5, 5)]:
        parts.append(f'''\t(gr_line
\t\t(start {x1} {y1})
\t\t(end {x2} {y2})
\t\t(stroke (width 0.1) (type default))
\t\t(layer "Edge.Cuts")
\t\t(uuid "{uuid.uuid4()}")
\t)''')
    parts.append(")")
    with open(path, "w") as f:
        f.write("\n".join(parts) + "\n")


def _fail(msg):
    print("FAIL  " + msg)
    sys.exit(1)


def _run(args):
    r = subprocess.run([sys.executable] + args, capture_output=True, text=True, cwd=REPO)
    if r.returncode != 0:
        _fail(f"{args[0]} rc={r.returncode}\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    return r.stdout


def _json_summary(out):
    m = re.search(r"JSON_SUMMARY: (\{.*\})", out)
    return json.loads(m.group(1)) if m else {}


def min_riser_pitch(board, net_name, run_dir, min_len=0.2):
    """Min FINITE distance between same-net meander ARMS (#501's guarantee).

    Arms are the risers: segments PERPENDICULAR to the straight run they
    meander (run_dir), at least min_len long. The direction filter is what
    excludes the 45-degree chamfers — at wide pitches their diagonals grow
    long enough to pass any length cutoff and sit ~pitch/sqrt(2) apart, which
    is expected geometry, not an arm pair. Finite segment-to-segment distance
    (not line offset): welded joints measure ~0 (one electrical shape,
    skipped) and near-colinear copper far apart along the trace measures far."""
    from geometry_utils import segment_to_segment_distance
    ux, uy = run_dir
    n = math.hypot(ux, uy)
    ux, uy = ux / n, uy / n
    pcb = parse_kicad_pcb(board)
    nid = {n.name: i for i, n in pcb.nets.items()}[net_name]
    risers = []
    for s in pcb.segments:
        if s.net_id != nid:
            continue
        dx, dy = s.end_x - s.start_x, s.end_y - s.start_y
        ln = math.hypot(dx, dy)
        if ln >= min_len and abs((dx * ux + dy * uy) / ln) < 0.1:
            risers.append(s)
    worst = None
    for i, a in enumerate(risers):
        for b in risers[i + 1:]:
            d = segment_to_segment_distance(a.start_x, a.start_y, a.end_x, a.end_y,
                                            b.start_x, b.start_y, b.end_x, b.end_y)
            if d > 1e-6 and (worst is None or d < worst):
                worst = d
    return worst


def _grade(board, nets_arg=None):
    scope = ["--nets"] + nets_arg if nets_arg else []
    drc = _run(["check_drc.py", board] + scope)
    if "NO DRC VIOLATIONS" not in drc:
        _fail(f"DRC violations on {board}:\n{drc[-1500:]}")
    conn = _run(["check_connected.py", board] + scope)
    if "FULLY CONNECTED" not in conn:
        _fail(f"connectivity failure on {board}:\n{conn[-1500:]}")


def test_synth_demo(tmp, spacing=None, expect_pitch=None):
    """Route the synthetic board (diff chain then SE chain) and grade it."""
    tag = f"sp{spacing}" if spacing else "default"
    src = os.path.join(tmp, f"synth_{tag}.kicad_pcb")
    mid = os.path.join(tmp, f"synth_{tag}_dp.kicad_pcb")
    out = os.path.join(tmp, f"synth_{tag}_out.kicad_pcb")
    write_synth_board(src)
    sp = ["--meander-spacing", str(spacing)] if spacing else []

    dp_out = _run(["route_diff.py", src, mid,
                   "DP_A_P", "DP_A_N", "DP_B_P", "DP_B_N", "DP_C_P", "DP_C_N",
                   "--track-width", "0.2", "--clearance", "0.15", "--diff-pair-gap", "0.15",
                   "--length-match-group", "DP_A_*", "DP_B_*", "DP_C_*"] + sp)
    s = _json_summary(dp_out)
    if s.get("successful") != 3 or s.get("failed"):
        _fail(f"[{tag}] expected 3/3 pairs routed, got {s.get('successful')}/{s.get('failed')}")

    se_out = _run(["route.py", mid, out, "--nets", "SE1", "SE2", "SE3", "SE4",
                   "--track-width", "0.2", "--clearance", "0.15",
                   "--length-match-group", "SE[1-3]"] + sp)
    s = _json_summary(se_out)
    if s.get("successful") != 4:
        _fail(f"[{tag}] expected 4/4 SE nets routed, got {s.get('successful')}")

    _grade(out)

    # The #501 guarantee, measured on the written copper: SE3 (the shortest net,
    # the most meandered) must keep its arms >= the demanded pitch apart.
    width = 0.2
    want = expect_pitch if expect_pitch else 2.0 * width
    pitch = min_riser_pitch(out, "SE3", run_dir=(1, 0))  # SE3 runs horizontally
    if pitch is None:
        _fail(f"[{tag}] SE3 has no meander risers at all")
    if pitch < want - 1e-6:
        _fail(f"[{tag}] SE3 arm pitch {pitch:.3f} < required {want:.3f}")
    # DP_B's legs are regenerated from the meandered centerline; its arms must
    # also never sit tighter than the SE guarantee. The grid router lays its
    # slanted connection as horizontal runs + 45-degree jogs, so the meandered
    # straight run is horizontal and the risers are vertical.
    dpp = min_riser_pitch(out, "DP_B_P", run_dir=(1, 0))
    if dpp is None:
        _fail(f"[{tag}] DP_B_P has no meander risers (centerline meanders missing)")
    if dpp < 2.0 * width - 1e-6:
        _fail(f"[{tag}] DP_B_P arm pitch {dpp:.3f} < {2.0 * width:.3f}")
    # DP_C is MULTIPOINT (two 20mm legs, span 20mm vs the 40mm target): the
    # group pass must meander its longest MST leg (#520).
    dpc = min_riser_pitch(out, "DP_C_P", run_dir=(1, 0))
    if dpc is None:
        _fail(f"[{tag}] DP_C_P has no meander risers (multipoint span not meandered)")
    if dpc < 2.0 * width - 1e-6:
        _fail(f"[{tag}] DP_C_P arm pitch {dpc:.3f} < {2.0 * width:.3f}")
    print(f"PASS  synth [{tag}]: 3/3 pairs + 3/3 SE, DRC-clean, connected; "
          f"SE3 pitch {pitch:.3f} DP_B_P pitch {dpp:.3f} DP_C_P pitch {dpc:.3f} (>= {want:.2f})")


def test_lvds_intra_pair(tmp):
    """Intra-pair P/N matching on the bundled lvds_converter board (SE-style
    meanders on one leg of a coupled pair, at the board's own 0.2 netclass)."""
    src = os.path.join(REPO, "kicad_files", "lvds_converter_dualclk.kicad_pcb")
    out = os.path.join(tmp, "lvds_intra.kicad_pcb")
    log = _run(["route_diff.py", src, out,
                "--diff-pair-gap", "0.25", "--track-width", "0.2", "--clearance", "0.2",
                "--diff-pair-intra-match"])
    m = re.search(r"intra-pair: (\d+) bumps added, new P=[\d.]+mm, new delta=([\d.]+)mm", log)
    if not m:
        _fail("intra-pair pass added no bumps on lvds_converter (expected /CLK leg meander)")
    bumps, delta = int(m.group(1)), float(m.group(2))
    if bumps < 1 or delta > 0.1:
        _fail(f"intra-pair match ineffective: {bumps} bumps, residual delta {delta}")
    print(f"PASS  lvds intra-pair: {bumps} bump(s), residual P/N delta {delta:.3f}mm")


def _net_len(board, name):
    from net_queries import net_copper_lengths
    pcb = parse_kicad_pcb(board)
    nid = {n.name: i for i, n in pcb.nets.items()}[name]
    return net_copper_lengths(pcb, [nid])[nid]


def test_protected_nets(tmp):
    """#521: matched groups and routed pairs are recorded as protected in the
    sibling .kicad_pro; a later step's glob rip skips them, an exact-name rip
    overrides."""
    src = os.path.join(tmp, "prot.kicad_pcb")
    s1 = os.path.join(tmp, "prot_s1.kicad_pcb")
    s2 = os.path.join(tmp, "prot_s2.kicad_pcb")
    s3 = os.path.join(tmp, "prot_s3.kicad_pcb")
    s4 = os.path.join(tmp, "prot_s4.kicad_pcb")
    write_synth_board(src)
    _run(["route_diff.py", src, s1,
          "DP_A_P", "DP_A_N", "DP_B_P", "DP_B_N", "DP_C_P", "DP_C_N",
          "--track-width", "0.2", "--clearance", "0.15", "--diff-pair-gap", "0.15",
          "--length-match-group", "DP_A_*", "DP_B_*", "DP_C_*"])
    # SE1 stays unrouted here: the attack step below needs a real routing
    # target or the run no-ops before the rip expansion.
    _run(["route.py", s1, s2, "--nets", "SE2", "SE3",
          "--track-width", "0.2", "--clearance", "0.15", "--length-match-group", "SE*"])

    from protected_nets import read_protected_nets, pro_path_for_board
    prot = read_protected_nets(pro_path_for_board(s2))
    for n in ("DP_A_P", "DP_A_N", "DP_C_N", "SE2", "SE3"):
        if n not in prot:
            _fail(f"{n} missing from protected nets after the chain: {prot}")
    if prot.get("SE3") != "length-matched" or prot.get("DP_A_P") != "diff-pair":
        _fail(f"unexpected protection reasons: {prot}")

    # Attack step: route the fresh SE1 with a glob rip over everything.
    # Protected nets must survive with their meandered lengths intact.
    before = {n: _net_len(s2, n) for n in ("SE2", "SE3", "DP_B_P", "DP_C_P")}
    out = _run(["route.py", s2, s3, "--nets", "SE1", "--rip-existing-nets", "*",
                "--track-width", "0.2", "--clearance", "0.15"])
    if "PROTECTED net(s) excluded" not in out:
        _fail("glob rip printed no protected-nets exclusion")
    if "eligible for rip-up" in out:
        _fail("glob rip still made protected nets rip-eligible:\n" + out[-1200:])
    for n, lb in before.items():
        la = _net_len(s3, n)
        if abs(la - lb) > 1e-6:
            _fail(f"protected {n} changed under glob rip: {lb:.3f} -> {la:.3f}")

    # Override: an exact (non-glob) name stays RIP-ELIGIBLE despite protection
    # (whether it is actually ripped depends on congestion, so assert the
    # policy decision -- the eligibility print -- not the rip). SE4 is the
    # fresh routing target (SE1 was consumed by the attack step above).
    out = _run(["route.py", s3, s4, "--nets", "SE4", "--rip-existing-nets", "SE3",
                "--track-width", "0.2", "--clearance", "0.15"])
    if "eligible for rip-up" not in out or "SE3" not in out.split("eligible for rip-up")[1][:80]:
        _fail("exact-name override did not make SE3 rip-eligible:\n" + out[-1200:])
    if "PROTECTED net(s) excluded" in out:
        _fail("exact-name override still reported SE3 as excluded")
    print(f"PASS  protected nets: {len(prot)} recorded; glob rip skipped them "
          f"(lengths held); exact-name override made SE3 rip-eligible")


def test_power_trace_ampacity(tmp):
    """#487: power nets get a trace-side ampacity report (bottleneck segment
    at the stackup copper weight), printed AND carried in JSON_SUMMARY."""
    src = os.path.join(tmp, "amp.kicad_pcb")
    out = os.path.join(tmp, "amp_out.kicad_pcb")
    write_synth_board(src)
    log = _run(["route.py", src, out, "--nets", "SE1", "SE2",
                "--power-nets", "SE1", "--power-nets-widths", "0.8",
                "--track-width", "0.2", "--clearance", "0.15"])
    if "Power trace ampacity" not in log:
        _fail("no trace-ampacity report printed")
    s = _json_summary(log)
    amp = {e["net"]: e for e in s.get("power_trace_ampacity", [])}
    if "SE1" not in amp:
        _fail(f"SE1 missing from power_trace_ampacity: {s.get('power_trace_ampacity')}")
    e = amp["SE1"]
    if abs(e["bottleneck_width_mm"] - 0.8) > 1e-3 or e["max_current_ipc2152_a"] <= 0:
        _fail(f"implausible ampacity entry: {e}")
    print(f"PASS  power trace ampacity: SE1 {e['max_current_ipc2152_a']}A at "
          f"{e['bottleneck_width_mm']}mm / {e['copper_oz']:.0f}oz in JSON_SUMMARY")


def test_locked_nets(tmp):
    """KiCad-locked copper: the net is never rip-eligible, even named exactly."""
    src = os.path.join(tmp, "lock.kicad_pcb")
    s1 = os.path.join(tmp, "lock_s1.kicad_pcb")
    s2 = os.path.join(tmp, "lock_s2.kicad_pcb")
    write_synth_board(src)
    _run(["route.py", src, s1, "--nets", "SE2", "SE3",
          "--track-width", "0.2", "--clearance", "0.15"])
    # Lock SE2's copper the way KiCad writes it (token between layer and net).
    txt = open(s1).read()
    locked = txt.replace('(net "SE2")', '(locked yes)\n\t\t(net "SE2")')
    if locked == txt:
        _fail("could not inject (locked yes) into SE2 segments")
    open(s1, "w").write(locked)

    before = _net_len(s1, "SE2")
    out = _run(["route.py", s1, s2, "--nets", "SE1", "--rip-existing-nets", "SE2",
                "--track-width", "0.2", "--clearance", "0.15"])
    if "PROTECTED net(s) excluded" not in out or "locked" not in out:
        _fail("locked net was not excluded from an exact-name rip:\n" + out[-1200:])
    if "eligible for rip-up" in out:
        _fail("locked net became rip-eligible despite lock")
    after = _net_len(s2, "SE2")
    if abs(after - before) > 1e-6:
        _fail(f"locked SE2 changed: {before:.3f} -> {after:.3f}")
    print("PASS  locked nets: exact-name rip refused, copper untouched")


def test_impedance_redo(tmp):
    """#521: --impedance records the declaration in .kicad_pro; a later step
    rerouting the net WITHOUT --impedance recomputes the same widths."""
    src = os.path.join(tmp, "imp.kicad_pcb")
    s1 = os.path.join(tmp, "imp_s1.kicad_pcb")
    s1b = os.path.join(tmp, "imp_s1_stripped.kicad_pcb")
    s2 = os.path.join(tmp, "imp_s2.kicad_pcb")
    write_synth_board(src)
    out = _run(["route.py", src, s1, "--nets", "SE2",
                "--track-width", "0.15", "--clearance", "0.15", "--impedance", "60"])
    pcb = parse_kicad_pcb(s1)
    nid = {n.name: i for i, n in pcb.nets.items()}["SE2"]
    w1 = max(s.width for s in pcb.segments if s.net_id == nid)
    if abs(w1 - 0.15) < 1e-9:
        _fail("impedance step routed at the fallback width; stackup not read?")

    from protected_nets import read_impedance_specs, pro_path_for_board
    spec = read_impedance_specs(pro_path_for_board(s1)).get("SE2")
    if not spec or spec.get("ohms") != 60:
        _fail(f"impedance spec not recorded: {spec}")

    # Strip SE2's copper (simulating a rip) and reroute WITHOUT --impedance.
    from kicad_writer import remove_segments_from_content, remove_vias_from_content
    content = open(s1).read()
    content, _ = remove_segments_from_content(
        content, [s for s in pcb.segments if s.net_id == nid])
    content, _ = remove_vias_from_content(
        content, [v for v in pcb.vias if v.net_id == nid])
    open(s1b, "w").write(content)
    import shutil
    shutil.copy(pro_path_for_board(s1), pro_path_for_board(s1b))
    out = _run(["route.py", s1b, s2, "--nets", "SE2",
                "--track-width", "0.15", "--clearance", "0.15"])
    if "Reapplying stored 60 ohm" not in out:
        _fail("redo step did not reapply the stored impedance spec:\n" + out[-1200:])
    pcb2 = parse_kicad_pcb(s2)
    nid2 = {n.name: i for i, n in pcb2.nets.items()}["SE2"]
    w2 = max(s.width for s in pcb2.segments if s.net_id == nid2)
    if abs(w2 - w1) > 1e-6:
        _fail(f"redo width {w2:.4f} != impedance width {w1:.4f}")
    print(f"PASS  impedance redo: 60 ohm spec persisted; reroute without "
          f"--impedance came back at {w2:.3f}mm (not the 0.15 default)")

    # check_impedance auto-reads the stored declarations (#521): SE2's entry
    # (gap 0 = declared non-coplanar) is picked up with no flags at all.
    out = _run(["check_impedance.py", s2, "--exit-zero"])
    if "Auto-read 1 net impedance declaration(s)" not in out:
        _fail("check_impedance did not auto-read the stored spec:\n" + out[:1200])

    # Coplanar declaration: route SE3 as a declared CPW (gap 0.3). The board
    # has no pour, so auditing SE3 at ITS recorded gap -- no --coplanar-gap
    # flag passed -- must flag the broken promise (exit 1).
    s3 = os.path.join(tmp, "imp_s3.kicad_pcb")
    _run(["route.py", s2, s3, "--nets", "SE3", "--track-width", "0.15",
          "--clearance", "0.15", "--impedance", "60", "--coplanar-gap", "0.3"])
    r = subprocess.run([sys.executable, "check_impedance.py", s3, "--nets", "SE3"],
                       capture_output=True, text=True, cwd=REPO)
    if "(1 coplanar)" not in r.stdout:
        _fail("stored coplanar declaration not auto-read:\n" + r.stdout[:1200])
    if r.returncode != 1:
        _fail(f"declared-coplanar net over no pour should exit 1 without any "
              f"--coplanar-gap flag (rc={r.returncode}):\n{r.stdout[-1200:]}")
    print("PASS  check_impedance audits per-net stored declarations "
          "(coplanar promise flagged with no flags passed)")


def main():
    tmp = tempfile.mkdtemp(prefix="meander_demo_")
    test_synth_demo(tmp)
    # Non-default spacing must reach the geometry: 3W on a 0.2 track = 0.6mm.
    test_synth_demo(tmp, spacing=3.0, expect_pitch=0.6)
    test_lvds_intra_pair(tmp)
    test_protected_nets(tmp)
    test_locked_nets(tmp)
    test_impedance_redo(tmp)
    test_power_trace_ampacity(tmp)
    print("PASS  all meander demo chains")


if __name__ == "__main__":
    main()
