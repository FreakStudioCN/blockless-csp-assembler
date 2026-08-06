from __future__ import annotations

import sys
from pathlib import Path


REPLACEMENTS = (
    (
        "stroke:#F2EDA1; stroke-width:0.1200; stroke-opacity:1;",
        "stroke:#8A5A00; stroke-width:0.3200; stroke-opacity:1;",
    ),
    (
        "stroke:#D0D2CD; stroke-width:0.1000; stroke-opacity:1;",
        "stroke:#263238; stroke-width:0.4200; stroke-opacity:1;",
    ),
)


def enhance(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    updated = text
    for source, target in REPLACEMENTS:
        updated = updated.replace(source, target)
    if updated == text:
        return False
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(updated)
    return True


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: enhance_kicad_svg.py <file.svg> [file.svg ...]", file=sys.stderr)
        return 2

    changed = 0
    for raw_path in sys.argv[1:]:
        path = Path(raw_path)
        if not path.exists():
            raise FileNotFoundError(path)
        if enhance(path):
            changed += 1
            print(f"Enhanced SVG contrast: {path}")
    print(f"Enhanced {changed} SVG file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
