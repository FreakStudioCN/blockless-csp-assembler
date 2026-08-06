import argparse
import os
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
GENERATED = ROOT / "generated"
REAL_CAD = REPO / "public" / "assets" / "real-cad"
PRESENTATION_CAD = ROOT / "assets" / "presentation-cad"
MODEL_RE = re.compile(r'(\(model\s+")([^"]+)(")')


def resolve_model_path(board_dir, filename):
    path = Path(filename)
    if path.is_absolute() and path.exists():
        return path.resolve()

    candidates = [
        board_dir / filename,
        GENERATED / filename,
        REPO / filename,
        ROOT / filename,
        REAL_CAD / Path(filename).name,
        PRESENTATION_CAD / Path(filename).name,
    ]
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.exists():
            return resolved
    return None


def repair_board(board_path):
    board_path = Path(board_path).resolve()
    board_dir = board_path.parent
    changed = 0
    text = board_path.read_text(encoding="utf-8")

    def replace(match):
        nonlocal changed
        prefix, filename, suffix = match.groups()
        resolved = resolve_model_path(board_dir, filename)
        if not resolved:
            return match.group(0)

        next_filename = os.path.relpath(resolved, board_dir).replace("\\", "/")
        if next_filename != filename:
            changed += 1
        return f"{prefix}{next_filename}{suffix}"

    next_text = MODEL_RE.sub(replace, text)

    if changed:
        board_path.write_text(next_text, encoding="utf-8", newline="")
    print(f"{board_path.name}: repaired {changed} model path(s)")


def main():
    parser = argparse.ArgumentParser(description="Repair KiCad 3D model paths after moving/routing boards.")
    parser.add_argument("boards", nargs="+", help="One or more .kicad_pcb files.")
    args = parser.parse_args()

    for board in args.boards:
        repair_board(board)


if __name__ == "__main__":
    main()
