#!/usr/bin/env python3
"""Guards against results that depend on PROCESS HISTORY rather than inputs.

The GUI runs many routing steps in ONE long-lived process; the CLI runs each
step in a fresh one. Anything that leaks across steps -- a set iteration order,
an id()-keyed cache -- makes the GUI disagree with the CLI, and makes the GUI
disagree with ITSELF between launches. Two such bugs have already shipped:

  * `get_selected_nets()` returned `list(self._checked_nets)`. Set-of-strings
    order is randomized per process (PYTHONHASHSEED), the router routes nets in
    the order given, and three runs of one plan produced 3425 / 3433 / 3431
    segments (fixed 2df22ca with sorted()).
  * `local_to_global` composed pad coordinates in float mm while pcbnew used
    integer nm, so the two fronts' A* tie-breaks diverged (fixed 78f1731).

This file pins the remaining cases found while chasing eth_tap step 11.

Rules enforced:
  1. Layer-name lists must not come from `list(set(...))`. Layer names are
     STRINGS, so set order varies per process, and these lists become the
     router's `layers=` -- layer order decides which layer is tried first.
     Use `sorted(...)` or `dict.fromkeys(...)` (order-preserving dedupe).
  2. `obstacle_costs._MERGE_MEMO` is keyed on `id()`. Its entries must hold the
     keyed objects alive (so an address cannot be recycled into a stale hit)
     and the memo must be bounded (so keepalive cannot leak).

`list(set(...))` over grid CELLS (tuples of ints) is deliberately NOT flagged:
int hashing is not randomized, so those orders are stable.

Pure Python, no pcbnew/wx. Run: python3 tests/test_process_order_determinism.py
"""
import ast
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT)

failures = []

# Files whose list(set(...)) provably builds LAYER lists. Kept explicit rather
# than pattern-matched: the same idiom over int tuples is fine.
LAYER_ORDER_FILES = ('route_planes.py', 'check_drc.py', 'route.py',
                     'repair_planes.py', 'obstacle_map.py')


def _list_of_set_calls(path):
    """Return [(lineno, source_segment)] for `list(set(...))` expressions."""
    try:
        src = open(path, encoding='utf-8').read()
    except OSError:
        return []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == 'list'
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Call)
                and isinstance(node.args[0].func, ast.Name)
                and node.args[0].func.id == 'set'):
            seg = ast.get_source_segment(src, node) or ''
            hits.append((node.lineno, ' '.join(seg.split())[:120]))
    return hits


def test_no_set_derived_layer_lists():
    print("1. layer lists are not built from list(set(...))")
    bad = []
    for fname in LAYER_ORDER_FILES:
        path = os.path.join(ROOT, fname)
        if not os.path.exists(path):
            continue
        for lineno, seg in _list_of_set_calls(path):
            low = seg.lower()
            if 'layer' in low:
                bad.append(f"{fname}:{lineno}  {seg}")
    if bad:
        for b in bad:
            print(f"  FAIL {b}")
        failures.append(f"{len(bad)} set-derived layer list(s): {bad}")
    else:
        print(f"  ok   none in {len(LAYER_ORDER_FILES)} routing modules")


def test_check_drc_fallback_is_sorted():
    print("2. check_drc's segment-derived layer fallback is deterministic")
    src = open(os.path.join(ROOT, 'check_drc.py'), encoding='utf-8').read()
    if 'routing_layers = sorted(set(seg.layer' in src:
        print("  ok   uses sorted(set(...))")
    else:
        print("  FAIL fallback is not sorted")
        failures.append("check_drc routing_layers fallback is not sorted()")


def test_merge_memo_keepalive_and_bound():
    print("3. obstacle_costs._MERGE_MEMO has keepalive + a bound")
    import obstacle_costs
    if not hasattr(obstacle_costs, '_MERGE_MEMO_MAX'):
        print("  FAIL no _MERGE_MEMO_MAX bound")
        failures.append("_MERGE_MEMO is unbounded")
    else:
        print(f"  ok   bounded at {obstacle_costs._MERGE_MEMO_MAX}")
    src = open(os.path.join(ROOT, 'obstacle_costs.py'), encoding='utf-8').read()
    # The stored tuple must carry the keyed object so its id() cannot be reused.
    if '_MERGE_MEMO[key] = (sig, all_costs, per_net_costs' in src:
        print("  ok   entry pins per_net_costs (and arrays) alive")
    else:
        print("  FAIL entry does not keep the id()-keyed object alive")
        failures.append("_MERGE_MEMO entries lack keepalive refs")


# Modules scanned by rule 5. Widened beyond the router because the placement
# engine shipped this exact bug (#457): quench's `net_refs` was a
# Dict[int, Set[str]] whose iteration order became MST point order.
STR_SET_SCAN_DIRS = ('placement', 'kicad_routing_plugin')
STR_SET_SCAN_FILES = LAYER_ORDER_FILES + ('connectivity.py', 'placement/quench.py')


def _str_set_attrs(tree):
    """Names annotated as holding a set OF STRINGS, directly or as dict values.

    Matches `x: Set[str]`, `self.x: Dict[int, Set[str]]`, and the lowercase
    builtin-generic spellings. Sets of int / tuple-of-int are deliberately NOT
    collected: int hashing is not randomized, so those orders are stable.
    """
    found = set()

    def is_str_set(node):
        if not isinstance(node, ast.Subscript):
            return False
        base = node.value
        name = base.attr if isinstance(base, ast.Attribute) else getattr(base, 'id', '')
        if name in ('Set', 'FrozenSet', 'set', 'frozenset'):
            inner = node.slice
            iname = inner.attr if isinstance(inner, ast.Attribute) else getattr(inner, 'id', '')
            return iname == 'str'
        if name in ('Dict', 'dict') and isinstance(node.slice, ast.Tuple) \
                and len(node.slice.elts) == 2:
            return is_str_set(node.slice.elts[1])
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and node.annotation is not None \
                and is_str_set(node.annotation):
            t = node.target
            if isinstance(t, ast.Attribute):
                found.add(t.attr)
            elif isinstance(t, ast.Name):
                found.add(t.id)
    return found


def _iterations_over(tree, names):
    """[(lineno, name)] for `for _ in <name>` / `for _ in <name>[...]` and
    `list(<name>)`, where <name> is one of the string-set names."""
    def base_name(node):
        if isinstance(node, ast.Subscript):
            node = node.value
        if isinstance(node, ast.Attribute):
            return node.attr
        if isinstance(node, ast.Name):
            return node.id
        return None

    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.For):
            n = base_name(node.iter)
            if n in names:
                hits.append((node.lineno, n))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id in ('list', 'tuple')
              and len(node.args) == 1):
            n = base_name(node.args[0])
            if n in names:
                hits.append((node.lineno, n))
    return hits


def test_no_ordered_use_of_string_sets():
    print("5. no ORDER-BEARING iteration over a Set[str] (#457)")
    paths = [os.path.join(ROOT, f) for f in STR_SET_SCAN_FILES]
    for d in STR_SET_SCAN_DIRS:
        dpath = os.path.join(ROOT, d)
        if os.path.isdir(dpath):
            paths += [os.path.join(dpath, f) for f in sorted(os.listdir(dpath))
                      if f.endswith('.py')]
    bad = []
    scanned = 0
    for path in dict.fromkeys(paths):
        if not os.path.exists(path):
            continue
        try:
            src = open(path, encoding='utf-8').read()
            tree = ast.parse(src)
        except (OSError, SyntaxError):
            continue
        scanned += 1
        names = _str_set_attrs(tree)
        if not names:
            continue
        rel = os.path.relpath(path, ROOT)
        for lineno, name in _iterations_over(tree, names):
            bad.append(f"{rel}:{lineno}  iterates `{name}` (declared Set[str]) "
                       f"-- order is PYTHONHASHSEED-dependent; sort it, or "
                       f"declare/build it as a list")
    if bad:
        for b in bad:
            print(f"  FAIL {b}")
        failures.append(f"{len(bad)} ordered use(s) of a Set[str]: {bad}")
    else:
        print(f"  ok   none in {scanned} scanned module(s)")


def test_selected_nets_sorted():
    print("4. GUI net selection stays deterministically ordered (2df22ca)")
    path = os.path.join(ROOT, 'kicad_routing_plugin', 'fanout_gui.py')
    src = open(path, encoding='utf-8').read()
    if 'return list(self._checked_nets)' in src:
        print("  FAIL get_selected_nets returns list(set) again")
        failures.append("get_selected_nets returned to list(set(...))")
    elif 'return sorted(self._checked_nets)' in src:
        print("  ok   returns sorted()")
    else:
        print("  WARN could not confirm; check get_selected_nets by hand")


def main():
    for t in (test_no_set_derived_layer_lists,
              test_check_drc_fallback_is_sorted,
              test_merge_memo_keepalive_and_bound,
              test_selected_nets_sorted,
              test_no_ordered_use_of_string_sets):
        t()
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print("  " + f)
        return 1
    print("PASS: no process-history-dependent ordering or id() aliasing")
    return 0


if __name__ == '__main__':
    sys.exit(main())
