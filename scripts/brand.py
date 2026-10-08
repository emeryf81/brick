"""B.R.I.C.K. brand kit generator: icon, logo (light/dark), banner, social preview.

Design system
- Ink      #111A2E  (text, dark backgrounds)
- Terracotta #E4572E (the brick: a real brick is terracotta)  light #F3865F  dark #B33E1C
- Sand     #F6B94F  (studs / the dots in the wordmark)
- Cream    #FFF6EA  (text on dark)
- Slate    #5D6680  (secondary text)
Type: Outfit ExtraBold for the wordmark, Inter SemiBold/Medium for supporting text (both OFL, converted to paths).

Usage: put Outfit.ttf and Inter.ttf (the variable fonts from Google Fonts) next to this script, then
`python scripts/brand.py` writes the SVGs to scripts/out/ (needs fonttools). The PNGs are renders of those SVGs.
"""
from __future__ import annotations

import math
from pathlib import Path

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

HERE = Path(__file__).parent
INK, INK2 = "#111A2E", "#1B2742"
TERRA, TERRA_L, TERRA_D, TERRA_TOP = "#E4572E", "#F3865F", "#B33E1C", "#F06B42"
SAND, SAND_D = "#F6B94F", "#D9952A"
CREAM, SLATE, SLATE_L = "#FFF6EA", "#5D6680", "#9AA6C2"

_FONTS: dict[tuple[str, int], TTFont] = {}


def font(name: str, wght: int) -> TTFont:
    key = (name, wght)
    if key not in _FONTS:
        f = TTFont(HERE / f"{name}.ttf")
        axes = {"wght": wght}
        if "opsz" in [a.axisTag for a in f["fvar"].axes]:
            axes["opsz"] = 28
        _FONTS[key] = instantiateVariableFont(f, axes)
    return _FONTS[key]


def text_path(s: str, x: float, y: float, size: float, name: str = "Outfit", wght: int = 800, tracking: float = 0.0,
              anchor: str = "start") -> tuple[str, float]:
    """The text as one SVG path (d attribute) and its width."""
    f = font(name, wght)
    upm = f["head"].unitsPerEm
    gs, cmap, hmtx = f.getGlyphSet(), f.getBestCmap(), f["hmtx"]
    k = size / upm
    width = sum(hmtx[cmap[ord(c)]][0] * k for c in s) + tracking * size * (len(s) - 1)
    if anchor == "middle":
        x -= width / 2
    elif anchor == "end":
        x -= width
    d, cx = [], x
    for c in s:
        g = cmap[ord(c)]
        pen = SVGPathPen(gs)
        gs[g].draw(TransformPen(pen, (k, 0, 0, -k, cx, y)))
        d.append(pen.getCommands())
        cx += hmtx[g][0] * k + tracking * size
    return " ".join(d), width


def glyph_width(c: str, size: float, name: str = "Outfit", wght: int = 800) -> float:
    f = font(name, wght)
    return f["hmtx"][f.getBestCmap()[ord(c)]][0] * size / f["head"].unitsPerEm


# ------------------------------------------------------------------ isometric brick
C30, S30 = math.cos(math.radians(30)), 0.5


def P(ox: float, oy: float, u: float, x: float, y: float, z: float) -> tuple[float, float]:
    return ox + (x - y) * C30 * u, oy + ((x + y) * S30 - z) * u


def pts(p: list[tuple[float, float]]) -> str:
    return " ".join(f"{a:.2f},{b:.2f}" for a, b in p)


def brick(ox: float, oy: float, u: float, w: int = 2, d: int = 2, h: float = 1.2, colors: tuple = (TERRA_TOP, TERRA, TERRA_D),
          stud: tuple = (TERRA_L, TERRA_D, TERRA), outline: str | None = None, sw: float = 0, at: tuple = (0, 0, 0)) -> str:
    """An isometric brick with its origin (back corner of the base) at ox, oy; u = one stud unit in px;
    at = where it stands in brick units (x, y, z)."""
    top, left, right = colors
    ax, ay, az = at
    q = lambda x, y, z: P(ox, oy, u, x + ax, y + ay, z + az)  # noqa: E731
    stroke = f' stroke="{outline}" stroke-width="{sw}" stroke-linejoin="round"' if outline else ""
    out = [
        f'<polygon points="{pts([q(0, d, 0), q(w, d, 0), q(w, d, h), q(0, d, h)])}" fill="{left}"{stroke}/>',
        f'<polygon points="{pts([q(w, 0, 0), q(w, d, 0), q(w, d, h), q(w, 0, h)])}" fill="{right}"{stroke}/>',
        f'<polygon points="{pts([q(0, 0, h), q(w, 0, h), q(w, d, h), q(0, d, h)])}" fill="{top}"{stroke}/>',
    ]
    r, sh = 0.3, 0.2
    rx, ry = r * 1.2247 * u, r * 0.7071 * u
    for sx, sy in sorted(((i + 0.5, j + 0.5) for i in range(w) for j in range(d)), key=lambda p: p[0] + p[1]):
        bx, by = q(sx, sy, h)
        tx, ty = q(sx, sy, h + sh)
        stud_top, stud_side, stud_edge = stud
        out.append(f'<path d="M{bx - rx:.2f},{by:.2f} A{rx:.2f},{ry:.2f} 0 0 0 {bx + rx:.2f},{by:.2f} L{tx + rx:.2f},{ty:.2f} '
                   f'A{rx:.2f},{ry:.2f} 0 0 1 {tx - rx:.2f},{ty:.2f} Z" fill="{stud_side}"/>')
        out.append(f'<ellipse cx="{tx:.2f}" cy="{ty:.2f}" rx="{rx:.2f}" ry="{ry:.2f}" fill="{stud_top}"/>')
    return "\n".join(out)


def brick_box(u: float, w: int = 2, d: int = 2, h: float = 1.2) -> tuple[float, float, float, float]:
    """Bounding box (minx, miny, maxx, maxy) of a brick drawn at origin 0,0."""
    ps = [P(0, 0, u, x, y, z) for x in (0, w) for y in (0, d) for z in (0, h + 0.2 + 0.25)]
    xs, ys = [p[0] for p in ps], [p[1] for p in ps]
    return min(xs), min(ys), max(xs), max(ys)


# ------------------------------------------------------------------ pieces
def icon_svg(size: int = 512, tile: bool = True) -> str:
    """The app icon: one 2x2 terracotta brick on an ink tile."""
    u = size * 0.205
    x0, y0, x1, y1 = brick_box(u)
    ox, oy = size / 2 - (x0 + x1) / 2, size / 2 - (y0 + y1) / 2 + size * 0.015
    shadow = f'<ellipse cx="{size / 2:.1f}" cy="{oy + y1 + size * 0.035:.1f}" rx="{size * 0.27:.1f}" ry="{size * 0.045:.1f}" fill="#000" opacity=".28" filter="url(#blur)"/>'
    bg = (f'<rect width="{size}" height="{size}" rx="{size * 0.22:.1f}" fill="url(#tile)"/>'
          f'<rect x="{size * 0.012:.1f}" y="{size * 0.012:.1f}" width="{size * 0.976:.1f}" height="{size * 0.976:.1f}" rx="{size * 0.21:.1f}" fill="none" stroke="#fff" stroke-opacity=".07" stroke-width="{size * 0.008:.1f}"/>') if tile else ""
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}">
<defs><linearGradient id="tile" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{INK2}"/><stop offset="1" stop-color="{INK}"/></linearGradient>
<filter id="blur" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="{size * 0.018:.1f}"/></filter></defs>
{bg}{shadow if tile else ""}
{brick(ox, oy, u)}
</svg>'''


def wordmark(x: float, baseline: float, size: float, color: str, dot: str = SAND) -> tuple[str, float]:
    """B.R.I.C.K. with the dots as round studs; returns svg and width."""
    out, cx = [], x
    gap, r = size * 0.035, size * 0.085
    for i, c in enumerate("BRICK"):
        d, w = text_path(c, cx, baseline, size)
        out.append(f'<path d="{d}" fill="{color}"/>')
        cx += w + gap
        out.append(f'<circle cx="{cx + r:.2f}" cy="{baseline - r:.2f}" r="{r:.2f}" fill="{dot}"/>')
        cx += 2 * r + (gap * 2.6 if i < 4 else 0)
    return "\n".join(out), cx - x


def logo_svg(dark: bool = False) -> str:
    """Horizontal logo: icon tile + wordmark + descriptor."""
    H = 300
    icon = icon_svg(240)
    inner = icon[icon.index(">") + 1:icon.rindex("</svg>")].replace('id="tile"', 'id="tileL"').replace("url(#tile)", "url(#tileL)") \
        .replace('id="blur"', 'id="blurL"').replace("url(#blur)", "url(#blurL)")
    wm, ww = wordmark(296, 168, 132, CREAM if dark else INK)
    sub, sw = text_path("BRICK REGISTRATION & INVENTORY CATALOGING KIT", 300, 226, 25.5, "Inter", 600, tracking=0.1)
    W = int(max(296 + ww, 300 + sw) + 40)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">
<g transform="translate(30 30)">{inner}</g>
{wm}
<path d="{sub}" fill="{SLATE_L if dark else SLATE}"/>
</svg>'''


def pattern_defs(u: float = 22) -> str:
    """A faint isometric grid of studs for dark backgrounds."""
    dx, dy = 2 * C30 * u, u
    rx, ry = 0.3 * 1.2247 * u, 0.3 * 0.7071 * u
    return f'''<pattern id="studs" width="{dx:.2f}" height="{dy:.2f}" patternUnits="userSpaceOnUse">
<ellipse cx="{dx / 2:.2f}" cy="{dy / 2:.2f}" rx="{rx:.2f}" ry="{ry:.2f}" fill="#fff" opacity=".045"/>
<ellipse cx="0" cy="0" rx="{rx:.2f}" ry="{ry:.2f}" fill="#fff" opacity=".045"/><ellipse cx="{dx:.2f}" cy="0" rx="{rx:.2f}" ry="{ry:.2f}" fill="#fff" opacity=".045"/>
<ellipse cx="0" cy="{dy:.2f}" rx="{rx:.2f}" ry="{ry:.2f}" fill="#fff" opacity=".045"/><ellipse cx="{dx:.2f}" cy="{dy:.2f}" rx="{rx:.2f}" ry="{ry:.2f}" fill="#fff" opacity=".045"/></pattern>'''


def stack(ox: float, oy: float, u: float) -> str:
    """A small composition of bricks: a 2x4 base, a 2x2 on top, a sand 1x2 accent."""
    g = []
    g.append(brick(ox, oy, u, w=4, d=2))                                                       # base
    g.append(brick(ox, oy, u, w=2, d=2, at=(0, 0, 1.2)))                                        # on top, back half
    g.append(brick(ox, oy, u, w=1, d=2, at=(2, 0, 1.2),
                   colors=("#FFD27A", SAND, SAND_D), stud=("#FFE3A6", SAND_D, SAND)))            # sand accent
    return "\n".join(g)


def banner_svg(W: int = 1280, H: int = 400) -> str:
    u = 46
    art = stack(250, 175, u)
    wm, ww = wordmark(560, 190, 116, CREAM)
    sub, _ = text_path("BRICK REGISTRATION & INVENTORY CATALOGING KIT", 564, 240, 20.5, "Inter", 600, tracking=0.12)
    tag, tw = text_path("The inventory app for your LEGO® sets in Home Assistant", 564, 300, 24, "Inter", 500)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">
<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{INK2}"/><stop offset="1" stop-color="{INK}"/></linearGradient>
<radialGradient id="glow" cx=".22" cy=".55" r=".5"><stop offset="0" stop-color="{TERRA}" stop-opacity=".22"/><stop offset="1" stop-color="{TERRA}" stop-opacity="0"/></radialGradient>
{pattern_defs()}</defs>
<rect width="{W}" height="{H}" fill="url(#bg)"/><rect width="{W}" height="{H}" fill="url(#studs)"/><rect width="{W}" height="{H}" fill="url(#glow)"/>
{art}
{wm}
<path d="{sub}" fill="{SAND}"/>
<path d="{tag}" fill="{SLATE_L}"/>
<rect x="564" y="268" width="{tw:.0f}" height="0" fill="none"/>
</svg>'''


def social_svg(W: int = 1280, H: int = 640) -> str:
    u = 52
    x0, y0, x1, y1 = -4 * C30 * u * 0 - 2 * C30 * u, -2.6 * u, 4 * C30 * u, 4 * u      # rough box of the stack
    art = stack(W / 2 - 1 * C30 * u, 200, u)
    wm, ww = wordmark(0, 0, 112, CREAM)
    wm2, ww = wordmark((W - ww) / 2, 420, 112, CREAM)
    sub, _ = text_path("BRICK REGISTRATION & INVENTORY CATALOGING KIT", W / 2, 470, 22, "Inter", 600, tracking=0.12, anchor="middle")
    tag, _ = text_path("The inventory app for your LEGO® sets in Home Assistant", W / 2, 522, 26, "Inter", 500, anchor="middle")
    feat, _ = text_path("Collection  ·  Value & return  ·  Watchlist  ·  Deals  ·  Price history", W / 2, 562, 19, "Inter", 500, anchor="middle")
    note, _ = text_path("Independent software. LEGO® is a trademark of the LEGO Group, which does not sponsor, authorize or endorse this project.",
                        W / 2, 612, 13.5, "Inter", 400, anchor="middle")
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">
<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{INK2}"/><stop offset="1" stop-color="{INK}"/></linearGradient>
<radialGradient id="glow" cx=".5" cy=".3" r=".45"><stop offset="0" stop-color="{TERRA}" stop-opacity=".22"/><stop offset="1" stop-color="{TERRA}" stop-opacity="0"/></radialGradient>
{pattern_defs()}</defs>
<rect width="{W}" height="{H}" fill="url(#bg)"/><rect width="{W}" height="{H}" fill="url(#studs)"/><rect width="{W}" height="{H}" fill="url(#glow)"/>
{art}
{wm2}
<path d="{sub}" fill="{SAND}"/>
<path d="{tag}" fill="{CREAM}"/>
<path d="{feat}" fill="{SLATE_L}"/>
<path d="{note}" fill="{SLATE}"/>
</svg>'''


if __name__ == "__main__":
    out = HERE / "out"
    out.mkdir(exist_ok=True)
    (out / "icon.svg").write_text(icon_svg())
    (out / "icon-transparent.svg").write_text(icon_svg(tile=False))
    (out / "logo.svg").write_text(logo_svg())
    (out / "logo-dark.svg").write_text(logo_svg(dark=True))
    (out / "banner.svg").write_text(banner_svg())
    (out / "social.svg").write_text(social_svg())
    print("ok")
