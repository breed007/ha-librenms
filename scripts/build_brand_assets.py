#!/usr/bin/env python3
"""Build the integration's brand image set from LibreNMS's official logos.

Since Home Assistant 2026.3 a custom integration ships its own brand images in
a ``brand/`` directory, and those take priority over the brands CDN. No pull
request against home-assistant/brands is needed -- that repository no longer
accepts icons for custom integrations.

The PNGs are rendered from the SVGs LibreNMS publishes, so the integration
shows LibreNMS's own mark rather than something invented for it. Source artwork
is downloaded at run time and is not vendored here; re-run this script to pick
up upstream changes.

Usage::

    python3 -m venv .toolvenv
    .toolvenv/bin/pip install resvg-py pillow pyoxipng
    .toolvenv/bin/python scripts/build_brand_assets.py

Output lands in ``custom_components/librenms/brand/``, which is where Home
Assistant looks for it.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import io
from pathlib import Path
import sys
import tempfile
import urllib.request

try:
    import oxipng
    from PIL import Image
    import resvg_py
except ImportError as err:  # pragma: no cover - developer tooling
    sys.exit(f"missing build dependency: {err}\nSee the module docstring.")

SOURCE_BASE = "https://raw.githubusercontent.com/librenms/librenms/master/html/images"

# LibreNMS names its files for the background they sit on: `_light` artwork is
# dark grey and belongs on a white background, `_dark` artwork is white and
# belongs on a dark one. Home Assistant uses the opposite convention -- the
# unprefixed name is the light-background image, and `dark_` is for dark
# backgrounds -- so the mapping below is intentionally crossed over.
ICON_LIGHT_BG = "librenms_logo_only_light.svg"
ICON_DARK_BG = "librenms_logo_only_dark.svg"
LOGO_LIGHT_BG = "librenms_logo_light.svg"
LOGO_DARK_BG = "librenms_logo_dark.svg"

# Render this many times larger than the target, then downsample. Small marks
# come out noticeably cleaner than rendering straight to the final size.
SUPERSAMPLE = 4


@dataclass(frozen=True)
class Target:
    """One output file."""

    filename: str
    source: str
    size: int
    square: bool

    @property
    def description(self) -> str:
        """Return a human-readable size for the summary table."""
        return f"{self.size}x{self.size}" if self.square else f"h={self.size}"


# Icons must be exactly square: 256 and 512. Logos keep the brand's aspect
# ratio, with the shortest side in 128-256 (standard) and 256-512 (@2x).
# Home Assistant recognises exactly these eight filenames.
TARGETS: tuple[Target, ...] = (
    Target("icon.png", ICON_LIGHT_BG, 256, True),
    Target("icon@2x.png", ICON_LIGHT_BG, 512, True),
    Target("dark_icon.png", ICON_DARK_BG, 256, True),
    Target("dark_icon@2x.png", ICON_DARK_BG, 512, True),
    Target("logo.png", LOGO_LIGHT_BG, 128, False),
    Target("logo@2x.png", LOGO_LIGHT_BG, 256, False),
    Target("dark_logo.png", LOGO_DARK_BG, 128, False),
    Target("dark_logo@2x.png", LOGO_DARK_BG, 256, False),
)


def fetch_sources(destination: Path) -> None:
    """Download each distinct source SVG into `destination`."""
    for name in sorted({target.source for target in TARGETS}):
        url = f"{SOURCE_BASE}/{name}"
        path = destination / name
        with urllib.request.urlopen(url, timeout=30) as response:
            path.write_bytes(response.read())
        print(f"  fetched {name} ({path.stat().st_size} bytes)")


def render(svg_path: Path, height: int) -> Image.Image:
    """Rasterise an SVG at `height` pixels and trim its transparent border."""
    png = resvg_py.svg_to_bytes(svg_path=str(svg_path), height=height)
    image = Image.open(io.BytesIO(bytes(png))).convert("RGBA")

    # The brands repo asks for the minimum amount of empty space, and the
    # source viewBoxes are not always tight around the artwork.
    if (bbox := image.getbbox()) is not None:
        image = image.crop(bbox)
    return image


def build_square(svg_path: Path, size: int) -> Image.Image:
    """Return the mark centred on a transparent square canvas."""
    art = render(svg_path, size * SUPERSAMPLE)

    scale = size / max(art.width, art.height)
    art = art.resize(
        (max(1, round(art.width * scale)), max(1, round(art.height * scale))),
        Image.LANCZOS,
    )

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(art, ((size - art.width) // 2, (size - art.height) // 2), art)
    return canvas


def build_wide(svg_path: Path, height: int) -> Image.Image:
    """Return the wordmark scaled to an exact height, aspect preserved."""
    art = render(svg_path, height * SUPERSAMPLE)
    width = max(1, round(art.width * (height / art.height)))
    return art.resize((width, height), Image.LANCZOS)


def encode(image: Image.Image) -> bytes:
    """Return interlaced, losslessly optimised PNG bytes."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return oxipng.optimize_from_memory(
        buffer.getvalue(),
        level=6,
        interlace=oxipng.Interlacing.Adam7,
        strip=oxipng.StripChunks.safe(),
        optimize_alpha=True,
    )


def main() -> int:
    """Build every target and report what was written."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("custom_components/librenms/brand"),
        help="directory to write the PNG set into",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        help="use already-downloaded SVGs instead of fetching them",
    )
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        if args.source_dir:
            sources = args.source_dir
            print(f"Using local sources from {sources}")
        else:
            sources = Path(tmp)
            print(f"Fetching official LibreNMS artwork from {SOURCE_BASE}")
            fetch_sources(sources)

        print(f"\nWriting to {args.output}")
        for target in TARGETS:
            svg = sources / target.source
            image = (
                build_square(svg, target.size)
                if target.square
                else build_wide(svg, target.size)
            )
            data = encode(image)
            (args.output / target.filename).write_bytes(data)
            print(
                f"  {target.filename:<18} {image.width:>4}x{image.height:<4} "
                f"{len(data):>6} bytes   <- {target.source}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
