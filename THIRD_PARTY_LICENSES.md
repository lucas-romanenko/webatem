# Third-party licenses

WebATEM's own code is MIT-licensed (see [LICENSE](LICENSE)). The ATEM
protocol library it installs, [atemwire](https://github.com/lucas-romanenko/bmdwire/tree/main/atemwire)
(a fork of Martijn Braam's pyatem), is LGPL-3.0-only and ships its own
notices; [hyperdeckwire](https://github.com/lucas-romanenko/bmdwire/tree/main/hyperdeckwire) is
MIT. Neither is copied into this repository.

This file reproduces the license notices for the third-party frontend
assets bundled in this repository and in the built image. See
[atem_control/static/vendor/README.md](atem_control/static/vendor/README.md)
for versions, sources, and update instructions.

## MIT-licensed components

The following components are licensed under the MIT License (reproduced
once below), each under its own copyright:

- **Alpine.js 3.14.3** — Copyright © 2019–2021 Caleb Porzio and
  contributors — <https://github.com/alpinejs/alpine>
  (bundled as `atem_control/static/vendor/alpine.min.js`; the minified
  build strips its banner, so the notice is reproduced here)
- **Bootstrap Icons 1.11.3** — Copyright © 2019–2024 The Bootstrap
  Authors — <https://github.com/twbs/icons>
  (bundled CSS and font files under
  `atem_control/static/vendor/bootstrap-icons/`)
- **Tailwind CSS 3.4.16** — Copyright © Tailwind Labs, Inc. —
  <https://github.com/tailwindlabs/tailwindcss>
  (not committed; compiled into `vendor/webatem.css` at build time)
- **daisyUI 4.12.24** — Copyright © 2020 Pouya Saadeghi —
  <https://github.com/saadeghi/daisyui>
  (not committed; compiled into `vendor/webatem.css` at build time)

### MIT License

```
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```

## Python dependencies

Python packages (Django, channels, uvicorn, Pillow, whitenoise, …) are
not vendored in this repository — they are installed from PyPI at image
build time per `requirements.txt`, and each carries its own license
metadata inside the installed distribution.

## @alpinejs/collapse 3.14.3

MIT — the same license and copyright as Alpine.js above (Caleb Porzio and
contributors). Vendored as `atem_control/static/vendor/alpinejs-collapse-3.14.3.min.js`
for the settings accordion's height animation.

## Archivo and IBM Plex Mono (fonts)

The brand's two families (docs/brand/handoff): **Archivo** — Copyright 2020
The Archivo Project Authors (https://github.com/Omnibus-Type/Archivo) — and
**IBM Plex Mono** — Copyright © 2017 IBM Corp. with Reserved Font Name
"Plex" (https://github.com/IBM/plex). Both are licensed under the SIL Open
Font License, Version 1.1; the full texts are
`atem_control/static/brand/fonts/OFL-Archivo.txt` and
`atem_control/static/brand/fonts/OFL-IBMPlexMono.txt`, beside the woff2
files (latin and latin-ext subsets, as served by Google Fonts).

## Poppins (font)

Copyright 2020 The Poppins Project Authors (https://github.com/itfoundry/Poppins).
Licensed under the SIL Open Font License, Version 1.1. The full license text
is `atem_control/static/fonts/poppins/OFL.txt`, beside the TTFs. The font is
self-hosted so the UI needs no internet access.
