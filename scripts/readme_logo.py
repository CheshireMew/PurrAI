"""Export the README mark from the application's CatMark component and primary color."""

import argparse
from pathlib import Path
import re
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/assets/logo.svg"


def render():
    component = (ROOT / "frontend/src/CatMark.vue").read_text(encoding="utf-8")
    stylesheet = (ROOT / "frontend/src/style.css").read_text(encoding="utf-8")
    svg = re.search(r"<svg\b[^>]*>(.*?)</svg>", component, re.S)
    viewbox = re.search(r'viewBox="([^"]+)"', component)
    color = re.search(r"--el-color-primary:\s*(#[0-9a-fA-F]{6})\b", stylesheet)
    if not all((svg, viewbox, color)):
        raise ValueError("CatMark SVG, viewBox, or primary color could not be resolved")
    result = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{viewbox[1]}" fill="none" color="{color[1]}" '
        'role="img" aria-labelledby="title desc">\n'
        '  <title id="title">PurrAI</title>\n'
        '  <desc id="desc">The cat mark used by the PurrAI admin console.</desc>\n'
        f'{svg[1].strip()}\n</svg>\n'
    )
    ET.fromstring(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true", help="regenerate the README SVG")
    action.add_argument("--check", action="store_true", help="check that the SVG matches its source")
    args = parser.parse_args()
    expected = render()
    if args.write:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(expected, encoding="utf-8", newline="\n")
        print(f"Generated: {OUTPUT}")
    elif not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != expected:
        parser.exit(1, "README logo differs from CatMark.vue or the primary color; run --write.\n")
    else:
        print("OK: README logo matches CatMark.vue and the primary color.")


if __name__ == "__main__":
    main()
