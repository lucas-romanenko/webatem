# CLAUDE.md — WebATEM

Browser control for Blackmagic ATEM switchers: a Django + Channels app that
finds the ATEMs on the LAN and gives the operator the whole control surface
(program/preview, T-bar, transitions, keyers, Fairlight, media pool, macros,
HyperDeck transport, state save/load). It is the public extraction of a
private AV platform's ATEM control page ("upstream" below) — same code,
resynced by a tool, with the equipment/auth/user layers swapped for a thin
one. Product name **WebATEM**, wordmark `webATEM`. Independent project: not
affiliated with or endorsed by the switcher manufacturer — that line stays in
the README, the connect page and the launcher window.

## Three doors, one tag

| Door | What | Where |
|---|---|---|
| Desktop launcher | `webatem` command frozen by PyInstaller: macOS `.dmg` (arm64 + Intel), Windows `.exe`, Linux binary | `.github/workflows/desktop.yml`, `build/desktop/webatem.spec` |
| Docker image | `ghcr.io/lucas-romanenko/webatem:<version>` (amd64 + arm64), plain uvicorn via `docker-entrypoint.sh` | `.github/workflows/image.yml`, `Dockerfile`, `compose.yml` |
| PyPI | `pipx install webatem` (trusted publishing, environment `pypi`) | `.github/workflows/publish.yml` |

A `v<version>` tag on `main` (must equal `project.version` in `pyproject.toml`)
runs all three. **Every workflow keys on the tag push** — publish.yml used to
key on the GitHub release, but the release is created by the desktop build
with the workflow token and GitHub never starts other workflows from that,
so PyPI silently got skipped (v0.2.3). Release procedure: branch → PR → CI
green + `gh workflow run desktop.yml --ref <branch>` green on all four →
merge → `git tag vX.Y.Z main && git push origin vX.Y.Z` → wait for the three
runs → `gh release edit vX.Y.Z --notes …` (the bot-created release has no
notes) → verify the four assets, `pypi.org/pypi/webatem/<v>/json` and
`docker manifest inspect ghcr.io/lucas-romanenko/webatem:<v>`. A desktop job
that fails in `actions/upload-artifact` (GitHub timeout, seen once) is fixed
by `gh run rerun <id> --failed`; each job attaches its own asset. Never
re-tag; a broken release gets the next number. `main` has no ruleset.

Dependencies: `pyproject.toml` carries RANGES for the framework (WebATEM is
also a library inside a project with its own pins) and exact pins for the
two device libraries; `requirements.txt` is the exact-pin lock WebATEM's own
builds apply as constraints (`pip install -c requirements.txt ".[desktop]"`). The
tray and window libraries are the `desktop` extra — a server never installs
them; `pipx install "webatem[desktop]"` is the launcher, plain `webatem` the
headless server. The device libraries `atemwire` and `hyperdeckwire` come from
the `lucas-romanenko/bmdwire` monorepo via PyPI; Dependabot opens the bump
PRs (`.github/dependabot.yml`, cooldown excluded for those two) and
`dependabot-automerge.yml` merges them once CI is green.

## Layout

```
webatem/            the project package: settings, asgi, urls, websocket origin guard,
                    context.py (app_title + app_version), server.py (listen address:
                    HOST/PORT env > server.json > defaults; the `runtime` registry),
                    views.py (/server/settings/, /server/quit/, /launcher/),
                    launcher.py (the desktop launcher — see below), __main__.py,
                    templates/base.html, launcher.html, brand/_mark.svg
atem_control/       the Django app (synced from upstream, see "The sync"):
                    control/ (consumer, commands, views…), media_pool/, profile/,
                    hyperdeck/, discovery.py (WebATEM-own: mDNS _switcher_ctrl._udp +
                    opt-in sweep), sightings.py (no-op stand-in for upstream's
                    equipment sightings), js/ (ES modules → build/js → static/js/atem_control.js),
                    static/css/{atem_control,atem_connect}.css (verbatim upstream),
                    static/css/theme.css (GENERATED from upstream's theme sheet),
                    static/brand/ (WebATEM-own: brand.css, fonts/, icons, tray/),
                    templates/ (control.html + control/*.html synced; connect.html and
                    control/_header.html are hand-merged MANUAL files)
tools/sync_from_av_server.py   the resync (run after upstream control-page work)
tools/brand_assets.py          generates every brand asset from the handoff's ratios
docs/brand/handoff/            the identity system (README.md = the spec, the .dc.html board)
docs/brand/                    masters + exports + banner.png (also the GitHub social preview)
docs/DESIGN_BRIEF.md           the brief that preceded the handoff — superseded by it
build/desktop/                 icon.{svg,png,icns,ico}, icon-512.png, webatem.spec
tests/unit/                    the suite (test_server_settings.py = launcher/supervisor,
                               test_brand.py = assets and pages; the rest synced from upstream)
```

## The sync — what you may edit

`tools/sync_from_av_server.py` overwrites, from the upstream checkout: the
`atem_control/js` modules, `atem_control/static/css` (except files not
upstream), `build/js`, the REWRITE files (dispatch table, watcher,
broadcast, dialog, the hand-edited JS files, activity.py, device_api.py),
the REWRITE pairs (uploader/tally/netutil/pp-select/custom-scrollbar), the
synced tests, the templates (minus `TEMPLATE_EXCLUDE`), and it REGENERATES
`theme.css` (`_theme_css`: dark theme keyed `dark`, light dropped, brand
words out). It reports drift on the MANUAL list (consumer.py, views.py,
hyperdeck/views.py, profile/*.py, urls.py, routing.py, models.py,
`_header.html`, `connect.html`) for hand-merging — equipment / auth /
`user=` hunks are never ported. So: **edit `theme.css` never; put every
WebATEM-only style in `atem_control/static/brand/brand.css`**, which
`base.html` loads LAST and which therefore wins; WebATEM-own files live in
`webatem/`, `atem_control/static/brand/`, `discovery.py`, `sightings.py`,
`atem_discovery.js`, `atem_server_settings.js`. Discovery is WebATEM-only by
decision — upstream has an equipment database and will not get mDNS.

## The seam — `atem_control/hooks.py` (since 0.4.0)

`atem_control` is meant to be HOSTED by a larger platform as well as run
standalone, so nothing in the app reaches past one class for anything the
host might own: `Hooks` (access for pages and the socket, activity and
connection records, name-for-IP, the switcher list for suggestions, HyperDeck
names, sightings, the HyperDeck binding diff, the dropped-still check and upload,
template context, discovery on/off). `settings.WEBATEM_HOOKS` names the
host's subclass; unset = the defaults, which ARE standalone WebATEM. The
`activity` and `sightings` modules are facades over it — keep call sites on
them. The app's label is `webatem_atem` (tables `webatem_atem_*`), on
purpose: the host may own an app called `atem_control` with its own history,
and Django keys migrations and tables on the label. Migration 0002 copies a
0.3-era `atem_control_atemcontrollog` (no user column) into the new table;
a table WITH a user column is somebody else's and is left alone. Templates:
the host overrides `base.html` / `control/_header.html` by path and fills
`control_extra` in control.html. The hooks are pinned by
`tests/unit/test_hooks.py`; a new dependency on the surroundings goes
through a new hook method with a working default, never a direct import.

## The launcher (`webatem/launcher.py`) — Companion, literally

Lucas's standard is Bitfocus Companion: an icon in the menu bar / tray, a
small launcher window, nothing in the Dock or taskbar, and it opens exactly
what the user chose. Decisions that were paid for on his Mac:

- **Tray on the main thread** (`pystray` `icon.run()`), three items:
  Show/Hide window, Launch GUI, Quit. macOS needs the main thread.
- **The window is a separate process**: `webatem --window <url>` runs
  pywebview; Show spawns it (`_WindowChild`), Hide ends it. Two event loops
  in one process lost the menu-bar icon (0.2.2). **The tray process never
  imports `webview`** — its Cocoa backend sets the activation policy to
  Regular (a Dock icon) at import; `_window_support` probes with
  `find_spec` (pinned by a test). The window process pre-imports the
  backend and switches back to Accessory (no Dock icon); on Windows the form
  gets `ShowInTaskbar = False` in `before_show`.
- **Two uvicorns on one app** (`_Supervisor`): the public one on the chosen
  interface:port (restartable from the window / settings page) and the
  window's OWN loopback server on a free port, never restarted — the window
  used to be served by the server it restarts and died with it (0.2.3).
  Sockets are pre-bound (`_bind`) and handed to `server.run(sockets=…)`, so a
  taken port or a gone interface is an OSError here; a specific interface
  ALWAYS gets a 127.0.0.1 socket beside it (a VPN address is not reachable
  from its own machine, seen live). Startup: gone interface → all
  interfaces with a note; taken port (8000 is Companion's; ours is **8880**)
  → the next free one with a note. `timeout_graceful_shutdown=5`.
- **Tray and window first, Django after**: `main()` binds the window port,
  spawns the window (a "starting…" page that polls the URL), runs the tray,
  and boots Django (migrate + collectstatic into the data dir) on a thread.
- **Launch GUI opens the address the window shows — no probe, no
  fallback.** A loopback fallback was built and removed: "confusing;
  Companion opens what the user chose and if it doesn't work it doesn't
  work." Say what happened (a startup note), never substitute.
- The Interface select starts on the placeholder "Change network
  interface…" until the user has chosen (`source == 'default'`).
- Quit from the window: `POST /server/quit/` → `runtime.quit()` → a Timer
  (the response goes out first) → `_on_ui_thread(icon.stop)`.
- Start at login: LaunchAgent plist / HKCU Run / autostart .desktop, with
  `--autostart` (no window, no browser). `start_minimized` in server.json.
- macOS menu-bar icon: `_mac_menu_bar_icon` swaps in the 44 px TEMPLATE
  image (black + alpha, `setTemplate_(True)`, size 22) after pystray's own
  `_assert_image`; Windows picks `win-16`/`win-32` by `SM_CXSMICON`; Linux
  `linux-24`. Windows tray/taskbar behaviour is untested on real hardware.
- Unsigned build: macOS says "could not verify" → Privacy & Security → Open
  Anyway. Notarization needs an Apple Developer ID (declined so far).
- The frozen build is PyInstaller onefile, so each Show re-extracts the
  bundle (a few seconds). A persistent hidden window child is the follow-up
  if that ever matters.

Headless smoke of the whole launcher (no display): a stand-in `pystray`
module on `PYTHONPATH` + `DISPLAY=:0` + `--network host` in the image,
`python -m webatem`, then drive `/server/settings/` and `/server/quit/` on
the window server's port (it prints `WebATEM window server on
127.0.0.1:<port>`). The suite's supervisor tests run real uvicorns.

## The brand (`docs/brand/handoff/README.md` is the spec — high fidelity)

The mark is a control panel in miniature: PROGRAM red `#D62718` over
PREVIEW green `#2FD07A`, the T-bar slot beside them, on a `#0A0B0C` tile;
below 30 px it is the two bars alone. Wordmark `webATEM` — Archivo, `web`
400 muted, `ATEM` 800, `-0.03em`. Archivo is the interface face; **IBM Plex
Mono is for anything a machine reported** (addresses, ports, source
buttons, status, version). Tokens: ink `#0E0F10`, surface `#141618`, raised
`#1E2124`, border `#26292C` / strong `#34383C`, text `#EDEDEA`, muted
`#9A9E9F`, faint `#7E8386`, link `#FF7A6B` (text only, never a fill). Rules
that hold: the state colours ARE the brand — `--pgm` is never a fill for
anything not live, `--pvw` never for anything not cued; there is no neutral
accent, so primary actions are raised grey with the strong border; radii
8 px buttons / 6 px inputs / 14 px cards; keys/BKGD keep amber as a third
state. Both fonts are self-hosted (OFL, licences beside the woff2) — the app
runs on air-gapped LANs, never load Google Fonts at runtime.

Regenerate assets with `tools/brand_assets.py` in a throwaway container
(`python:3.12-slim` + pillow fonttools brotli uharfbuzz icnsutil; HarfBuzz
needs the woff2 decompressed to sfnt first; Pillow's ICO writer wants the
largest image first). Do not hand-edit an exported icon. The pitch is
"server on Mac · Windows · Linux" and "control from any device on the
network" — never "no install on the client" (Lucas: the user installs
WebATEM). The README banner is also the GitHub social preview (uploaded by
hand in repository settings).

## Working here

- Tests, in the image: `docker compose build && docker run --rm --entrypoint
  pytest ghcr.io/lucas-romanenko/webatem:latest tests/ -q` (mount
  `-v ./tests:/app/tests:ro` to run edited tests without a rebuild). The
  count is whatever the tail line says; don't write it down.
- Screenshots (Lucas reviews through a published artifact): run the image on
  the host network on a spare port (`-e PORT=8895 -e DATA_DIR=/tmp/wd`), then
  Playwright from the host (`~/.local/node20/bin/node`, playwright out of the
  npx cache, `channel: 'chrome'`, deviceScaleFactor 2); the control page
  needs a switcher on the network — a bench unit, never a live room;
  wait for `Alpine.store('atem').stateReady && state.sources`.
- CSS is compiled (`npm run build:css` → `atem_control/static/vendor/webatem.css`,
  not committed; CI and the image build it). A new utility class needs the
  build. `[hidden]` is made to win at the end of `theme.css` — keep it.
- Commit messages say why; the standing attribution trailer applies.
- Never touch the upstream checkout from here; port upstream work with the
  sync tool and hand-merge the MANUAL drift it reports.
