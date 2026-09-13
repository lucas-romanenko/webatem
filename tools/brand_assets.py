"""Generate every brand asset from the design handoff (docs/brand/handoff/README.md).

The mark is a control panel in miniature — the program bus (red) above the
preview bus (green) with the T-bar lever alongside — built from ratios of the
tile size S, so one function draws every size. Below 30 px the T-bar is
dropped (two bars only). Everything here is derived, nothing is hand-drawn:
re-run after changing a value.

    python tools/brand_assets.py          # needs Pillow, fonttools, brotli, uharfbuzz, icnsutil

Outputs (paths relative to the repo):
  docs/brand/                 mark / lockup / wordmark SVGs (dark + light), PNG exports
  build/desktop/              icon.svg, icon.png (1024), icon.icns, icon.ico, icon-512.png
  atem_control/static/        icon.png (1024)
  atem_control/static/brand/  favicon.ico, favicon-32.png, apple-touch-icon.png, icon-192/512.png,
                              mark.svg (inline-able), tray/*.png
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
FONTS = ROOT / 'atem_control' / 'static' / 'brand' / 'fonts'

DARK = dict(tile='#0A0B0C', border='#34383C', slot='#3C4145', handle='#EDEDEA', pgm='#D62718', pvw='#2FD07A')
LIGHT = dict(tile='#D3D2CD', border='#CFCEC9', slot='#A7A6A1', handle='#2A2A27', pgm='#B81E10', pvw='#1E9E57')
WORD_DARK = dict(web='#9A9E9F', atem='#EDEDEA')
WORD_LIGHT = dict(web='#6E706C', atem='#111312')


# ---------------------------------------------------------------------------
# Geometry — the handoff's ratios, verbatim.
# ---------------------------------------------------------------------------

def geometry(S: int, tbar: bool | None = None, inset: int = 0) -> dict:
    """Rects for a tile of size S at origin (inset, inset). tbar=None follows
    the 30 px rule."""
    if tbar is None:
        tbar = S >= 30
    r = round(S * 0.235)
    pad = max(2, round(S * 0.17))
    gap = max(2, round(S * 0.08))
    br = max(1, round(S * 0.045))
    x0 = y0 = inset + 1 + pad                    # border-box: 1 px border, then padding
    inner = S - 2 - 2 * pad
    g = {'S': S, 'inset': inset, 'radius': r, 'bar_radius': br, 'tbar': tbar}
    if tbar:
        slot_w = max(4, round(S * 0.22))
        left_w = inner - gap - slot_w
        bar_h = (inner - gap) / 2
        g['pgm'] = (x0, y0, x0 + left_w, y0 + bar_h)
        g['pvw'] = (x0, y0 + bar_h + gap, x0 + left_w, y0 + inner)
        sx = x0 + left_w + gap
        g['slot'] = (sx, y0, sx + slot_w, y0 + inner)
        hh = max(2, round(S * 0.13))
        hy = y0 + inner * 0.56
        g['handle'] = (sx, hy, sx + slot_w, hy + hh)
    else:
        bar_h = (inner - gap) / 2
        g['pgm'] = (x0, y0, x0 + inner, y0 + bar_h)
        g['pvw'] = (x0, y0 + bar_h + gap, x0 + inner, y0 + inner)
    return g


# ---------------------------------------------------------------------------
# SVG
# ---------------------------------------------------------------------------

def _rect(box, rx, fill, extra=''):
    x1, y1, x2, y2 = box
    return f'<rect x="{x1:.3f}" y="{y1:.3f}" width="{x2 - x1:.3f}" height="{y2 - y1:.3f}" rx="{rx}" fill="{fill}"{extra}/>'


def mark_svg_body(S: int, colors=DARK, tbar=None, inset=0, scalable=False) -> str:
    """The mark's elements (no <svg> wrapper) for a tile at (inset, inset)."""
    g = geometry(S, tbar, inset)
    stroke_extra = ' vector-effect="non-scaling-stroke"' if scalable else ''
    parts = [
        f'<rect x="{inset + 0.5}" y="{inset + 0.5}" width="{S - 1}" height="{S - 1}" rx="{g["radius"]}" '
        f'fill="{colors["tile"]}" stroke="{colors["border"]}" stroke-width="1"{stroke_extra}/>',
        _rect(g['pgm'], g['bar_radius'], colors['pgm']),
        _rect(g['pvw'], g['bar_radius'], colors['pvw']),
    ]
    if g['tbar']:
        parts.append(_rect(g['slot'], g['bar_radius'], colors['slot']))
        parts.append(_rect(g['handle'], g['bar_radius'], colors['handle']))
    return '\n  '.join(parts)


def mark_svg(S: int, colors=DARK, tbar=None, canvas: int | None = None, scalable=False) -> str:
    canvas = canvas or S
    inset = (canvas - S) // 2
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{canvas}" height="{canvas}" viewBox="0 0 {canvas} {canvas}">\n'
            f'  {mark_svg_body(S, colors, tbar, inset, scalable)}\n</svg>\n')


# ---------------------------------------------------------------------------
# Raster (Pillow, 4x supersampled)
# ---------------------------------------------------------------------------

def mark_png(S: int, colors=DARK, tbar=None, canvas: int | None = None, square_bg: str | None = None) -> Image.Image:
    """RGBA tile of size S centred on a canvas (default S). square_bg fills
    the whole canvas (the apple-touch icon: iOS rounds the corners itself)."""
    canvas = canvas or S
    ss = 4
    im = Image.new('RGBA', (canvas * ss, canvas * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if square_bg:
        d.rectangle((0, 0, canvas * ss, canvas * ss), fill=square_bg)
    inset = (canvas - S) // 2
    g = geometry(S, tbar, inset)

    def box(b):
        return tuple(v * ss for v in b)
    if not square_bg:
        d.rounded_rectangle(box((inset, inset, inset + S, inset + S)), radius=g['radius'] * ss, fill=colors['tile'],
                            outline=colors['border'], width=ss)
    d.rounded_rectangle(box(g['pgm']), radius=g['bar_radius'] * ss, fill=colors['pgm'])
    d.rounded_rectangle(box(g['pvw']), radius=g['bar_radius'] * ss, fill=colors['pvw'])
    if g['tbar']:
        d.rounded_rectangle(box(g['slot']), radius=g['bar_radius'] * ss, fill=colors['slot'])
        d.rounded_rectangle(box(g['handle']), radius=g['bar_radius'] * ss, fill=colors['handle'])
    return im.resize((canvas, canvas), Image.LANCZOS)


def template_png(S: int) -> Image.Image:
    """macOS menu-bar template image: black silhouette of the tile with the
    two bus bars knocked out (the 30 px rule puts the tray in two-bar form)."""
    ss = 4
    g = geometry(S, tbar=False)
    mask = Image.new('L', (S * ss, S * ss), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle((0, 0, S * ss - 1, S * ss - 1), radius=g['radius'] * ss, fill=255)
    for key in ('pgm', 'pvw'):
        d.rounded_rectangle(tuple(v * ss for v in g[key]), radius=g['bar_radius'] * ss, fill=0)
    mask = mask.resize((S, S), Image.LANCZOS)
    im = Image.new('RGBA', (S, S), (0, 0, 0, 255))
    im.putalpha(mask)
    return im


# ---------------------------------------------------------------------------
# Wordmark: Archivo outlines to SVG paths (shaped with HarfBuzz so kerning is real)
# ---------------------------------------------------------------------------

def _font(weight: int):
    from fontTools.ttLib import TTFont
    return TTFont(FONTS / f'Archivo-{weight}-latin.woff2')


def _shape(text: str, weight: int, size: float, tracking_em: float):
    """[(glyph_name, x_offset, y_offset, advance)] in px, and the total advance."""
    import uharfbuzz as hb
    # HarfBuzz reads sfnt, not WOFF2: decompress with fontTools first.
    sfnt = io.BytesIO()
    tt0 = _font(weight)
    tt0.flavor = None
    tt0.save(sfnt)
    face = hb.Face(sfnt.getvalue())
    font = hb.Font(face)
    upem = face.upem
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(font, buf, {'kern': True, 'liga': True})
    tt = _font(weight)
    order = tt.getGlyphOrder()
    scale = size / upem
    out, x = [], 0.0
    for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
        out.append((order[info.codepoint], x + pos.x_offset * scale, pos.y_offset * scale))
        x += pos.x_advance * scale + tracking_em * size
    x -= tracking_em * size          # no tracking after the last glyph
    return out, x, tt


def wordmark_paths(size: float, colors=WORD_DARK, x0: float = 0.0, baseline: float = 0.0) -> tuple[str, float]:
    """SVG <path> elements for 'web' (400) + 'ATEM' (800), letter-spacing
    -0.03em, drawn on one baseline. Returns (svg, total width)."""
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen
    parts, x = [], x0
    for text, weight, color in (('web', 400, colors['web']), ('ATEM', 800, colors['atem'])):
        glyphs, adv, tt = _shape(text, weight, size, -0.03)
        gs = tt.getGlyphSet()
        upem = tt['head'].unitsPerEm
        s = size / upem
        for name, gx, gy in glyphs:
            pen = SVGPathPen(gs)
            tpen = TransformPen(pen, (s, 0, 0, -s, x + gx, baseline - gy))
            gs[name].draw(tpen)
            dpath = pen.getCommands()
            if dpath:
                parts.append(f'<path d="{dpath}" fill="{color}"/>')
        x += adv - (-0.03 * size)      # the tracking between 'web' and 'ATEM' is the same -0.03em
    x += -0.03 * size
    return '\n  '.join(parts), x - x0


def wordmark_metrics(size: float):
    tt = _font(800)
    hhea = tt['hhea']
    upem = tt['head'].unitsPerEm
    return hhea.ascent * size / upem, -hhea.descent * size / upem, tt['OS/2'].sCapHeight * size / upem


def lockup_svg(mark: int, word: float, gap: float, colors, wcolors, pad: float = 0.0) -> str:
    """Mark and wordmark on one horizontal line: the wordmark's cap height is
    centred on the mark (what the board does optically)."""
    asc, desc, cap = wordmark_metrics(word)
    baseline = pad + mark / 2 + cap / 2
    paths, w = wordmark_paths(word, wcolors, x0=pad + mark + gap, baseline=baseline)
    W = pad + mark + gap + w + pad
    H = pad + mark + pad
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" viewBox="0 0 {W:.2f} {H:.2f}">\n'
            f'  {mark_svg_body(mark, colors, True, int(pad), scalable=True)}\n  {paths}\n</svg>\n')


def wordmark_svg(word: float, wcolors) -> str:
    asc, desc, cap = wordmark_metrics(word)
    paths, w = wordmark_paths(word, wcolors, x0=0, baseline=asc)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" height="{asc + desc:.0f}" viewBox="0 0 {w:.2f} {asc + desc:.2f}">\n'
            f'  {paths}\n</svg>\n')


# ---------------------------------------------------------------------------
# Icon containers
# ---------------------------------------------------------------------------

def write_ico(path: Path, sizes=(16, 24, 32, 48, 64, 128, 256)):
    """Pillow keeps only the sizes that fit the FIRST image, and uses an
    appended image of the exact size where one is given — so largest first,
    each size drawn on its own (16 and 24 in two-bar form)."""
    order = sorted(sizes, reverse=True)
    ims = [mark_png(s) for s in order]
    ims[0].save(path, format='ICO', sizes=[(s, s) for s in order], append_images=ims[1:])


def write_icns(path: Path):
    """The macOS icon: Apple's grid puts the rounded square at 824/1024 with
    a margin, so every size is the mark on that grid; the 16 px entry is the
    two-bar form."""
    import icnsutil
    icns = icnsutil.IcnsFile()
    # (canvas px, key)
    for px, key in ((16, 'icp4'), (32, 'icp5'), (64, 'icp6'), (128, 'ic07'), (256, 'ic08'),
                    (512, 'ic09'), (1024, 'ic10'), (32, 'ic11'), (64, 'ic12'), (256, 'ic13'), (512, 'ic14')):
        tile = round(px * 824 / 1024)
        im = mark_png(tile, canvas=px)
        b = io.BytesIO()
        im.save(b, 'PNG')
        icns.add_media(key, data=b.getvalue())
    icns.write(path)


def main() -> None:
    brand = ROOT / 'docs' / 'brand'
    static = ROOT / 'atem_control' / 'static'
    sb = static / 'brand'
    desk = ROOT / 'build' / 'desktop'
    for d in (brand, sb, sb / 'tray', desk):
        d.mkdir(parents=True, exist_ok=True)

    # --- docs/brand: masters ---------------------------------------------
    (brand / 'mark.svg').write_text(mark_svg(1024))
    (brand / 'mark-light.svg').write_text(mark_svg(1024, LIGHT))
    (brand / 'mark-16.svg').write_text(mark_svg(16))
    (brand / 'mark-16-light.svg').write_text(mark_svg(16, LIGHT))
    (brand / 'lockup-dark.svg').write_text(lockup_svg(56, 34, 20, DARK, WORD_DARK, pad=4))
    (brand / 'lockup-light.svg').write_text(lockup_svg(56, 34, 20, LIGHT, WORD_LIGHT, pad=4))
    (brand / 'wordmark-dark.svg').write_text(wordmark_svg(56, WORD_DARK))
    (brand / 'wordmark-light.svg').write_text(wordmark_svg(56, WORD_LIGHT))
    for s in (112, 56, 32, 16):
        mark_png(s).save(brand / f'mark-{s}.png')
        mark_png(s, LIGHT).save(brand / f'mark-{s}-light.png')
    mark_png(512).save(brand / 'mark-512.png')

    # --- desktop icons -----------------------------------------------------
    (desk / 'icon.svg').write_text(mark_svg(824, canvas=1024))          # Apple's grid
    mark_png(824, canvas=1024).save(desk / 'icon.png')
    write_icns(desk / 'icon.icns')
    write_ico(desk / 'icon.ico')
    mark_png(512).save(desk / 'icon-512.png')

    # --- web -----------------------------------------------------------------
    mark_png(1024).save(static / 'icon.png')                             # PWA / generic
    write_ico(sb / 'favicon.ico', sizes=(16, 32, 48))
    mark_png(32).save(sb / 'favicon-32.png')
    mark_png(180, canvas=180, square_bg=DARK['tile']).save(sb / 'apple-touch-icon.png')
    mark_png(192).save(sb / 'icon-192.png')
    mark_png(512).save(sb / 'icon-512.png')
    (sb / 'mark.svg').write_text(mark_svg(1024, scalable=True))          # inline-able, scales with CSS

    # --- tray / menu bar -----------------------------------------------------
    template_png(22).save(sb / 'tray' / 'mac-template-22.png')
    template_png(44).save(sb / 'tray' / 'mac-template-44.png')
    mark_png(16).save(sb / 'tray' / 'win-16.png')
    mark_png(32).save(sb / 'tray' / 'win-32.png')
    mark_png(24).save(sb / 'tray' / 'linux-24.png')
    print('brand assets written')


if __name__ == '__main__':
    sys.exit(main())
