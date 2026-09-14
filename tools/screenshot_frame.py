"""Wrap a page screenshot in a browser window, for the README.

A bare screenshot of a web app reads as a mock-up; the same picture inside a
window with an address bar reads as software someone is using. This draws
that window in the brand's own colours — no stock frame, no drop-shadow
clip-art — and is reproducible, so every screenshot in docs/ matches.

    python tools/screenshot_frame.py shot.png "192.168.1.240:8880/atem/" out.png

Needs Pillow. The input should be a 2x capture (deviceScaleFactor 2); every
measurement below is in those device pixels.
"""
from __future__ import annotations

import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

SCALE = 2                      # the captures are 2x
BAR = 44 * SCALE               # title bar height
PAD = 10 * SCALE               # frame inset around the page
RADIUS = 12 * SCALE
SHADOW_BLUR = 18 * SCALE
SHADOW_DROP = 8 * SCALE
MARGIN = 28 * SCALE            # room around the window for the shadow

CHROME = '#1E2124'             # --surface-raised
EDGE = '#34383C'               # --border-strong
PILL = '#141618'               # --surface
URL_TEXT = '#9A9E9F'           # --text-muted
DOTS = ('#FF5F57', '#FEBC2E', '#28C840')

FONT_CANDIDATES = [
    'atem_control/static/brand/fonts/IBMPlexMono-400-latin.woff2',   # not loadable by PIL
    '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf',
    '/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf',
]


def _font(size: int):
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:      # noqa: BLE001 — try the next one
            continue
    return ImageFont.load_default()


def frame(page: Image.Image, url: str) -> Image.Image:
    page = page.convert('RGB')
    win_w = page.width + PAD * 2
    win_h = page.height + BAR + PAD
    out = Image.new('RGBA', (win_w + MARGIN * 2, win_h + MARGIN * 2), (0, 0, 0, 0))

    # the shadow: the window's silhouette, blurred, nudged down
    shadow = Image.new('RGBA', out.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (MARGIN, MARGIN + SHADOW_DROP, MARGIN + win_w, MARGIN + win_h + SHADOW_DROP),
        radius=RADIUS, fill=(0, 0, 0, 150))
    out.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(SHADOW_BLUR)))

    # the window itself
    win = Image.new('RGBA', (win_w, win_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(win)
    d.rounded_rectangle((0, 0, win_w - 1, win_h - 1), radius=RADIUS, fill=CHROME, outline=EDGE, width=SCALE)

    # traffic lights
    r = 6 * SCALE
    cx, cy = 18 * SCALE, BAR // 2
    for colour in DOTS:
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=colour)
        cx += 19 * SCALE

    # the address pill, centred, with the URL in mono
    pill_w, pill_h = min(int(win_w * 0.46), 520 * SCALE), 24 * SCALE
    px = (win_w - pill_w) // 2
    py = (BAR - pill_h) // 2
    d.rounded_rectangle((px, py, px + pill_w, py + pill_h), radius=pill_h // 2, fill=PILL)
    f = _font(13 * SCALE)
    tw = d.textlength(url, font=f)
    d.text((px + (pill_w - tw) / 2, py + pill_h / 2), url, font=f, fill=URL_TEXT, anchor='lm')

    # the page, with the bottom corners rounded like the window
    page_box = Image.new('RGBA', (page.width, page.height), (0, 0, 0, 0))
    mask = Image.new('L', (page.width, page.height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, page.width - 1, page.height - 1), radius=RADIUS // 2, fill=255)
    ImageDraw.Draw(mask).rectangle((0, 0, page.width - 1, RADIUS), fill=255)   # square at the top
    page_box.paste(page, (0, 0), mask)
    win.alpha_composite(page_box, (PAD, BAR))

    out.alpha_composite(win, (MARGIN, MARGIN))
    return out


def main() -> None:
    if len(sys.argv) != 4:
        print(__doc__)
        raise SystemExit(2)
    src, url, dest = sys.argv[1:4]
    frame(Image.open(src), url).save(dest)
    print(f'{dest}  {Image.open(dest).size[0]}x{Image.open(dest).size[1]}')


if __name__ == '__main__':
    main()
