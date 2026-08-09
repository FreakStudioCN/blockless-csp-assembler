from __future__ import annotations

import re
import sys
from pathlib import Path


STYLE_RE = re.compile(r'style="(?P<style>[^"]+)"')
VIEWBOX_RE = re.compile(r'viewBox="(?P<x>[-0-9.]+) (?P<y>[-0-9.]+) (?P<w>[-0-9.]+) (?P<h>[-0-9.]+)"')
OUTLINE_RE = re.compile(r'\n\s*<rect id="blockless-board-outline"[^>]+/>\n?', re.MULTILINE)

LAYER_STYLES = {
    "#C83434": {"stroke": "#D93F34", "fill": "#D93F34", "min_width": 0.38},  # F.Cu
    "#4D7FC4": {"stroke": "#4388E8", "fill": "#4388E8", "min_width": 0.38},  # B.Cu
    "#F2EDA1": {"stroke": "#9A6800", "fill": "#F8F1B0", "min_width": 0.34},  # silk
    "#8A5A00": {"stroke": "#9A6800", "fill": "#F8F1B0", "min_width": 0.34},  # enhanced silk
    "#D0D2CD": {"stroke": "#1F2A2E", "fill": "#263238", "min_width": 0.44},  # outline / fab
    "#263238": {"stroke": "#1F2A2E", "fill": "#263238", "min_width": 0.44},  # enhanced outline
}


def replace_style_value(style: str, key: str, value: str) -> str:
    pattern = re.compile(rf"({re.escape(key)}\s*:\s*)[^;]+")
    if pattern.search(style):
        return pattern.sub(rf"\g<1>{value}", style)
    return f"{style.rstrip(';')}; {key}:{value};"


def enhance_style(style: str) -> str:
    updated = style
    upper_style = style.upper()
    for color, rule in LAYER_STYLES.items():
        color_upper = color.upper()
        if color_upper not in upper_style:
            continue
        if re.search(rf"stroke\s*:\s*{re.escape(color)}", updated, flags=re.IGNORECASE):
            updated = re.sub(rf"(stroke\s*:\s*){re.escape(color)}", rf"\g<1>{rule['stroke']}", updated, flags=re.IGNORECASE)
            width_match = re.search(r"stroke-width\s*:\s*([0-9.]+)", updated)
            if width_match:
                width = float(width_match.group(1))
                updated = replace_style_value(updated, "stroke-width", f"{max(width, rule['min_width']):.4f}")
            else:
                updated = replace_style_value(updated, "stroke-width", f"{rule['min_width']:.4f}")
            updated = replace_style_value(updated, "stroke-opacity", "1")
        if re.search(rf"fill\s*:\s*{re.escape(color)}", updated, flags=re.IGNORECASE):
            updated = re.sub(rf"(fill\s*:\s*){re.escape(color)}", rf"\g<1>{rule['fill']}", updated, flags=re.IGNORECASE)
            updated = replace_style_value(updated, "fill-opacity", "1.0000")
    return updated


def add_board_outline(text: str) -> str:
    match = VIEWBOX_RE.search(text)
    if not match:
        return text
    x = float(match.group("x"))
    y = float(match.group("y"))
    w = float(match.group("w"))
    h = float(match.group("h"))
    inset = 0.18
    outline = (
        f'\n<rect id="blockless-board-outline" x="{x + inset:.4f}" y="{y + inset:.4f}" '
        f'width="{max(0, w - inset * 2):.4f}" height="{max(0, h - inset * 2):.4f}" '
        'rx="1.0000" ry="1.0000" fill="none" stroke="#F4C766" '
        'stroke-width="0.5200" stroke-opacity="1" stroke-linecap="round" stroke-linejoin="round"/>\n'
    )
    text = OUTLINE_RE.sub("", text)
    return text.replace("</svg>", f"{outline}</svg>")


def enhance(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    updated = STYLE_RE.sub(lambda match: f'style="{enhance_style(match.group("style"))}"', text)
    updated = add_board_outline(updated)
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
            print(f"Enhanced SVG readability: {path.name}")
    print(f"Enhanced {changed} SVG file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
