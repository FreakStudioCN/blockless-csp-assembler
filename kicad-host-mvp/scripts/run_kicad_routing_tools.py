import runpy
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "tools" / "KiCadRoutingTools-0.19.3"
DEPS = REPO / "tools" / "kicad-python-deps"
RUST_ROUTER = TOOLS / "rust_router"


def main():
    for path in (str(DEPS), str(RUST_ROUTER), str(TOOLS)):
        if path not in sys.path:
            sys.path.insert(0, path)

    route_py = TOOLS / "route.py"
    if not route_py.exists():
        raise SystemExit(f"Missing KiCadRoutingTools route.py at {route_py}")

    sys.argv = [str(route_py), *sys.argv[1:]]
    runpy.run_path(str(route_py), run_name="__main__")


if __name__ == "__main__":
    main()
