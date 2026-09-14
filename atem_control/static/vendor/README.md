# Vendored frontend assets

Everything the frontend loads is served from here — no CDN, so the app
works on an air-gapped studio network. License notices for all of these
are reproduced in the repository root
[THIRD_PARTY_LICENSES.md](../../../THIRD_PARTY_LICENSES.md).

| Asset | Version | Source | License |
|---|---|---|---|
| `alpine.min.js` | 3.14.3 | <https://github.com/alpinejs/alpine> | MIT |
| `alpinejs-collapse-3.14.3.min.js` | 3.14.3 | <https://github.com/alpinejs/alpine> (`@alpinejs/collapse`) | MIT |
| `../fonts/poppins/` (TTF, 5 weights) | — | <https://github.com/itfoundry/Poppins> | OFL 1.1 (`OFL.txt` alongside) |
| `bootstrap-icons/` (CSS + woff/woff2 fonts) | 1.11.3 | <https://github.com/twbs/icons> | MIT |
| `webatem.css` | built | Tailwind CSS 3.4.16 + daisyUI 4.12.24 | MIT |

Not in this directory but first-party: `../css/theme.css` (one dark theme in
DaisyUI 4 variable syntax; loads after `webatem.css`) and `../brand/brand.css`
(the palette that shows; loads last).

`webatem.css` is **not committed** — the Docker build's CSS stage
compiles it from `styles/app.css` + `tailwind.config.js` (versions pinned
in `package.json`). For a bare-metal checkout, build it with:

```sh
npm install
npm run build:css      # or watch:css during development
```

When updating a vendored file, update its version here and in
`THIRD_PARTY_LICENSES.md` in the same commit.
