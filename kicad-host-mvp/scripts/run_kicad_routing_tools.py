import runpy
import os
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
TOOLS = Path(os.environ.get("KICAD_ROUTING_TOOLS_DIR", REPO / "tools" / "KiCadRoutingTools"))
DEPS = Path(os.environ.get("BLOCKLESS_KICAD_DEPS", REPO / "tools" / "kicad-python-deps"))
RUST_ROUTER = TOOLS / "rust_router"


def main():
    paths = [RUST_ROUTER, TOOLS]
    if DEPS.exists():
        paths.insert(0, DEPS)

    for path in map(str, paths):
        if path not in sys.path:
            sys.path.insert(0, path)

    route_py = TOOLS / "route.py"
    if not route_py.exists():
        raise SystemExit(f"Missing KiCadRoutingTools route.py at {route_py}")

    sys.argv = [str(route_py), *sys.argv[1:]]
    runpy.run_path(str(route_py), run_name="__main__")


if __name__ == "__main__":
    main()
