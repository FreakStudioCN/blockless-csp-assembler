import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "kicad-host-mvp"
SCRIPTS = ROOT / "scripts"
TOOLS = REPO / "tools" / "KiCadRoutingTools"
DEPS = REPO / "tools" / "kicad-python-deps"
KRT_REVISION = "efa553532d422fb455d2f53c15539f0d813fd32f"


def first_existing(candidates):
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def find_kicad_tools():
    cli = first_existing([os.environ.get("KICAD_CLI")])
    kicad_python = first_existing([os.environ.get("KICAD_PYTHON")])
    if platform.system() == "Windows":
        program_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
        bases = [program_files / "KiCad" / version / "bin" for version in ("10.0", "9.0", "8.0")]
        cli = cli or first_existing(base / "kicad-cli.exe" for base in bases)
        kicad_python = kicad_python or first_existing(base / "python.exe" for base in bases)
    elif platform.system() == "Darwin":
        app = Path("/Applications/KiCad/KiCad.app/Contents")
        cli = cli or first_existing([app / "MacOS" / "kicad-cli", shutil.which("kicad-cli")])
        candidates = [app / "Frameworks" / "Python.framework" / "Versions" / "Current" / "bin" / "python3"]
        versions = app / "Frameworks" / "Python.framework" / "Versions"
        if versions.is_dir():
            candidates.extend(sorted(versions.glob("*/bin/python3"), reverse=True))
        kicad_python = kicad_python or first_existing(candidates)
    cli = cli or first_existing([shutil.which("kicad-cli")])
    if not kicad_python:
        raise SystemExit("KiCad Python was not found. Install KiCad 10 or set KICAD_PYTHON.")
    return kicad_python, cli


def run(command, *, env=None, log=None):
    command = [str(value) for value in command]
    print("> " + " ".join(command))
    if log:
        completed = subprocess.run(command, cwd=REPO, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(completed.stdout, encoding="utf-8")
        print(completed.stdout, end="")
        if completed.returncode:
            raise SystemExit(f"Command failed ({completed.returncode}). Log: {log}")
        return
    subprocess.run(command, cwd=REPO, env=env, check=True)


def setup_router():
    kicad_python, _ = find_kicad_tools()
    if not shutil.which("git"):
        raise SystemExit("Git is required to download KiCadRoutingTools.")
    if not TOOLS.exists():
        TOOLS.parent.mkdir(parents=True, exist_ok=True)
        print("[1/3] Download pinned KiCadRoutingTools revision")
        run(["git", "init", TOOLS])
        run(["git", "-C", TOOLS, "remote", "add", "origin", "https://github.com/drandyhaas/KiCadRoutingTools.git"])
        run(["git", "-C", TOOLS, "fetch", "--depth", "1", "origin", KRT_REVISION])
        run(["git", "-C", TOOLS, "checkout", "--detach", "FETCH_HEAD"])
    else:
        revision = subprocess.check_output(["git", "-C", str(TOOLS), "rev-parse", "HEAD"], text=True).strip()
        if revision != KRT_REVISION:
            raise SystemExit(f"tools/KiCadRoutingTools is not pinned to {KRT_REVISION}. Remove it and run setup again.")
        print("[1/3] Pinned KiCadRoutingTools revision is already present")
    DEPS.mkdir(parents=True, exist_ok=True)
    print("[2/3] Install Python dependencies")
    run([kicad_python, "-m", "pip", "install", "--upgrade", "--target", DEPS, "-r", TOOLS / "requirements.txt"])
    print("[3/3] Build the Rust routing extension")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(DEPS), str(TOOLS), str(TOOLS / "rust_router")])
    run([kicad_python, TOOLS / "build_router.py"], env=env)
    print("KiCadRoutingTools is ready.")


def build_boards():
    kicad_python, _ = find_kicad_tools()
    run([kicad_python, SCRIPTS / "build_boards_with_kicad_python.py"])


def route_boards(cases):
    kicad_python, cli = find_kicad_tools()
    if not cli:
        raise SystemExit("KiCad CLI was not found. Install KiCad 10 or set KICAD_CLI.")
    if not (TOOLS / "route.py").exists():
        raise SystemExit("KiCadRoutingTools is missing. Run npm run kicad:setup-router first.")
    configs = {
        "long-range-env-node": (["5V", "3V3", "BAT+", "GND"], ["0.6", "0.45", "0.8", "0.7"]),
        "desktop-air-monitor": (["5V", "3V3", "GND"], ["0.6", "0.45", "0.7"]),
    }
    unknown = sorted(set(cases) - set(configs))
    if unknown:
        raise SystemExit(f"Unknown case: {', '.join(unknown)}")

    input_dir = ROOT / "generated" / "external-router-input"
    output_dir = ROOT / "generated" / "external-router-output"
    report_dir = ROOT / "reports"
    svg_dir = ROOT / "generated" / "kicad-svg"
    web_render_dir = REPO / "public" / "assets" / "kicad-renders"
    web_svg_dir = REPO / "public" / "assets" / "kicad-svg"
    for directory in (input_dir, output_dir, report_dir, svg_dir, web_render_dir, web_svg_dir):
        directory.mkdir(parents=True, exist_ok=True)

    print("[1/5] Generate and validate plans")
    run([sys.executable, SCRIPTS / "generate_plan.py"])
    run([sys.executable, SCRIPTS / "validate_plans.py"])
    print("[2/5] Build unrouted KiCad boards")
    run([kicad_python, SCRIPTS / "build_boards_with_kicad_python.py", "--unrouted", "--output-dir", input_dir, "--suffix=-unrouted"])

    for name in cases:
        power_nets, power_widths = configs[name]
        input_pcb = input_dir / f"{name}-unrouted.kicad_pcb"
        output_pcb = output_dir / f"{name}-krt-routed.kicad_pcb"
        route_log = report_dir / f"{name}-krt-route.log"
        drc_report = report_dir / f"{name}-krt-drc.rpt"
        render_png = ROOT / "generated" / f"{name}-krt-render.png"
        svg_top = svg_dir / f"{name}-krt-routed-fcu.svg"
        svg_bottom = svg_dir / f"{name}-krt-routed-bcu.svg"

        print(f"[3/5] Route {name}")
        route_args = [
            kicad_python, SCRIPTS / "run_kicad_routing_tools.py", input_pcb, output_pcb,
            "--nets", "*", "--layers", "F.Cu", "B.Cu", "--track-width", "0.25",
            "--clearance", "0.2", "--via-size", "0.75", "--via-drill", "0.35",
            "--ordering", "mps", "--max-iterations", "10000", "--stats",
            "--power-nets", *power_nets, "--power-nets-widths", *power_widths,
        ]
        run(route_args, log=route_log)

        print(f"[4/5] DRC, render, and export {name}")
        run([kicad_python, SCRIPTS / "polish_kicad_output.py", output_pcb])
        run([sys.executable, SCRIPTS / "repair_kicad_model_paths.py", output_pcb])
        run([cli, "pcb", "drc", "--output", drc_report, "--format", "report", "--severity-all", output_pcb])
        run([cli, "pcb", "render", "--output", render_png, "--width", "1366", "--height", "768", "--quality", "high", "--background", "opaque", "--perspective", "--rotate", "-55,0,38", "--zoom", "0.78", output_pcb])
        shutil.copy2(render_png, web_render_dir / render_png.name)
        for svg, layers, mirror in ((svg_top, "F.Cu,F.SilkS,Edge.Cuts", False), (svg_bottom, "B.Cu,B.SilkS,Edge.Cuts", True)):
            command = [cli, "pcb", "export", "svg", "--output", svg, "--mode-single"]
            if mirror:
                command.append("--mirror")
            command.extend(["--layers", layers, "--page-size-mode", "2", "--fit-page-to-board", "--exclude-drawing-sheet", output_pcb])
            run(command)
        run([sys.executable, SCRIPTS / "enhance_kicad_svg.py", svg_top, svg_bottom])
        shutil.copy2(svg_top, svg_dir / f"{name}-krt-routed.svg")
        shutil.copy2(svg_top, web_svg_dir / f"{name}-krt-routed.svg")
        shutil.copy2(svg_top, web_svg_dir / svg_top.name)
        shutil.copy2(svg_bottom, web_svg_dir / svg_bottom.name)
        print(f"[5/5] Done {name}")


def main():
    parser = argparse.ArgumentParser(description="Cross-platform Blockless KiCad host commands.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    commands.add_parser("setup-router")
    commands.add_parser("build")
    route = commands.add_parser("route")
    route.add_argument("--case", action="append", dest="cases")
    args = parser.parse_args()

    if args.command == "check":
        kicad_python, cli = find_kicad_tools()
        print(f"KiCad Python: {kicad_python}")
        print(f"KiCad CLI: {cli or 'not found'}")
    elif args.command == "setup-router":
        setup_router()
    elif args.command == "build":
        build_boards()
    else:
        route_boards(args.cases or ["long-range-env-node", "desktop-air-monitor"])


if __name__ == "__main__":
    main()
