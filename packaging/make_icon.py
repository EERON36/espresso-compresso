#!/usr/bin/env python3
"""Render the established espresso_compresso_icon.svg design as a Windows ICO."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


BACKGROUND = "#EFE5D5"
INK = "#292621"
ORANGE = "#C5683A"
SURFACE = "#FFF9F0"
BLUE = "#3E687E"
STEAM = "#B79278"


def scaled(value: float, size: int) -> int:
    return round(value * size / 256)


def render(size: int) -> Image.Image:
    """Draw the same mug, film strip, steam, and shadow as the SVG source."""
    image = Image.new("RGBA", (size, size), BACKGROUND)
    draw = ImageDraw.Draw(image)
    box = lambda values: tuple(scaled(value, size) for value in values)
    width = lambda value: max(1, scaled(value, size))

    draw.rounded_rectangle(box((40, 82, 168, 186)), radius=scaled(18, size), fill=ORANGE, outline=INK, width=width(12))
    draw.arc(box((158, 98, 232, 172)), start=275, end=85, fill=INK, width=width(12))
    draw.line(box((56, 118, 152, 118)), fill=SURFACE, width=width(12))
    draw.rounded_rectangle(box((91, 104, 117, 132)), radius=scaled(4, size), fill=BLUE, outline=INK, width=width(7))
    draw.line(box((78, 70, 81, 54, 79, 42, 81, 27)), fill=STEAM, width=width(10))
    draw.line(box((125, 70, 128, 54, 126, 42, 128, 27)), fill=STEAM, width=width(10))
    draw.ellipse(box((26, 191, 182, 217)), fill=BLUE)
    return image


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Established SVG source; must exist")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.source.is_file():
        raise SystemExit(f"Icon SVG not found: {args.source}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    image = render(256)
    image.save(args.output, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


if __name__ == "__main__":
    main()
