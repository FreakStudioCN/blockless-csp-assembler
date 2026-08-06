import math
import heapq
import struct
from pathlib import Path

import pcbnew


MM = 1_000_000


def mm(value):
    return int(round(value * MM))


def wxpoint(x, y):
    return pcbnew.VECTOR2I(mm(x), mm(y))


def add_segment(board, layer, start, end, width_mm, net=None):
    if layer not in (pcbnew.F_Cu, pcbnew.B_Cu):
        shape = pcbnew.PCB_SHAPE(board)
        shape.SetShape(pcbnew.SHAPE_T_SEGMENT)
        shape.SetStart(wxpoint(*start))
        shape.SetEnd(wxpoint(*end))
        shape.SetWidth(mm(width_mm))
        shape.SetLayer(layer)
        board.Add(shape)
        return shape

    segment = pcbnew.PCB_TRACK(board)
    segment.SetStart(wxpoint(*start))
    segment.SetEnd(wxpoint(*end))
    segment.SetWidth(mm(width_mm))
    segment.SetLayer(layer)
    if net is not None:
        segment.SetNet(net)
    board.Add(segment)
    return segment


def add_footprint_segment(footprint, layer, start, end, width_mm):
    shape = pcbnew.PCB_SHAPE(footprint)
    shape.SetShape(pcbnew.SHAPE_T_SEGMENT)
    shape.SetStart(wxpoint(*start))
    shape.SetEnd(wxpoint(*end))
    shape.SetWidth(mm(width_mm))
    shape.SetLayer(layer)
    footprint.Add(shape)
    return shape


def add_footprint_rect(footprint, layer, width, height, line_width=0.05, margin=0.0, origin=(0, 0), rotation=0):
    half_w = width / 2 + margin
    half_h = height / 2 + margin
    corners = [(-half_w, -half_h), (half_w, -half_h), (half_w, half_h), (-half_w, half_h), (-half_w, -half_h)]
    for start, end in zip(corners, corners[1:]):
        sx, sy = rotate_point(*start, rotation)
        ex, ey = rotate_point(*end, rotation)
        add_footprint_segment(footprint, layer, (origin[0] + sx, origin[1] + sy), (origin[0] + ex, origin[1] + ey), line_width)


def add_via(board, point, net=None):
    via = pcbnew.PCB_VIA(board)
    via.SetPosition(wxpoint(*point))
    via.SetDrill(mm(0.35))
    via.SetWidth(mm(0.75))
    via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    if net is not None:
        via.SetNet(net)
    board.Add(via)
    return via


def get_or_create_net(board, name):
    existing = board.FindNet(name)
    if existing:
        return existing
    net = pcbnew.NETINFO_ITEM(board, name)
    board.Add(net)
    return net


def add_text(board, text, x, y, size=1.2, layer=pcbnew.F_SilkS):
    item = pcbnew.PCB_TEXT(board)
    item.SetText(text)
    item.SetPosition(wxpoint(x, y))
    item.SetTextSize(pcbnew.VECTOR2I(mm(size), mm(size)))
    item.SetLayer(layer)
    board.Add(item)
    return item


def hide_footprint_texts(footprint):
    footprint.Reference().SetVisible(False)
    footprint.Value().SetVisible(False)


def add_outline(board, width, height):
    add_segment(board, pcbnew.Edge_Cuts, (0, 0), (width, 0), 0.1)
    add_segment(board, pcbnew.Edge_Cuts, (width, 0), (width, height), 0.1)
    add_segment(board, pcbnew.Edge_Cuts, (width, height), (0, height), 0.1)
    add_segment(board, pcbnew.Edge_Cuts, (0, height), (0, 0), 0.1)


def module_pin_xy(module, pin_name):
    pins = module.get("pins", [])
    index = pins.index(pin_name) if pin_name in pins else 0
    width, height = module["size_mm"]
    count = max(1, len(pins))
    margin = min(2.2, max(0.8, height * 0.18))
    usable = max(0.5, height - 2 * margin)
    y = -height / 2 + margin if count == 1 else -height / 2 + margin + index * usable / (count - 1)
    if pin_name in {"VIN", "VCC", "OUT+", "B+", "3V3"}:
        return -width / 2, y
    if pin_name in {"GND", "OUT-", "B-"}:
        return -width / 2, y
    return width / 2, y


def rotate_point(x, y, deg):
    angle = math.radians(deg)
    return x * math.cos(angle) - y * math.sin(angle), x * math.sin(angle) + y * math.cos(angle)


def resolve_model_path(filename):
    path = Path(filename)
    if path.is_absolute():
        return path
    generated_dir = Path(__file__).resolve().parents[2] / "generated"
    return (generated_dir / filename).resolve()


def read_binary_stl_bounds(filename):
    path = resolve_model_path(filename)
    if not path.exists():
        return None

    data = path.read_bytes()
    if len(data) < 84:
        return None

    triangle_count = struct.unpack_from("<I", data, 80)[0]
    expected_len = 84 + triangle_count * 50
    if expected_len > len(data):
        return None

    mins = [float("inf"), float("inf"), float("inf")]
    maxs = [float("-inf"), float("-inf"), float("-inf")]
    offset = 84
    for _ in range(triangle_count):
        offset += 12
        for _vertex in range(3):
            x, y, z = struct.unpack_from("<fff", data, offset)
            offset += 12
            mins[0] = min(mins[0], x)
            mins[1] = min(mins[1], y)
            mins[2] = min(mins[2], z)
            maxs[0] = max(maxs[0], x)
            maxs[1] = max(maxs[1], y)
            maxs[2] = max(maxs[2], z)
        offset += 2

    if not all(math.isfinite(value) for value in mins + maxs):
        return None

    return mins, maxs


def transformed_bounds(bounds, scale, rotation_z):
    mins, maxs = bounds
    angle = math.radians(rotation_z)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    transformed = []
    for x in (mins[0], maxs[0]):
        for y in (mins[1], maxs[1]):
            for z in (mins[2], maxs[2]):
                sx = x * scale
                sy = y * scale
                sz = z * scale
                transformed.append((sx * cos_a - sy * sin_a, sx * sin_a + sy * cos_a, sz))

    out_mins = [min(point[index] for point in transformed) for index in range(3)]
    out_maxs = [max(point[index] for point in transformed) for index in range(3)]
    return out_mins, out_maxs


def add_3d_model(footprint, module):
    cad = module.get("cad") or {}
    model_filename = cad.get("presentation") or cad.get("step") or cad.get("stl")
    if not model_filename:
        return

    width, height = module["size_mm"]
    rotation_z = 0
    scale = 1.0
    offset_x = 0
    offset_y = 0
    offset_z = 0

    if cad.get("presentation"):
        model = pcbnew.FP_3DMODEL()
        model.m_Filename = model_filename.replace("\\", "/")
        model.m_Scale = pcbnew.VECTOR3D(1, 1, 1)
        model.m_Rotation = pcbnew.VECTOR3D(0, 0, 0)
        model.m_Offset = pcbnew.VECTOR3D(0, 0, 0)
        model.m_Show = True
        model.m_Opacity = 1.0
        footprint.Add3DModel(model)
        return

    bounds = read_binary_stl_bounds(cad.get("stl", model_filename))
    if not bounds:
        return

    mins, maxs = bounds
    model_w = maxs[0] - mins[0]
    model_h = maxs[1] - mins[1]
    direct_error = abs(model_w - width) + abs(model_h - height)
    rotated_error = abs(model_h - width) + abs(model_w - height)
    rotation_z = 90 if rotated_error < direct_error else 0
    fit_w = model_h if rotation_z else model_w
    fit_h = model_w if rotation_z else model_h
    if fit_w > 0 and fit_h > 0:
        scale = min(width / fit_w, height / fit_h) * 0.98

    transformed_mins, transformed_maxs = transformed_bounds(bounds, scale, rotation_z)
    offset_x = (transformed_mins[0] + transformed_maxs[0]) / 2
    offset_y = (transformed_mins[1] + transformed_maxs[1]) / 2
    offset_z = -transformed_mins[2]

    model = pcbnew.FP_3DMODEL()
    model.m_Filename = model_filename.replace("\\", "/")
    model.m_Scale = pcbnew.VECTOR3D(scale, scale, scale)
    model.m_Rotation = pcbnew.VECTOR3D(0, 0, rotation_z)
    model.m_Offset = pcbnew.VECTOR3D(offset_x, offset_y, offset_z)
    model.m_Show = True
    model.m_Opacity = 1.0
    footprint.Add3DModel(model)


def pin_abs(module, pin_name):
    x, y, rotation = module["placement"]
    local_x, local_y = module_pin_xy(module, pin_name)
    rx, ry = rotate_point(local_x, local_y, rotation)
    return x + rx, y + ry


def add_module_footprint(board, module, pad_net_map):
    footprint = pcbnew.FOOTPRINT(board)
    footprint.SetReference(module["id"])
    footprint.SetValue(module["name"])
    hide_footprint_texts(footprint)
    x, y, rotation = module["placement"]
    footprint.SetPosition(wxpoint(x, y))
    footprint.SetOrientationDegrees(rotation)
    board.Add(footprint)

    width, height = module["size_mm"]
    add_footprint_rect(footprint, pcbnew.F_CrtYd, width, height, line_width=0.05, margin=1.0, origin=(x, y), rotation=rotation)
    add_footprint_rect(footprint, pcbnew.F_Fab, width, height, line_width=0.08, margin=0.0, origin=(x, y), rotation=rotation)
    half_w = width / 2
    half_h = height / 2
    corners = [(-half_w, -half_h), (half_w, -half_h), (half_w, half_h), (-half_w, half_h), (-half_w, -half_h)]
    for start, end in zip(corners, corners[1:]):
        sx, sy = rotate_point(*start, rotation)
        ex, ey = rotate_point(*end, rotation)
        add_segment(board, pcbnew.F_SilkS, (x + sx, y + sy), (x + ex, y + ey), 0.12)

    for pin in module.get("pins", []):
        pad = pcbnew.PAD(footprint)
        pad.SetName(pin)
        pad.SetShape(pcbnew.PAD_SHAPE_OVAL)
        pad.SetSize(pcbnew.VECTOR2I(mm(1.7), mm(1.2)))
        pad.SetDrillSize(pcbnew.VECTOR2I(mm(0.8), mm(0.8)))
        pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
        pad.SetLayerSet(pcbnew.LSET.AllCuMask())
        px, py = module_pin_xy(module, pin)
        rx, ry = rotate_point(px, py, rotation)
        pad.SetPosition(wxpoint(x + rx, y + ry))
        pad.SetOrientationDegrees(rotation)
        net = pad_net_map.get((module["id"], pin))
        if net is not None:
            pad.SetNet(net)
        footprint.Add(pad)

    add_3d_model(footprint, module)
    add_text(board, module["id"], x - width / 2 + 1.2, y + height / 2 - 2.0, 0.8, pcbnew.Dwgs_User)
    return footprint


def parse_endpoint(endpoint):
    module_id, pin = endpoint.split(".", 1)
    return module_id, pin


GRID_MM = 2.0
EDGE_MARGIN_MM = 2.0
MODULE_CLEARANCE_MM = 1.8


def point_key(point):
    return f"{point[0]:.1f},{point[1]:.1f}"


def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def clamp(value, lower, upper):
    return min(upper, max(lower, value))


def snap_point(point, board_size):
    width, height = board_size
    return (
        clamp(round(point[0] / GRID_MM) * GRID_MM, EDGE_MARGIN_MM, width - EDGE_MARGIN_MM),
        clamp(round(point[1] / GRID_MM) * GRID_MM, EDGE_MARGIN_MM, height - EDGE_MARGIN_MM),
    )


def module_rect(module, clearance=MODULE_CLEARANCE_MM):
    x, y, rotation = module["placement"]
    width, height = module["size_mm"]
    if rotation in {90, 270}:
        width, height = height, width
    return (
        x - width / 2 - clearance,
        y - height / 2 - clearance,
        x + width / 2 + clearance,
        y + height / 2 + clearance,
    )


def keepout_rect(keepout):
    x, y, width, height = keepout
    return (
        x - MODULE_CLEARANCE_MM,
        y - MODULE_CLEARANCE_MM,
        x + width + MODULE_CLEARANCE_MM,
        y + height + MODULE_CLEARANCE_MM,
    )


def rect_contains(rect, point):
    x1, y1, x2, y2 = rect
    return x1 <= point[0] <= x2 and y1 <= point[1] <= y2


def rect_distance(rect, point):
    x1, y1, x2, y2 = rect
    dx = max(x1 - point[0], 0, point[0] - x2)
    dy = max(y1 - point[1], 0, point[1] - y2)
    return math.hypot(dx, dy)


def point_blocked(point, obstacles, start, end):
    if distance(point, start) < GRID_MM * 1.4 or distance(point, end) < GRID_MM * 1.4:
        return False
    return any(rect_contains(rect, point) for rect in obstacles)


def segment_blocked(a, b, obstacles, start, end):
    steps = max(2, math.ceil(distance(a, b) / 0.8))
    for index in range(1, steps + 1):
        point = (
            a[0] + (b[0] - a[0]) * index / steps,
            a[1] + (b[1] - a[1]) * index / steps,
        )
        if point_blocked(point, obstacles, start, end):
            return True
    return False


def clearance_penalty(point, obstacles):
    penalty = 0
    for rect in obstacles:
        gap = rect_distance(rect, point)
        if gap < 0.8:
            penalty += 25
        elif gap < 2.4:
            penalty += (2.4 - gap) * 3
    return penalty


def simplify_path(points):
    deduped = []
    for point in points:
        if not deduped or distance(point, deduped[-1]) > 0.05:
            deduped.append(point)

    simplified = []
    for point in deduped:
        simplified.append(point)
        while len(simplified) >= 3:
            a, b, c = simplified[-3], simplified[-2], simplified[-1]
            cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
            if abs(cross) > 0.01:
                break
            simplified.pop(-2)
    return simplified


def fallback_route(start, end):
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    diagonal = min(abs(dx), abs(dy)) * 0.55
    if diagonal < 1.5:
        return [start, end]
    return [
        start,
        (start[0] + math.copysign(diagonal, dx), start[1] + math.copysign(diagonal, dy)),
        (end[0] - math.copysign(diagonal, dx), end[1] - math.copysign(diagonal, dy)),
        end,
    ]


def reconstruct_path(came_from, points_by_key, end_key):
    points = []
    cursor = end_key
    while cursor:
        points.append(points_by_key[cursor])
        cursor = came_from.get(cursor)
    return list(reversed(points))


def route_astar(start, end, board_size, obstacles):
    start_node = snap_point(start, board_size)
    end_node = snap_point(end, board_size)
    start_key = point_key(start_node)
    end_key = point_key(end_node)
    open_heap = [(distance(start_node, end_node), start_key)]
    open_keys = {start_key}
    came_from = {}
    points_by_key = {start_key: start_node, end_key: end_node}
    g_score = {start_key: 0}
    moves = [
        (GRID_MM, 0),
        (-GRID_MM, 0),
        (0, GRID_MM),
        (0, -GRID_MM),
        (GRID_MM, GRID_MM),
        (GRID_MM, -GRID_MM),
        (-GRID_MM, GRID_MM),
        (-GRID_MM, -GRID_MM),
    ]

    iterations = 0
    while open_heap and iterations < 4000:
        iterations += 1
        _, current_key = heapq.heappop(open_heap)
        if current_key not in open_keys:
            continue
        open_keys.remove(current_key)
        current = points_by_key[current_key]
        if current_key == end_key:
            path = reconstruct_path(came_from, points_by_key, current_key)
            return simplify_path([start] + path[1:-1] + [end])

        for move in moves:
            next_point = (
                clamp(current[0] + move[0], EDGE_MARGIN_MM, board_size[0] - EDGE_MARGIN_MM),
                clamp(current[1] + move[1], EDGE_MARGIN_MM, board_size[1] - EDGE_MARGIN_MM),
            )
            next_key = point_key(next_point)
            if next_key == current_key:
                continue
            if point_blocked(next_point, obstacles, start, end):
                continue
            if segment_blocked(current, next_point, obstacles, start, end):
                continue

            diagonal_bias = -0.15 if move[0] and move[1] else 0
            tentative = g_score[current_key] + distance(current, next_point) + diagonal_bias + clearance_penalty(next_point, obstacles)
            if tentative >= g_score.get(next_key, float("inf")):
                continue
            came_from[next_key] = current_key
            points_by_key[next_key] = next_point
            g_score[next_key] = tentative
            heapq.heappush(open_heap, (tentative + distance(next_point, end_node), next_key))
            open_keys.add(next_key)

    return simplify_path(fallback_route(start, end))


def route_net(board, modules_by_id, board_size, net_name, source, targets):
    net = get_or_create_net(board, net_name)
    source_module_id, source_pin = parse_endpoint(source)
    source_module = modules_by_id[source_module_id]
    start = pin_abs(source_module, source_pin)
    width = 0.6 if net_name in {"5V", "3V3", "GND", "BAT+"} else 0.25

    for index, target in enumerate(targets):
        target_module_id, target_pin = parse_endpoint(target)
        target_module = modules_by_id[target_module_id]
        end = pin_abs(target_module, target_pin)
        layer = pcbnew.F_Cu if index % 2 == 0 else pcbnew.B_Cu

        ignored = {source_module_id, target_module_id}
        obstacles = [
            module_rect(module)
            for module_id, module in modules_by_id.items()
            if module_id not in ignored
        ]
        obstacles.extend(keepout_rect(module["keepout"]) for module in modules_by_id.values() if "keepout" in module)
        path = route_astar(start, end, board_size, obstacles)
        for segment_start, segment_end in zip(path, path[1:]):
            add_segment(board, layer, segment_start, segment_end, width, net)


def add_keepout(board, keepout):
    x, y, width, height = keepout
    add_segment(board, pcbnew.Dwgs_User, (x, y), (x + width, y), 0.15)
    add_segment(board, pcbnew.Dwgs_User, (x + width, y), (x + width, y + height), 0.15)
    add_segment(board, pcbnew.Dwgs_User, (x + width, y + height), (x, y + height), 0.15)
    add_segment(board, pcbnew.Dwgs_User, (x, y + height), (x, y), 0.15)
    add_text(board, "AI keepout", x, y - 1.2, 0.9, pcbnew.Dwgs_User)


def build_board_from_plan(board, plan, route_internal=True):
    for track in list(board.GetTracks()):
        board.Remove(track)
    for zone in list(board.Zones()):
        board.Remove(zone)
    for drawing in list(board.GetDrawings()):
        board.Remove(drawing)
    for footprint in list(board.GetFootprints()):
        board.Remove(footprint)

    width, height = plan["board_mm"]
    add_outline(board, width, height)
    add_text(board, plan.get("title", "AI modular backplane"), 5, 5, 1.2, pcbnew.Dwgs_User)

    modules_by_id = {}
    pad_net_map = {}
    for net_name, source, targets in plan.get("nets", []):
        net = get_or_create_net(board, net_name)
        for endpoint in [source] + targets:
            module_id, pin = parse_endpoint(endpoint)
            pad_net_map[(module_id, pin)] = net

    for module in plan["modules"]:
        modules_by_id[module["id"]] = module
        add_module_footprint(board, module, pad_net_map)
        if "keepout" in module:
            add_keepout(board, module["keepout"])

    if route_internal:
        for net_name, source, targets in plan.get("nets", []):
            route_net(board, modules_by_id, (width, height), net_name, source, targets)

    board.SetDesignSettings(board.GetDesignSettings())
    return board
