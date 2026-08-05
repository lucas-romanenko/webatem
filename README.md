# WebATEM

**Browser-based control for Blackmagic ATEM switchers.** One Docker container,
any device with a browser — desktop, tablet, or phone, on any OS. Built for
multi-operator broadcast environments and battle-tested in daily live
production.

> 📸 *Screenshot/GIF coming here — control surface, media pool, keyers.*

## Why

ATEM Software Control is a desktop app, one machine at a time. This runs on
a server once and gives every operator on the network a full control surface
at a URL — no installs, no accounts, several operators sharing one switcher
session safely.

## Features

- **Full switcher control** — program/preview buses per M/E, cut/auto,
  transition styles (mix, dip, wipe, DVE, stinger) with per-style settings,
  fade to black, color generators, aux routing, source renaming that follows
  live switcher labels.
- **Upstream & downstream keyers** — USK 1–4 (luma, chroma, pattern, DVE with
  fly keyframes, masks), DSKs with tie/rate/clip/gain, on-air countdowns.
- **Fairlight audio** — per-strip faders, EQ, dynamics, master bus, and live
  audio meters streamed over WebSocket (tab-gated so idle pages cost nothing).
- **Media pool** — live thumbnails of every still slot, and drag-and-drop
  image upload straight onto a slot (validated and resized to 1080p
  server-side).
- **Macros** — browse and run switcher macros.
- **Save/restore switcher state** — full switcher profile export/import as
  XML, compatible with ATEM Software Control's "Save Switcher State",
  including media pool images and macro bytecode.
- **HyperDeck transport** — clip browser and play/pause/stop/loop for decks
  bound to the ATEM (TCP/9993), from the same page.
- **Multi-operator by design** — one pooled connection per switcher shared by
  all operators; state fan-out over WebSockets with adaptive polling
  (30 ms during transitions, relaxed when idle).
- **Auditability** — connection/disconnection log (drives the "Recent
  ATEMs" quick-connect), operator actions in the application log.

## Quick start (Docker)

```bash
git clone https://github.com/lucas-romanenko/webatem.git
cd webatem
docker compose up -d --build
```

Open `http://<server>:8000`, type your ATEM's IP, connect — that's it.

No `.env` file is required — every setting has a working default (SQLite
database and a generated secret key live in the `atem-data` volume). See
[.env.example](.env.example) for the knobs (`PORT`, `TIME_ZONE`,
`ALLOWED_HOSTS`, …).

## Running it for real

- **There is no login — by design.** Like the hardware panel, anyone who
  can reach the page can control your switchers. Keep it on the studio
  network. If you must expose it further, put your reverse proxy's auth
  (basic auth, SSO) and TLS in front, and set
  `CSRF_TRUSTED_ORIGINS=https://your.host`.
- It talks raw UDP to switchers on port 9910 (the ATEM protocol itself has
  no authentication — that's the hardware, not this app), so the container
  needs to be on a network that can reach them.
- **Single worker, by design.** The switcher connection pool, media-pool
  watcher, and channel layer are process-local. Don't scale this container
  horizontally; one instance handles many switchers and operators.
- Stills upload/capture currently assume **1080p** switchers (all production
  use so far). Non-1080p ATEMs will connect and control fine; media features
  are untested there.

## Architecture

```
Browser (Alpine.js + WebSockets)
   │  ws/atem/ · HTTP
   ▼
Django + Channels (single ASGI worker, uvicorn)
   │  pooled UDP session per switcher (pyatem)
   ▼
ATEM switchers (UDP 9910) · HyperDecks (TCP 9993 / FTP)
```

- **`pyatem/`** — a substantially modified fork of the OpenAtem protocol
  library: declarative wire-format DSL, hardened UDP transport (in-order
  delivery, retransmit serving, clean session close), ref-counted connection
  pooling, native interleaved bulk transfers, macro bytecode transfer, and
  ASC-compatible profile save/restore. A small C extension does YCbCr↔RGB
  conversion (compiled in the Docker build).
- **`atem_control/`** — the Django app: the WebSocket consumer, a
  declarative command dispatch table, the media-pool watcher, and the UI.
- **SQLite** for persistence (connection history) — no external
  database to run.

## Development

```bash
docker compose up -d --build          # run the app
docker compose exec webatem pytest tests/ -q   # run the test suite
```

The test suite (pyatem protocol, transport, DSL, macro codec, profile
round-trip, connection pool lifecycle, upload policy) runs
against a fake protocol layer — no hardware needed. CI runs it on every push.

Bare-metal instead of Docker: Python 3.12+, `pip install -r requirements.txt`,
compile the C extension (`gcc -shared -fPIC -O2 -I$(python3 -c 'import
sysconfig; print(sysconfig.get_paths()["include"])') pyatem/mediaconvertmodule.c
-o pyatem/mediaconvert$(python3 -c 'import sysconfig;
print(sysconfig.get_config_var("EXT_SUFFIX"))')`), then
`python manage.py migrate && python manage.py runserver`.

## License

- Application code: [MIT](LICENSE).
- `pyatem/` is a fork of the [OpenAtem pyatem
  library](https://git.sr.ht/~martijnbraam/pyatem) and remains
  **LGPL-3.0-only** — see [pyatem/LICENSE](pyatem/LICENSE) and
  [pyatem/NOTICE.md](pyatem/NOTICE.md).

Not affiliated with or endorsed by Blackmagic Design. ATEM and HyperDeck are
trademarks of Blackmagic Design Pty Ltd. Use against production hardware at
your own risk.
