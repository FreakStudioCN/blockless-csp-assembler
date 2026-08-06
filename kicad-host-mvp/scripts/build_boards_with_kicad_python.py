import json
import argparse
import os
import sys
from pathlib import Path

import pcbnew


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
PLUGIN = ROOT / "plugin" / "ai_modular_backplane"
OUTPUT = ROOT / "generated"

sys.path.insert(0, str(PLUGIN))

from board_builder import build_board_from_plan  # noqa: E402


def resolve_plan_asset_path(filename):
    path = Path(filename)
    if path.is_absolute():
        return path

    candidates = [
        OUTPUT / filename,
        REPO / filename,
        ROOT / filename,
        Path.cwd() / filename,
    ]
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.exists():
            return resolved
    return (OUTPUT / filename).resolve()


def rewrite_plan_cad_paths_to_absolute(plan):
    for module in plan.get("modules", []):
        cad = module.get("cad")
        if not isinstance(cad, dict):
            continue
        for key in ("presentation", "stl", "step"):
            filename = cad.get(key)
            if filename:
                cad[key] = str(resolve_plan_asset_path(filename)).replace("\\", "/")


def relativize_board_model_paths(board, board_path):
    board_dir = board_path.parent.resolve()
    for footprint in board.GetFootprints():
        for model in footprint.Models():
            filename = Path(model.m_Filename)
            if filename.is_absolute():
                model.m_Filename = os.path.relpath(filename, board_dir).replace("\\", "/")


def build(plan_path, route_internal=True, output_dir=OUTPUT, suffix=""):
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    output_dir = Path(output_dir)
    rewrite_plan_cad_paths_to_absolute(plan)
    board = pcbnew.BOARD()
    build_board_from_plan(board, plan, route_internal=route_internal)

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{plan_path.stem}{suffix}.kicad_pcb"
    relativize_board_model_paths(board, out_path)
    pcbnew.SaveBoard(str(out_path), board)
    print(out_path.name)


def main():
    parser = argparse.ArgumentParser(description="Build KiCad boards from AI module plans.")
    parser.add_argument("--unrouted", action="store_true", help="Create boards with footprints/netlist only.")
    parser.add_argument("--output-dir", default=str(OUTPUT), help="Output directory.")
    parser.add_argument("--suffix", default="", help="Filename suffix before .kicad_pcb.")
    args = parser.parse_args()

    plan_paths = sorted((ROOT / "plans").glob("*.json"))
    if not plan_paths:
        raise SystemExit("No plan JSON files found.")
    for plan_path in plan_paths:
        build(
            plan_path,
            route_internal=not args.unrouted,
            output_dir=Path(args.output_dir),
            suffix=args.suffix,
        )


if __name__ == "__main__":
    main()
