import argparse
import json
import math
from pathlib import Path

import pcbnew


ROOT = Path(__file__).resolve().parents[1]
MM = 1_000_000


def mm(value):
    return int(round(value * MM))


def wxpoint(x, y):
    return pcbnew.VECTOR2I(mm(x), mm(y))


def case_name_from_board(board_path):
    stem = board_path.stem
    for suffix in ("-krt-routed", "-unrouted", "-presentation"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def load_module_specs(board_path):
    plan_path = ROOT / "plans" / f"{case_name_from_board(board_path)}.json"
    if not plan_path.exists():
        return {}
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    return {
        module["id"]: {
            "size_mm": module["size_mm"],
            "placement": module["placement"],
        }
        for module in plan.get("modules", [])
    }


def rotate_point(x, y, deg):
    angle = math.radians(deg)
    return x * math.cos(angle) - y * math.sin(angle), x * math.sin(angle) + y * math.cos(angle)


def add_footprint_segment(footprint, layer, start, end, width_mm):
    shape = pcbnew.PCB_SHAPE(footprint)
    shape.SetShape(pcbnew.SHAPE_T_SEGMENT)
    shape.SetStart(wxpoint(*start))
    shape.SetEnd(wxpoint(*end))
    shape.SetWidth(mm(width_mm))
    shape.SetLayer(layer)
    footprint.Add(shape)


def add_footprint_rect(footprint, layer, width, height, line_width=0.05, margin=0.0, origin=(0, 0), rotation=0):
    half_w = width / 2 + margin
    half_h = height / 2 + margin
    corners = [(-half_w, -half_h), (half_w, -half_h), (half_w, half_h), (-half_w, half_h), (-half_w, -half_h)]
    for start, end in zip(corners, corners[1:]):
        sx, sy = rotate_point(*start, rotation)
        ex, ey = rotate_point(*end, rotation)
        add_footprint_segment(footprint, layer, (origin[0] + sx, origin[1] + sy), (origin[0] + ex, origin[1] + ey), line_width)


def refresh_courtyard(footprint, module_spec):
    for item in list(footprint.GraphicalItems()):
        if isinstance(item, pcbnew.PCB_SHAPE) and item.GetLayer() in (pcbnew.F_CrtYd, pcbnew.F_Fab):
            item.SetLayer(pcbnew.Dwgs_User)
    size_mm = module_spec["size_mm"]
    x, y, rotation = module_spec["placement"]
    add_footprint_rect(footprint, pcbnew.F_CrtYd, size_mm[0], size_mm[1], line_width=0.05, margin=1.0, origin=(x, y), rotation=rotation)
    add_footprint_rect(footprint, pcbnew.F_Fab, size_mm[0], size_mm[1], line_width=0.08, margin=0.0, origin=(x, y), rotation=rotation)


def enforce_mechanical_drc(project_path):
    if not project_path.exists():
        return False
    data = json.loads(project_path.read_text(encoding="utf-8"))
    severities = data.setdefault("board", {}).setdefault("design_settings", {}).setdefault("rule_severities", {})
    for key in ("courtyards_overlap", "malformed_courtyard", "missing_courtyard", "npth_inside_courtyard", "pth_inside_courtyard"):
        severities[key] = "error"
    project_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def polish_board(board_path):
    board = pcbnew.LoadBoard(str(board_path))
    module_specs = load_module_specs(board_path)
    moved_text = 0
    courtyards = 0

    for drawing in list(board.GetDrawings()):
        if isinstance(drawing, pcbnew.PCB_TEXT) and drawing.GetLayer() == pcbnew.F_SilkS:
            drawing.SetLayer(pcbnew.Dwgs_User)
            moved_text += 1

    hidden_fields = 0
    for footprint in list(board.GetFootprints()):
        module_spec = module_specs.get(footprint.GetReference())
        if module_spec:
            refresh_courtyard(footprint, module_spec)
            courtyards += 1
        for field in (footprint.Reference(), footprint.Value()):
            if hasattr(field, "IsVisible") and field.IsVisible():
                field.SetVisible(False)
                hidden_fields += 1

    pcbnew.SaveBoard(str(board_path), board)
    project_updated = enforce_mechanical_drc(board_path.with_suffix(".kicad_pro"))
    print(
        f"{board_path.name}: moved {moved_text} F.SilkS text items, "
        f"hid {hidden_fields} footprint fields, refreshed {courtyards} courtyards, "
        f"mechanical DRC {'enabled' if project_updated else 'not updated'}"
    )


def main():
    parser = argparse.ArgumentParser(description="Polish generated KiCad demo outputs without changing routed copper.")
    parser.add_argument("boards", nargs="+", type=Path)
    args = parser.parse_args()

    for board_path in args.boards:
        if not board_path.exists():
            raise SystemExit(f"Board not found: {board_path}")
        polish_board(board_path)


if __name__ == "__main__":
    main()
