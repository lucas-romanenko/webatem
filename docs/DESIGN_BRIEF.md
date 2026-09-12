# WebATEM — design brief

A brief for a design package: logo, colour scheme, app and tray icons in every
size, README art — and the work of applying them to this repository. Read all
of it before starting; the "Constraints" and "How things are wired" sections
decide where files go and what must not be touched.

## What WebATEM is

WebATEM is an open-source control surface for **Blackmagic ATEM video
switchers** that runs in a browser. It finds the switchers on the network and
gives the operator everything ATEM Software Control gives: program / preview
buses, cut / auto / the T-bar, transitions (mix, dip, wipe, DVE, stinger),
upstream and downstream keyers (luma, chroma, pattern, DVE), the Fairlight
audio mixer with per-channel EQ and dynamics, the media pool with drag-and-drop
uploads, macros, HyperDeck transport, and switcher-state save / load. One
person at a desk, a phone in a studio, or several people at once — it is a URL.

It ships three ways, all from one `v*` tag:

- **Desktop launcher** (macOS `.dmg` for Apple silicon and Intel, Windows
  `.exe`, Linux binary): a menu-bar / system-tray app in the shape of
  **Bitfocus Companion** — an icon in the menu bar / notification area (never
  the Dock or taskbar), a small launcher window (status band with *Running* +
  the address, interface, port, *Start minimized*, *Run at login*, **Launch
  GUI** / Hide / Quit). Launch GUI opens the control page in the browser.
- **Docker image** on a Linux box on the switcher LAN (`ghcr.io/lucas-romanenko/webatem`).
- **PyPI package** (`pipx install webatem`).

Users: broadcast and AV technicians, studio operators, streamers, anyone who
runs ATEMs and wants control from any device. The tone is professional
broadcast equipment — calm, precise, dark, a real control panel — not a
consumer app.

## Similar apps and what their visual language does

- **ATEM Software Control** (Blackmagic): neutral dark greys, raised buttons
  with a backlit glow when lit, broadcast tally colours — red program, green
  preview, amber / yellow for keys and BKGD. WebATEM's control page already
  follows this ("like a real control panel" is a settled decision; the
  backlit glow and rounded raised buttons stay).
- **Bitfocus Companion**: the launcher shape WebATEM copies — a black header
  with the logo, a coloured status band, a plain settings form; a simple
  three-item tray menu. Its brand is a flat red-orange mark on black.
- **Blackmagic hardware panels**, **OBS Studio**, **Elgato Stream Deck**: the
  same family — dark surfaces, few colours, colour means state.

Do not copy anyone's logo. "ATEM" and "Blackmagic" are Blackmagic Design's
trademarks, used descriptively in text only; the mark must not imitate their
logos or panel graphics. Nothing from the private upstream project this was
extracted from (a corporate AV platform) may appear — no third-party brand
colours, crowns, or wordmarks.

## What exists today (the placeholders to replace)

- **Icon**: `build/desktop/icon.svg` (1024 viewBox; a dark grey rounded square
  with amber and red elements — a placeholder), exported to
  `build/desktop/icon.png` (1024×1024), `build/desktop/icon.icns`,
  `build/desktop/icon.ico`. `atem_control/static/icon.png` is the same PNG and
  is used as the favicon and apple-touch-icon (`webatem/templates/base.html`),
  the launcher window header image at 88×88 CSS px
  (`webatem/templates/launcher.html`), and the **tray / menu-bar icon**
  (`_tray_image` in `webatem/launcher.py` loads it with Pillow) — a colour
  icon there is wrong for the macOS menu bar, which wants a monochrome
  template image.
- **Colour tokens**: `atem_control/static/css/theme.css`, DaisyUI 4 variables in
  OKLCH. The dark theme today: primary `--p: 73.85% 0.1646 56.80` (an orange,
  ≈ #F68B2A) on black content; base-100 / 200 / 300 ≈ #26282B / #2E3033 /
  #36393D; base-content white; success green, warning yellow, error
  `69.42% 0.1975 17.51` (≈ #FF5C6E); program tally `--pgm-red: #FF4557`
  (deliberately separate from error); `--rounded-btn: 10px`,
  `--rounded-box: 16px`. Read the file for the full set.
- **Type**: Poppins, self-hosted (`atem_control/static/fonts/`), licence OFL.
  Tailwind 3 + DaisyUI 4 compiled by `npm run build:css` into
  `atem_control/static/vendor/webatem.css`.
- **Launcher window**: `webatem/templates/launcher.html`, 520×700, black
  header (icon + "WebATEM 0.2.x"), a primary-colour status band, the form.
  The window's loading page is `_LOADING_HTML` in `webatem/launcher.py`
  (dark #111, an orange spinner #f68b2a, "WebATEM is starting…").
- **README**: text + three shields.io download badges, four screenshots under
  `docs/` (control surface, settings panel, Fairlight mixer, EQ). No logo,
  no hero image. The GitHub repository has no social-preview image.

## Deliverables

Produce every file below, commit them on a branch, and apply them (the
"Applying" section). Master files are SVG; raster exports are PNG with alpha
unless stated. Sizes are pixels.

1. **Logo system** — `docs/brand/`: `logo.svg` (mark + wordmark), `logo-mark.svg`,
   `logo-wordmark.svg`, each in light-on-dark and dark-on-light variants
   (`*-light.svg` / `*-dark.svg`), plus `logo-mono.svg` (single colour). PNG
   exports at 512 px height for the mark and 1600 px width for the lockup.
   The mark must read at 16 px.
2. **App icon** — master `build/desktop/icon.svg` (1024 viewBox);
   `build/desktop/icon.png` 1024×1024; `atem_control/static/icon.png` (same);
   **macOS** `build/desktop/icon.icns` with the full set
   (16, 32, 64, 128, 256, 512, 1024 — i.e. `icon_16x16` … `icon_512x512@2x`),
   drawn on the macOS rounded-square ("squircle") with the icon's own
   background per Apple's HIG; **Windows** `build/desktop/icon.ico` with
   16, 24, 32, 48, 64, 128, 256 (Pillow writes multi-size ICO; `icnsutil` from
   PyPI writes ICNS on Linux, `iconutil` on macOS); **Linux**
   `build/desktop/icon-512.png`.
3. **Menu-bar / tray icons** — `atem_control/static/tray/`: macOS
   **template image** (black + alpha only, no colour) at 22×22 and 44×44
   (`mac-template.png`, `mac-template@2x.png`); Windows colour tray icon at
   16×16 and 32×32 (`win-16.png`, `win-32.png`) and `tray.ico` holding both;
   Linux 22×22 and 24×24 (`linux-22.png`, `linux-24.png`). Then change
   `_tray_image` in `webatem/launcher.py` to pick per platform.
4. **Favicons** — `atem_control/static/`: `favicon.ico` (16, 32, 48),
   `icon-192.png`, `icon-512.png`, `apple-touch-icon.png` (180×180, no alpha,
   the icon's own background); update the `<link>` tags in
   `webatem/templates/base.html`.
5. **Launcher window art** — the mark for the header at 88×88 CSS px: an
   inline SVG or `launcher-mark@2x.png` (176×176) and `@3x` (264×264); restyle
   the header, status band and buttons to the new palette; a matching
   `_LOADING_HTML` (colours, spinner, optionally the mark as a data URI).
6. **README art** — `docs/brand/social-preview.png` **1280×640** (also to be
   uploaded as the GitHub social preview under repository settings), a README
   header lockup (SVG, rendered ≤ 480 px wide, light-on-dark works on both
   GitHub themes — use a `<picture>` with `prefers-color-scheme` if two
   variants), download buttons in the palette (own SVG buttons under
   `docs/brand/buttons/` or shields.io with custom hex colours).
7. **Colour scheme** — a palette with named roles, contrast-checked (WCAG AA
   for text on every surface it sits on), delivered as DaisyUI 4 token values
   for a dark theme (and, optionally, a light theme): `--p --pc --s --sc --a
   --ac --n --nc --b1 --b2 --b3 --bc --in --su --wa --er`, plus `--pgm-red`.
   Rules that hold: program tally is a saturated broadcast red distinct from
   the error colour; preview is green; keys / BKGD are amber / yellow; unlit
   buttons stay raised neutral grey; lit buttons keep the backlit glow.
8. **Type scale** — keep Poppins unless something licence-clean is clearly
   better; give sizes / weights for the launcher window (title, status,
   labels, buttons) and the control page headings.
9. **Optional**: a DMG background 660×400 at @2x (`build/desktop/dmg-background.png`)
   for a drag-to-Applications window (the workflow would need a create-dmg
   step — note it, do not build it unless asked).
10. **Screenshots** — after the theme lands, retake the four under `docs/`
    at 2× device scale (run the app, open `/launcher/`, `/atem/` and the
    control page; a switcher is needed for the control-page shots — if none
    is on the network, leave those and say so).

## How things are wired (read before editing)

- `atem_control/static/css/theme.css` is **generated**: `tools/sync_from_av_server.py`
  rebuilds it from the upstream project's theme on every sync (its THEME
  step). Do not hand-edit it. Put the brand palette in a new
  `atem_control/static/css/brand.css` loaded **after** `theme.css` in
  `webatem/templates/base.html` (DaisyUI reads the tokens from the last
  `[data-theme=dark]` rule that sets them), or add a BRAND step to the sync
  tool that applies the override — say which you did.
- The compiled stylesheet is not committed; CI and the Docker build run
  `npm run build:css`. New utility classes need that build.
- `[hidden] { display: none !important }` sits at the end of `theme.css`; keep
  it winning.
- Icons are committed files; no workflow generates them. PyInstaller reads
  `build/desktop/icon.icns` / `icon.ico` (`build/desktop/webatem.spec`).
- The launcher's tray image is loaded with Pillow; pystray on macOS shows the
  image as given (design the template image black-on-transparent).

## Verify

- `python -m pytest tests/ -q` — green (a test pins that the launcher page
  renders; add one that the new files exist and the tray picks per platform).
- `python -m webatem` (or Docker: `docker compose up`): `/launcher/` and
  `/atem/` render with the new theme; nothing white-on-white; tally colours
  unchanged in meaning.
- Every icon size opens; the macOS template image has no colour channel
  content; the favicon shows in a tab.
- Show the result as a rendered artifact (icons at real sizes on light and
  dark, the palette with contrast ratios, the launcher window, the README
  header) before merging.

## Legal

Original artwork only. No Blackmagic, Bitfocus, Apple or Microsoft marks.
Fonts under OFL or Apache. If any generated asset derives from a stock or
AI source, say so and keep the licence.
