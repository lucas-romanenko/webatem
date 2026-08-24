# WebATEM

**Control your Blackmagic ATEM switcher from any browser.** WebATEM finds the
ATEMs on your network and gives you a full control surface — switching,
keyers, audio, media pool, macros — at a URL, on any device.

<p align="center">
  <a href="https://github.com/lucas-romanenko/webatem/releases/latest/download/webatem-windows-x64.exe"><img src="https://img.shields.io/badge/Download-Windows-0078D6?style=for-the-badge&logo=windows&logoColor=white" alt="Download for Windows"></a>
  &nbsp;
  <a href="https://github.com/lucas-romanenko/webatem/releases/latest/download/webatem-macos-arm64.dmg"><img src="https://img.shields.io/badge/Download-macOS-000000?style=for-the-badge&logo=apple&logoColor=white" alt="Download for macOS"></a>
  &nbsp;
  <a href="https://github.com/lucas-romanenko/webatem/releases/latest/download/webatem-linux-x64"><img src="https://img.shields.io/badge/Download-Linux-E95420?style=for-the-badge&logo=linux&logoColor=white" alt="Download for Linux"></a>
</p>
<p align="center"><sub>Download it, open it, and your switchers appear. &nbsp;·&nbsp; Intel Mac? <a href="https://github.com/lucas-romanenko/webatem/releases/latest/download/webatem-macos-intel.dmg">Intel build</a> &nbsp;·&nbsp; The Linux build runs on desktop <b>and</b> headless servers.</sub></p>

Two ways to run it, both simple:

- **Just you, right now** → **download the app** for your Mac, Windows, or
  Linux machine, open it, and your switchers appear. Like ATEM Software
  Control, but in your browser.
- **Your whole team, always on** → **host it** with one Docker command on a
  box on your network; everyone opens a URL — no installs, no accounts.

Either way, it **auto-discovers the ATEMs on your network** (Bonjour/mDNS) and
lists them by name, exactly like ATEM Software Control.

> 📸 *Screenshot/GIF coming here — control surface, media pool, keyers.*

---

## Get started

### Option A — Download the app  *(easiest)*

1. Click your platform's **Download** button at the top (or the
   [Releases](https://github.com/lucas-romanenko/webatem/releases) page).
2. Open it. It starts a local server, opens your browser, and lists the ATEMs
   on your network. Pick one and you're controlling it.

That's the whole setup — nothing to install alongside it, no Python, no Docker.

**On Linux — desktop or headless server.** The same `webatem-linux-x64` binary
works both ways. Make it runnable and start it:

```bash
chmod +x webatem-linux-x64
./webatem-linux-x64
```

On a desktop it opens your browser; on a **headless server** (no display) it
skips that and just prints the address to open from another machine:

```
WebATEM is running.
  On this machine:      http://127.0.0.1:8000/atem/
  From another device:  http://192.168.1.50:8000/atem/
```

It listens on all interfaces, so browse to that `From another device` URL from
anywhere on the network. Leave it running under `nohup`, `tmux`, or a systemd
service to keep it up. (For a permanent multi-user install, Option B is
cleaner.)

<details>
<summary><b>First-launch security prompt</b> (the app isn't code-signed yet)</summary>

Because the downloads aren't signed with a paid developer certificate, the OS
warns you the first time. One-time, then it opens normally:

- **macOS:** double-click, let it get blocked, then **System Settings →
  Privacy & Security → “Open Anyway”**. (On older macOS: right-click → Open.)
- **Windows:** **More info → Run anyway** on the SmartScreen prompt.

Removing the warning entirely requires paid Apple/Microsoft signing
certificates — planned, not done yet.
</details>

### Option B — Host it  *(a whole team; always on)*

Run it on a **Linux box that's on the same network as your ATEMs** (this is
the ATEM-Software-Control-on-a-server model):

```bash
git clone https://github.com/lucas-romanenko/webatem.git
cd webatem
docker compose up -d --build
```

Open `http://<that-box>:8000` from any device on the network. It comes back
automatically on reboot. No `.env` file needed — every setting has a working
default (SQLite DB + a generated secret key live in the `atem-data` volume);
see [.env.example](.env.example) for knobs like `PORT`, `TIME_ZONE`,
`ALLOWED_HOSTS`.

> **Why Linux for hosting?** Discovery needs the app to see your LAN directly.
> That works natively on Linux (the container uses host networking). Docker
> Desktop on **Mac/Windows** runs containers in a VM that can’t see LAN
> discovery traffic — so on a Mac or PC, use the **downloadable app** (Option
> A), not Docker.

---

## Finding your switchers

Three ways, in order of magic:

1. **Automatic (Bonjour/mDNS).** On the same network as your ATEMs, they show
   up under **“On Your Network”** with their names, no typing. This is what
   ATEM Software Control does.
2. **Scan subnet.** For ATEMs that don’t advertise over Bonjour, type your
   subnet (e.g. `192.168.1`) and hit **Scan** — it finds them by IP.
3. **Manual IP.** Type an address and connect. Recent connections are
   remembered for one-click reconnect.

> Automatic discovery is **same-network only** — Bonjour/mDNS is link-local
> and doesn’t cross a router or VPN (this is true of ASC too). Reaching your
> ATEMs over a **VPN**? Auto-discovery won’t see them, but **Scan** and
> **manual IP** work fine over the tunnel.

---

## WebATEM vs. ATEM Software Control

ATEM Software Control (ASC) is Blackmagic’s own free control app, and it’s
excellent — WebATEM isn’t trying to replace all of it. The difference is
*access*: ASC is a desktop app tied to one machine; WebATEM is a control
surface any device on your network can open (and you can still run it locally
like ASC if you want).

|  | **WebATEM** | **ATEM Software Control** |
|---|---|---|
| **Runs on** | Any browser — Windows, macOS, **Linux**, ChromeOS, iPad, phone | Windows & macOS desktop only |
| **Client install** | None — just open a URL | Installed per machine |
| **Access from** | Any device on the network | The machine it’s installed on |
| **Multiple operators** | One shared switcher session, many operators | Each machine opens its own session |
| **Phones / tablets** | ✓ Responsive | ✗ |
| **How to run** | Download the app, **or** host one instance for everyone | Install on each machine |
| **Price** | Free, open source | Free (proprietary) |
| **Feature breadth** | Core: switching, keyers, Fairlight audio, media pool, macros, profiles, HyperDeck | Everything, incl. camera control (CCU), streaming & recording, SuperSource |
| **Support** | Community / self-hosted | Official Blackmagic |

**Reach for ASC** when you need the full feature set on one operator’s machine
— camera control, streaming/recording, SuperSource, recording macros.

**Reach for WebATEM** when you want the core control surface available to
anyone on the network, on any device or OS — a second operator on an iPad, a
Linux box in the rack room, a phone at the camera position.

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
  XML, compatible with ATEM Software Control’s “Save Switcher State”,
  including media pool images and macro bytecode.
- **HyperDeck transport** — clip browser and play/pause/stop/loop for decks
  bound to the ATEM (TCP/9993), from the same page.
- **Multi-operator by design** — one pooled connection per switcher shared by
  all operators; state fan-out over WebSockets with adaptive polling
  (30 ms during transitions, relaxed when idle).

## Running it for real

- **There is no login — by design.** Like the hardware panel, anyone who can
  reach the page can control your switchers. Keep it on the studio network. If
  you must expose it further, put your reverse proxy’s auth (basic auth, SSO)
  and TLS in front, and set `CSRF_TRUSTED_ORIGINS=https://your.host`.
- **Web pages can’t drive it cross-origin.** The control WebSocket only
  accepts browser connections from the app’s own pages (same-origin), so a
  malicious website open in an operator’s browser can’t reach the switchers.
  If your reverse proxy rewrites `Host`, list the browser-facing host in
  `WEBSOCKET_ALLOWED_ORIGINS`.
- It talks raw UDP to switchers on port 9910 (the ATEM protocol has no
  authentication — that’s the hardware, not this app), so it needs to be on a
  network that can reach them.
- **Single instance, by design.** The switcher connection pool, media-pool
  watcher, and channel layer are process-local; one instance handles many
  switchers and operators. Don’t run several against the same switchers.
- Stills upload/capture assume **1080p** switchers (all production use so far).
  Non-1080p ATEMs connect and control fine; media features are untested there.

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
  conversion.
- **`atem_control/`** — the Django app: the WebSocket consumer, a declarative
  command dispatch table, the media-pool watcher, LAN discovery, and the UI.
- **`launcher.py` + `build/desktop/`** — the desktop app: a PyInstaller build
  that runs the same web app locally and opens a browser, packaged per OS by
  `.github/workflows/desktop.yml`.
- **SQLite** for persistence (connection history) — no external database.

## Development

```bash
docker compose up -d --build                     # run the app
docker compose exec webatem pytest tests/ -q     # run the test suite
```

The suite (pyatem protocol, transport, DSL, macro codec, profile round-trip,
connection-pool lifecycle, upload policy) runs against a fake protocol layer —
no hardware needed. CI runs it on every push.

**Bare-metal / build the desktop app:** Python 3.14, `npm install && npm run
build:css` (the stylesheet — without it the app renders unstyled),
`pip install -r requirements.txt`, `python setup.py build_ext --inplace`
(the C extension), then `python launcher.py` (desktop) or
`python manage.py migrate && python manage.py runserver` (server). The per-OS
downloadable binaries are built by the **Desktop builds** GitHub Actions
workflow (`pyinstaller build/desktop/webatem.spec`).

## License

- Application code: [MIT](LICENSE).
- `pyatem/` is a fork of the [OpenAtem pyatem
  library](https://git.sr.ht/~martijnbraam/pyatem) and remains
  **LGPL-3.0-only** — see [pyatem/LICENSE](pyatem/LICENSE) and
  [pyatem/NOTICE.md](pyatem/NOTICE.md).
- Bundled frontend assets (Alpine.js, Bootstrap Icons, Tailwind/daisyUI) are
  MIT — notices in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).

Security posture and how to report vulnerabilities: [SECURITY.md](SECURITY.md).

Not affiliated with or endorsed by Blackmagic Design. ATEM and HyperDeck are
trademarks of Blackmagic Design Pty Ltd. Use against production hardware at
your own risk.
