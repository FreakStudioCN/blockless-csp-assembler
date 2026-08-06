"""Per-layer clearance rules from a board's custom design rules (#498).

KiCad stores layer-scoped clearance requirements in ``<project>.kicad_dru``
(Board Setup -> Custom Rules) -- netclasses cannot express them (a net spans
layers but carries one clearance). This module reads the PARSABLE SUBSET of
those rules into a ``{layer_name: clearance_mm}`` map the router and checkers
consume with REPLACEMENT semantics: on a ruled layer the rule value replaces
the net/class-resolved pair clearance (KiCad's own precedence -- custom rules
outrank netclasses and may tighten OR relax), while unruled layers keep the
existing resolution. The map is the board's own spec, so it outranks
``--clearance`` too; only the fab tier's physical floor pins it up.

Subset honored (anything else is skipped with a note -- mis-modeling KiCad's
expression engine would silently manufacture wrong clearances):
  - ``(constraint clearance (min <val>))`` rules whose scope is pure layers:
      * a ``(layer <name>|outer|inner)`` clause, and/or
      * a ``(condition "A.onLayer('X') || B.onLayer('Y') ...")`` expression
        containing ONLY onLayer terms joined by ``||``
  - a rule with NO scope applies to every copper layer (KiCad semantics)
  - later rules override earlier ones per layer (KiCad evaluates last-to-
    first, first match wins)
  - ``(severity ignore)`` rules are skipped
"""

import os
import re
from typing import Dict, List, Optional, Tuple

# value with unit suffix; KiCad writes e.g. 0.3mm / 12mil / 0.012in
_VAL = re.compile(r"^(-?[0-9.]+)\s*(mm|mil|in|um|nm)?$")
_UNIT_MM = {"mm": 1.0, "mil": 0.0254, "in": 25.4, "um": 0.001, "nm": 1e-6, None: 1.0}
_ONLAYER = re.compile(r"[AB]\s*\.\s*onLayer\s*\(\s*['\"]([^'\"]+)['\"]\s*\)")


def _to_mm(tok: str) -> Optional[float]:
    m = _VAL.match(tok.strip())
    if not m:
        return None
    try:
        return float(m.group(1)) * _UNIT_MM[m.group(2)]
    except (ValueError, KeyError):
        return None


def _tokenize(text: str):
    """S-expression tokens: '(', ')', atoms, quoted strings (quotes kept off)."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in "()":
            out.append(c)
            i += 1
        elif c in " \t\r\n":
            i += 1
        elif c == '"':
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            out.append("".join(buf))
            i = j + 1
        elif c == '#':  # comment to end of line
            while i < n and text[i] != "\n":
                i += 1
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n()"':
                j += 1
            out.append(text[i:j])
            i = j
    return out


def _parse_nodes(tokens):
    """Nested lists from the token stream."""
    stack, cur = [], []
    for t in tokens:
        if t == "(":
            stack.append(cur)
            cur = []
        elif t == ")":
            done = cur
            cur = stack.pop() if stack else []
            cur.append(done)
        else:
            cur.append(t)
    return cur


def _rule_layers(rule, copper_layers, notes) -> Optional[List[str]]:
    """Resolve a rule's layer scope to concrete copper layers, or None when the
    scope uses constructs outside the honored subset (rule must be skipped)."""
    outer = [l for l in copper_layers if l in ("F.Cu", "B.Cu")]
    inner = [l for l in copper_layers if l not in ("F.Cu", "B.Cu")]
    clause_set = cond_set = None
    for item in rule:
        if not isinstance(item, list) or not item:
            continue
        if item[0] == "layer" and len(item) >= 2:
            arg = item[1]
            if arg == "outer":
                clause_set = list(outer)
            elif arg == "inner":
                clause_set = list(inner)
            elif arg in copper_layers:
                clause_set = [arg]
            elif isinstance(arg, str) and arg.endswith(".Cu"):
                clause_set = []  # a copper layer this board doesn't have
            else:
                return None  # non-copper / wildcard layer clause: out of subset
        elif item[0] == "condition" and len(item) >= 2:
            expr = item[1]
            # Only onLayer terms joined by ||: strip every onLayer term and the
            # join operators; anything left means the expression carries other
            # predicates (netclass, area, ...) we must not guess at.
            found = _ONLAYER.findall(expr)
            residue = _ONLAYER.sub("", expr).replace("||", "").strip()
            if residue or not found:
                return None
            cond_set = [l for l in found if l in copper_layers]
    if clause_set is not None and cond_set is not None:
        return [l for l in clause_set if l in cond_set]
    if clause_set is not None:
        return clause_set
    if cond_set is not None:
        return cond_set
    return list(copper_layers)  # unscoped rule: applies everywhere


def parse_dru_layer_clearances(text: str, copper_layers: List[str]
                               ) -> Tuple[Dict[str, float], List[str]]:
    """Parse .kicad_dru text -> ({layer: clearance_mm}, notes).

    Later rules override earlier ones per layer. Notes describe every skipped
    clearance rule (out-of-subset scope) so the caller can print them.
    """
    result: Dict[str, float] = {}
    notes: List[str] = []
    for node in _parse_nodes(_tokenize(text)):
        if not isinstance(node, list) or not node or node[0] != "rule":
            continue
        name = node[1] if len(node) > 1 and not isinstance(node[1], list) else "?"
        clearance_mm = None
        severity_ignore = False
        for item in node:
            if not isinstance(item, list) or not item:
                continue
            if item[0] == "severity" and len(item) >= 2 and item[1] == "ignore":
                severity_ignore = True
            if item[0] == "constraint" and len(item) >= 2 and item[1] == "clearance":
                for sub in item[2:]:
                    if isinstance(sub, list) and sub and sub[0] == "min" and len(sub) >= 2:
                        clearance_mm = _to_mm(sub[1])
        if clearance_mm is None:
            continue  # not a clearance rule (or no min): not ours to model
        if severity_ignore:
            notes.append(f"rule '{name}': severity ignore, skipped")
            continue
        layers = _rule_layers(node, copper_layers, notes)
        if layers is None:
            notes.append(f"rule '{name}': clearance {clearance_mm}mm has a "
                         f"non-layer scope (netclass/area/... condition) -- "
                         f"skipped, KiCad will still enforce it")
            continue
        for l in layers:
            result[l] = clearance_mm
    return result, notes


def read_board_layer_clearances(board_path: str, copper_layers: List[str],
                                fab_clearance_floor: Optional[float] = None,
                                ) -> Tuple[Dict[str, float], List[str]]:
    """Auto-read the sibling ``<board>.kicad_dru`` layer-clearance map.

    Returns ({} , []) when there is no dru file -- the feature is a strict
    no-op for boards without custom rules. ``fab_clearance_floor`` pins each
    value UP to the fab tier's physical minimum (the one thing that outranks
    the board's own rules), with a note.
    """
    dru = os.path.splitext(board_path)[0] + ".kicad_dru" if board_path else ""
    if not dru or not os.path.isfile(dru):
        return {}, []
    try:
        text = open(dru, encoding="utf-8").read()
    except OSError as e:
        return {}, [f"could not read {dru}: {e}"]
    result, notes = parse_dru_layer_clearances(text, copper_layers)
    if fab_clearance_floor:
        for l, v in list(result.items()):
            if v < fab_clearance_floor:
                notes.append(f"layer {l}: rule clearance {v}mm below the fab "
                             f"floor {fab_clearance_floor}mm -- pinned up")
                result[l] = fab_clearance_floor
    return result, notes


_ANNOUNCED = set()  # (board path, map items) already printed this process


def board_layer_clearance_map(pcb_data) -> Dict[str, float]:
    """Fab-pinned #498 layer-clearance map for a PCBData, discovered via its
    ``source_path`` sibling .kicad_dru. Quiet ({} when there is none) -- for
    engines that consume the map outside a GridRouteConfig (fanout scalars)."""
    path = getattr(pcb_data, 'source_path', "") or ""
    if not path:
        return {}
    copper = list(getattr(pcb_data.board_info, 'copper_layers', None) or [])
    if not copper:
        return {}
    try:
        from fab_tiers import fab_floors
        floor = fab_floors(len(copper)).get('clearance')
    except Exception:
        floor = None
    lmap, _ = read_board_layer_clearances(path, copper, fab_clearance_floor=floor)
    return lmap


def min_rule_clearance(board_path: str) -> Optional[float]:
    """Smallest honored .kicad_dru layer-rule clearance for ``board_path``'s
    project, or None (no dru / no layer clearance rules). The DRC-settings
    writeback caps ``rules.min_clearance`` at this: KiCad's board minimum is an
    ABSOLUTE floor that outranks custom rules, so leaving it above a RELAXING
    rule re-manufactures phantom violations on copper legally routed at the
    rule value. Uses a generous synthetic copper list -- for the minimum it
    only matters that every plausibly-referenced layer resolves."""
    copper = ["F.Cu", "B.Cu"] + [f"In{i}.Cu" for i in range(1, 31)]
    lmap, _ = read_board_layer_clearances(board_path, copper)
    return min(lmap.values()) if lmap else None


def install_layer_clearances(config, layer_clearances, input_file, pcb_data=None):
    """Resolve and install the #498 per-layer map on ``config``, engine-side so
    BOTH fronts inherit it (the CLI passes nothing; the GUI passes nothing --
    there is deliberately no flag or control, the .kicad_dru is the single
    source of truth). An explicit dict (tests) wins and stops the auto-read,
    mirroring the net_clearances pattern; None -> read the sibling .kicad_dru
    of ``input_file``, expanded over the BOARD's full copper list (rules may
    target layers outside the routed subset -- via keep-outs still need them)
    and pinned up to the fab tier's clearance floor. Prints what is honored."""
    if layer_clearances is not None:
        config.layer_clearances = dict(layer_clearances)
        return
    if not input_file:
        # Engines whose signatures carry no input path (planes, fanout, oracle
        # sub-configs) discover the board file via PCBData.source_path.
        input_file = getattr(pcb_data, 'source_path', "") or ""
    copper = None
    if pcb_data is not None and getattr(pcb_data, 'board_info', None) is not None:
        copper = list(pcb_data.board_info.copper_layers or [])
    if not copper:
        copper = list(config.layers)
    try:
        from fab_tiers import fab_floors
        floor = fab_floors(len(copper)).get('clearance')
    except Exception:
        floor = None
    lmap, notes = read_board_layer_clearances(input_file or "", copper,
                                              fab_clearance_floor=floor)
    # One announcement per (board, map) per process -- plane/fanout runs build
    # several configs for the same board and would repeat it.
    _key = (os.path.abspath(input_file) if input_file else "",
            tuple(sorted(lmap.items())))
    if _key not in _ANNOUNCED:
        _ANNOUNCED.add(_key)
        for note in notes:
            print(f"  .kicad_dru: {note}")
        if lmap:
            vals = ", ".join(f"{l}:{v:g}" for l, v in sorted(lmap.items()))
            print(f"Per-layer clearance rules from the board's .kicad_dru (#498, "
                  f"replace net/class values on their layers): {vals}")
    config.layer_clearances = lmap
