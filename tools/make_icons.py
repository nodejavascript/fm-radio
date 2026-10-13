#!/usr/bin/env python3
"""Draw this site's icon set — a tuning dial, in the site's own amber on warm near-black.

    python3 tools/make_icons.py

Everything is drawn rather than downloaded: no paid stock, no licensed font, and no designer's
file to keep in step. The set the family requires is written in one pass, and the 180 PNG is the
one Google actually reads (its accepted favicon formats do not include SVG), so it is produced at
exactly 180x180 and the SVG is written beside it for browsers, which prefer it.

Requires Pillow. The SVG needs nothing at all.
"""

from __future__ import annotations

import math
import os

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(os.path.dirname(HERE), "site")
# 🔴 THE ICONS LIVE AT THE SITE ROOT, NOT IN A FOLDER — measured 13 October 2026 by the SEO audit,
# which asks for `/favicon.ico`, `/favicon-180.png`, `/favicon-32.png`, `/apple-touch-icon.png` and
# `/favicon.svg` and reported all five missing while they sat correctly in `/img/`. That is the
# convention every other site in the family keeps, and a crawler reads it without being told.
# The social card stays in `img/` because it is not an icon.
OUT = ROOT
IMG = os.path.join(ROOT, "img")

BG = (20, 15, 8)
AMBER = (245, 165, 36)
INK = (247, 240, 228)
MUTED = (163, 144, 122)

TICKS = 24


def draw_dial(size: int) -> Image.Image:
    """The mark: a ring of ticks with a needle, drawn large and reduced for a clean edge."""
    ss = 8
    s = size * ss
    im = Image.new("RGB", (s, s), BG)
    d = ImageDraw.Draw(im, "RGBA")

    # the lamp's glow behind everything, so the badge is not flat
    for r in range(int(s * 0.62), 0, -2):
        a = int(26 * (1 - r / (s * 0.62)) ** 2)
        d.ellipse((s / 2 - r, s / 2 - r, s / 2 + r, s / 2 + r), fill=AMBER + (a,))

    cx = cy = s / 2
    r_out = s * 0.40
    r_in = s * 0.31
    w = max(ss, int(s * 0.018))

    # the scale: 24 ticks, the ones on the quarters longer and brighter
    for i in range(TICKS):
        ang = -math.pi / 2 + i * (2 * math.pi / TICKS)
        major = i % 6 == 0
        inner = r_in - (s * 0.035 if major else 0)
        col = AMBER + (235 if major else 150,)
        d.line(
            [cx + inner * math.cos(ang), cy + inner * math.sin(ang), cx + r_out * math.cos(ang), cy + r_out * math.sin(ang)],
            fill=col,
            width=w * (2 if major else 1),
        )

    # the needle, standing between two ticks, with a darker casing so it reads over the glow
    ang = -math.pi / 2 + 2 * (2 * math.pi / TICKS) + 0.14
    d.line(
        [cx, cy, cx + (r_out - s * 0.02) * math.cos(ang), cy + (r_out - s * 0.02) * math.sin(ang)],
        fill=BG + (255,),
        width=w * 4,
    )
    d.line(
        [cx, cy, cx + (r_out - s * 0.02) * math.cos(ang), cy + (r_out - s * 0.02) * math.sin(ang)],
        fill=AMBER + (255,),
        width=w * 2,
    )

    # the hub
    hub = s * 0.075
    d.ellipse((cx - hub, cy - hub, cx + hub, cy + hub), fill=AMBER + (255,))
    hub2 = s * 0.032
    d.ellipse((cx - hub2, cy - hub2, cx + hub2, cy + hub2), fill=BG + (255,))

    return im.resize((size, size), Image.LANCZOS)


def svg() -> str:
    """The same drawing as a vector, for the browsers that prefer it.

    🔴 THE GLOW DISC STAYS, AND IT IS NOT DECORATION IN THE SET — 13 October 2026. Part 15c's
    signature keeps a circle's CENTRE and throws its radius away, so this disc is the shape
    `c(32.0,32.0)` and the bar must carry the same one or the two drawings count as different.
    The bar therefore repeats it with its OWN gradient id (`gm-head`, `gm-foot`), which is safe
    because the signature ignores fill and colour — and two elements on one page cannot share
    an id. An earlier pass removed the disc instead, and the mark read as mismatched.
    """
    L = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="FM radio">']
    L.append('  <defs><radialGradient id="g" cx="50%" cy="50%" r="60%">')
    L.append('    <stop offset="0" stop-color="#f5a524" stop-opacity=".22"/>')
    L.append('    <stop offset="1" stop-color="#f5a524" stop-opacity="0"/>')
    L.append("  </radialGradient></defs>")
    L.append('  <rect width="64" height="64" rx="14" fill="#140f08"/>')
    L.append('  <circle cx="32" cy="32" r="30" fill="url(#g)"/>')
    L.append('  <g stroke="#f5a524" stroke-linecap="round">')
    for i in range(TICKS):
        ang = -math.pi / 2 + i * (2 * math.pi / TICKS)
        major = i % 6 == 0
        r_in, r_out = (18.4, 27.2) if major else (20.6, 27.2)
        x1, y1 = 32 + r_in * math.cos(ang), 32 + r_in * math.sin(ang)
        x2, y2 = 32 + r_out * math.cos(ang), 32 + r_out * math.sin(ang)
        L.append(
            '    <line x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" stroke-width="%s" stroke-opacity="%s"/>'
            % (x1, y1, x2, y2, "2.6" if major else "1.3", "0.95" if major else "0.6")
        )
    L.append("  </g>")
    ang = -math.pi / 2 + 2 * (2 * math.pi / TICKS) + 0.14
    x2, y2 = 32 + 26.8 * math.cos(ang), 32 + 26.8 * math.sin(ang)
    L.append('  <line x1="32" y1="32" x2="%.2f" y2="%.2f" stroke="#140f08" stroke-width="5.4" stroke-linecap="round"/>' % (x2, y2))
    L.append('  <line x1="32" y1="32" x2="%.2f" y2="%.2f" stroke="#f5a524" stroke-width="2.8" stroke-linecap="round"/>' % (x2, y2))
    L.append('  <circle cx="32" cy="32" r="5.2" fill="#f5a524"/>')
    L.append('  <circle cx="32" cy="32" r="2.2" fill="#140f08"/>')
    L.append("</svg>")
    return "\n".join(L) + "\n"


def font(size: int, bold: bool = True):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def wrap(text: str, fnt, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if fnt.getlength(trial) <= width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def og() -> Image.Image:
    """1200x630, the shape every social card is cut to: the belief, then the address."""
    W, H = 1200, 630
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im, "RGBA")

    for r in range(760, 0, -3):
        a = int(40 * (1 - r / 760) ** 3)
        d.ellipse((W * 0.80 - r, 120 - r, W * 0.80 + r, 120 + r), fill=AMBER + (a,))

    # the dial's ticks along the bottom, like the site's own drawing
    for x in range(70, W - 60, 15):
        tall = (x - 70) % 75 == 0
        d.line((x, H - 40, x, H - 40 - (46 if tall else 24)), fill=AMBER + (190 if tall else 80,), width=2)

    x, y = 78, 150
    f1 = font(66)
    for line in wrap("A station is told from hiss by evidence, not by loudness.", f1, 900):
        d.text((x, y), line, font=f1, fill=INK)
        y += 76

    d.line((x, y + 22, x + 120, y + 22), fill=AMBER + (255,), width=4)
    y += 60
    d.text((x, y), "FM radio in a container · one Python file · MIT", font=font(34, False), fill=MUTED)
    d.text((W - 78 - font(32).getlength("fm-radio.nodejavascript.com"), H - 120),
           "fm-radio.nodejavascript.com", font=font(32), fill=AMBER)

    # the mark, big, at the right
    mark = draw_dial(300)
    im.paste(mark, (W - 300 - 70, 210))
    return im


def main() -> None:
    os.makedirs(OUT, exist_ok=True)

    icon180 = draw_dial(180)
    icon180.save(os.path.join(OUT, "favicon-180.png"))
    # iOS paints transparency black, so the touch icon is the same drawing with no alpha at all
    icon180.save(os.path.join(OUT, "apple-touch-icon.png"))
    draw_dial(32).save(os.path.join(OUT, "favicon-32.png"))
    draw_dial(512).save(os.path.join(IMG, "icon-512.png"))
    draw_dial(256).save(
        os.path.join(OUT, "favicon.ico"), sizes=[(16, 16), (32, 32), (48, 48)]
    )
    with open(os.path.join(OUT, "favicon.svg"), "w", encoding="utf-8") as fh:
        fh.write(svg())
    og().save(os.path.join(IMG, "og.png"))

    for name in sorted(os.listdir(OUT)):
        p = os.path.join(OUT, name)
        print("  %-22s %7d bytes" % (name, os.path.getsize(p)))


if __name__ == "__main__":
    main()
