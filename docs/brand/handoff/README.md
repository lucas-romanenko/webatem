# Handoff: WebATEM Brand & UI System

## Overview

Visual identity and interface system for **WebATEM** — a self-hosted server that a user
downloads and runs locally, exposing ATEM switcher control through any browser on the LAN.

This bundle covers the logo, color, typography, iconography, and the applied styling of the
control panel UI itself. Implement it as the app's visual layer.

## About the design files

`WebATEM Brand.dc.html` in this folder is a **design reference created in HTML** — a brand
board showing the intended look, not production code to copy. Recreate these values in
WebATEM's existing front end using its established patterns. If there is no front-end
convention yet, pick one appropriate to the project and implement the tokens below as CSS
custom properties.

The board also contains a **mark study** (section 01) showing rejected logo alternatives.
Those are documentation of the decision, not shipping assets. Only candidate **B** ships.

## Fidelity

**High fidelity.** All colors, type, sizes, and radii are final. Match them exactly.

---

## 1. Logo

### The mark

A miniature control panel: a program bus bar above a preview bus bar, with the T-bar
lever alongside.

Structure, at tile size `S`:

- Tile: `S × S`, `border-radius: round(S * 0.235)`, `background: #0A0B0C`,
  `border: 1px solid #34383C`, `box-sizing: border-box`,
  `padding: max(2, round(S * 0.17))`
- Tile is `display: flex; gap: max(2, round(S * 0.08))`
- **Left column** — `flex: 1`, `display: flex; flex-direction: column`, same gap:
  - Top bar: `flex: 1`, `background: #D62718` (PGM), `border-radius: max(1, round(S * 0.045))`
  - Bottom bar: `flex: 1`, `background: #2FD07A` (PVW), same radius
- **Right column** — the T-bar slot: `width: max(4, round(S * 0.22))`, same radius,
  `background: #3C4145`, `position: relative`
  - Handle: absolutely positioned, `left: 0; right: 0; top: 56%`,
    `height: max(2, round(S * 0.13))`, `background: #EDEDEA`, same radius

Because every dimension is a ratio of `S`, one implementation covers all sizes.

**Light-ground variant** (on paper `#F2F1EE` or similar): tile `#D3D2CD`,
border `#CFCEC9`, slot `#A7A6A1`, handle `#2A2A27`, PGM `#B81E10`, PVW `#1E9E57`.

### Small-size fallback — required

**Below 30px the T-bar is dropped.** The slot and handle collapse into noise. Under 30px
render the tile as `flex-direction: column` with only the two bus bars — red above green,
no slot, no right column. This is the 16px favicon form.

### Wordmark

`webATEM`, set solid with no space, in Archivo:

- `web` — weight **400**, color `#9A9E9F` (dark ground) / `#6E706C` (light ground)
- `ATEM` — weight **800**, color `#EDEDEA` (dark) / `#111312` (light)
- `letter-spacing: -0.03em`, `line-height: 1`

The weight and color contrast is the whole idea; do not equalize them.

### Lockup

Mark and wordmark on a horizontal baseline, `gap: 20px` at a 56px mark and 34px wordmark.
Scale the gap proportionally. The mark always sits left.

### Secondary wordmarks

- `web/atem` — IBM Plex Mono 600, the `/` in `#FF7A6B`. For CLI output, code, docs.
- `WEBATEM` — Archivo 600, `letter-spacing: 0.16em`. For small labels and stamps.

---

## 2. Color

| Token | Hex | Use |
|---|---|---|
| `--ink` | `#0E0F10` | Page background |
| `--surface` | `#141618` | Cards, panels |
| `--surface-raised` | `#1E2124` | Buttons, inputs, chrome |
| `--border` | `#26292C` | Card and section dividers |
| `--border-strong` | `#34383C` | Control outlines, logo tile edge |
| `--text` | `#EDEDEA` | Primary text |
| `--text-muted` | `#9A9E9F` | Secondary text |
| `--text-faint` | `#7E8386` | Labels, captions, mono metadata |
| `--pgm` | `#D62718` | Program bus — also the brand red |
| `--pvw` | `#2FD07A` | Preview bus — also the brand green |
| `--link` | `#FF7A6B` | Links and accent text on dark grounds |
| `--paper` | `#F2F1EE` | Light-ground background |

### Rules

1. **The state colors are the brand.** Red and green are not reserved away from the
   identity — they carry it. There is no separate neutral accent.
2. **`--pgm` is darkened from the usual hardware value** so white label text on it clears
   4.5:1. Do not substitute a brighter red for button fills carrying text.
3. **Never use `--pgm` as a fill for anything that is not live**, and never `--pvw` for
   anything that is not cued. Outside the logo, these two colors mean state.
4. **`--link` (`#FF7A6B`) is text-only.** It is a tint for legibility on dark grounds; it
   is not a fill. Hover: `#FF9A8E`.

### Contrast

- White `#FFFFFF` on `--pgm` — 5.0:1, passes.
- Ink `#0E0F10` on `--pvw` — passes; always use dark ink on green, never white.
- Offline / inactive controls: `#9A9E9F` text on `#1E2124` with a `#34383C` border.

---

## 3. Typography

Two families, both on Google Fonts:

```
Archivo — 400, 500, 600, 800
IBM Plex Mono — 400, 500, 600
```

### Division of labor

**Archivo** is the interface and prose face. **IBM Plex Mono is for anything a machine
reported** — IP addresses, ports, source numbers, timecode, bus labels, version strings,
status. This split is load-bearing; it is how the UI signals what is live data versus what
is chrome.

### Scale

| Role | Family | Size | Weight | Tracking |
|---|---|---|---|---|
| Display / wordmark | Archivo | 34–56px | 800 | -0.03em |
| Section heading | Archivo | 22px | 600 | -0.02em |
| Body | Archivo | 15–17px | 400 | 0, `line-height: 1.55` |
| Caption | Archivo | 13px | 400 | 0, `line-height: 1.55` |
| Overline / eyebrow | IBM Plex Mono | 11px | 400 | 0.18em, uppercase, `--text-faint` |
| Control label | IBM Plex Mono | 10px | 400 | 0.18em, uppercase, `--text-faint` |
| Button / data | IBM Plex Mono | 12–14px | 600 | 0.1em for uppercase button text |

---

## 4. Control panel UI

### App header

`padding: 16px 24px`, `display: flex; align-items: center; gap: 18px`,
`border-bottom: 1px solid #1E2124`, `flex-wrap: wrap`.

Left to right:

1. 30px mark + `webATEM` wordmark at 17px/800 (`web` at 400, `--text-muted`), `gap: 12px`
2. Vertical rule — `1px × 24px`, `--border`
3. Connection status, IBM Plex Mono 12px: a `7px` dot (`--pvw` connected,
   `--text-faint` offline), then the device name in `--text`, then the IP in `--text-faint`
4. Spacer
5. Version string, IBM Plex Mono 12px, `--text-faint`

### Bus rows

Each bus is an overline label (`PROGRAM` / `PREVIEW`, mono 10px, 0.18em, `--text-faint`)
above a `display: flex; gap: 10px; flex-wrap: wrap` row of source buttons.

Source button:

- `flex: 1`, `min-width: 72px`, `padding: 18px 12px`, `border-radius: 8px`
- Text centered, IBM Plex Mono 14px/600
- **Inactive:** `background: #1E2124`, `color: #9A9E9F`, `border: 1px solid #34383C`
- **Live (program row):** `background: var(--pgm)`, `color: #FFFFFF`, no border
- **Cued (preview row):** `background: var(--pvw)`, `color: #0E0F10`, no border

Hit targets stay at or above 44px tall. The 18px vertical padding at 14px type gives
roughly 54px — do not reduce it on touch layouts.

### Transition controls

`display: flex; gap: 10px`, `margin-top: 18px`.

- **CUT** — `background: var(--pgm)`, `color: #FFFFFF`
- **AUTO** — `background: #1E2124`, `color: var(--text)`, `border: 1px solid #34383C`

Both: `padding: 14px 28px`, `border-radius: 8px`, IBM Plex Mono 13px/600, `0.1em` tracking,
uppercase.

### Geometry

- Border radius: `6px` inputs, `8px` buttons, `12px` swatches, `14px` cards
- Card padding: `32px` desktop
- Section gap: `88px` on the brand board; `28px` is right inside the app

---

## 5. Icons and favicon

Render the mark at 112, 56, 32, and 16. Sizes 30 and above keep the T-bar; 16 uses the
two-bar fallback. Below 16, use a solid `--pgm` square.

---

## 6. Naming and legal

The project is independent. Carry this line in the README, the docs footer, and the app's
about panel:

> Not affiliated with or endorsed by the switcher manufacturer.

Do not use the hardware vendor's logos, product photography, or brand colors. The red and
green here are functional broadcast convention, sampled to their own values.

---

## Files

- `WebATEM Brand.dc.html` — the full brand board: logo, mark study, color, type,
  in-app header mock, README banner. Open it in a browser.

## Not included

The brand board contains a terminal start-up block that does not reflect the actual app.
Ignore section 06; nothing in this spec depends on it.
