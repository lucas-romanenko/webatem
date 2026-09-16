# CLAUDE.md — WebATEM

Browser control for Blackmagic ATEM switchers: a Django + Channels app that
finds the ATEMs on the LAN and gives the operator the whole control surface
(program/preview, T-bar, transitions, keyers, Fairlight, media pool, macros,
HyperDeck transport, state save/load). It is also the base of a private AV
platform's ATEM control page: that platform installs the package and supplies
access, audit, names and uploads through `atem_control/hooks.py` (see
"Ownership" and "The seam"). Product name **WebATEM**, wordmark `webATEM`. Independent project: not
affiliated with or endorsed by the switcher manufacturer — that line lives in
the **README** (both places there), and nowhere in the UI: the connect page's
copy and the launcher window's were removed 2026-09-14 (Lucas), the launcher
window signing its author instead. Keep the README's.

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
the `lucas-romanenko/bmdwire` monorepo via PyPI. Pins are bumped BY HAND
(`pyproject.toml`), on purpose: this repo has no bots in it — no
Dependabot config, no auto-merge — because a bot commit or pull request
puts a machine in the contributor list of what is a portfolio piece
(Lucas, 2026-09-14). The libraries are his own, so he knows when they
move.

## Layout

```
webatem/            the project package: settings, asgi, urls, websocket origin guard,
                    context.py (app_title + app_version), server.py (listen address:
                    HOST/PORT env > server.json > defaults; the `runtime` registry),
                    views.py (/server/settings/, /server/quit/, /launcher/),
                    launcher.py (the desktop launcher — see below), __main__.py,
                    templates/base.html, launcher.html, brand/_mark.svg
atem_control/       the Django app — the whole ATEM control surface, all of it
                    ours (see "Ownership"): control/ (consumer, commands, views…),
                    media_pool/, profile/, hyperdeck/, hooks.py (the seam, below),
                    activity.py + sightings.py (facades over the hooks),
                    discovery.py (mDNS _switcher_ctrl._udp + opt-in sweep),
                    js/ (ES modules → build/js → static/js/atem_control.js),
                    static/css/{atem_control,atem_connect,theme}.css,
                    static/brand/ (brand.css — loads last, wins — fonts/, icons, tray/),
                    templates/ (control.html + control/*.html, connect.html; a host
                    overrides base.html / control/_header.html by path)
tools/brand_assets.py          generates every brand asset from the handoff's ratios
docs/brand/handoff/            the identity system (README.md = the spec, the .dc.html board)
docs/brand/                    masters + exports + banner.png (also the GitHub social preview)
docs/DESIGN_BRIEF.md           the brief that preceded the handoff — superseded by it
build/desktop/                 icon.{svg,png,icns,ico}, icon-512.png, webatem.spec
tests/unit/                    the suite (test_server_settings.py = launcher/supervisor,
                               test_brand.py = assets and pages, test_hooks.py = the seam;
                               the rest is the app's own)
```

## Ownership — everything here is WebATEM's (since 2026-09-13)

WebATEM is the base. The private platform it grew out of installs the
`webatem` package and supplies its integration through
`atem_control/hooks.py` (next section) plus same-path template overrides;
nothing is copied in either direction any more (the sync tool and its tiers
are gone — until 2026-09-13 this app was resynced from that platform's
checkout). Two rules follow. Nothing specific to that platform's company —
names, studios, clients, addresses — enters this repo. And nothing generic
stays only over there: a behaviour both need lands here, with a working
default, and reaches the platform with the next release. WebATEM-wide styling goes in
`atem_control/static/brand/brand.css`, which `base.html` loads LAST and which
therefore wins. Discovery is WebATEM-only by decision — a platform with an
equipment database turns it off through the hooks.

## The seam — `atem_control/hooks.py` (since 0.4.0)

`atem_control` is meant to be HOSTED by a larger platform as well as run
standalone, so nothing in the app reaches past one class for anything the
host might own: `Hooks` (access for pages and the socket, activity and
connection records, name-for-IP, the switcher list for suggestions, HyperDeck
names, the switcher-name probe and rename record, sightings, the HyperDeck binding diff, the dropped-still check and upload,
template context, discovery on/off, an idle disconnect the host can ask
for — NEVER by default: a panel does not log itself out). `settings.WEBATEM_HOOKS` names the
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

## Surface rules paid for on real hardware

- **`atem_control` stays the module name. Do not propose renaming it** (Lucas,
  2026-09-15). It is a generic top-level import claimed in every project that
  installs this package, and one thing answers to four names here: the package
  is `webatem`, the module `atem_control`, the Django app label `webatem_atem`,
  the setting `WEBATEM_HOOKS`. That was weighed and kept. The collision is
  hypothetical, no consumer has hit it, and the churn is thirty imports here
  plus thirty in the host for a name that works. An outside review raised it as
  a now-or-never; the answer was never. If it ever does collide, the fix is the
  same size then as now, plus a major version.

- **The media pool takes any image, at any size.** No aspect ratio rule, no
  minimum, no byte ceiling. `uploader._prepare_frame` reads the switcher's live
  video mode and fits whatever arrives to that frame, which is what ATEM
  Software Control does and what this app is for. The strict 1920x1080 and 16:9
  default that lived here until 2026-09-15 was a host's policy left behind by
  the extraction, and it refused pictures the next step handled. A 64 MB upload
  cap lasted one commit: operators drop source images past 100 MB, and this app
  already hands anyone who can reach it the power to cut program, so a disk is
  not the interesting thing to protect. A host that wants a rule overrides
  `validate_still`; AV Server does, sized from the switcher's recorded mode.

- **Every page that POSTs sets the CSRF cookie** (`ensure_csrf_cookie` on
  `atem_connect` / `atem_control`, and on the launcher's two). Nothing else
  does: 0.5.0 removed the connect page's settings dialog and with it the last
  `{% csrf_token %}`, so the switcher rename, the media-pool drop and the
  profile POSTs all became 403s that the UI reported as its own failure
  ("Failed to set the name" while the switcher was fine). Pinned by tests in
  both suites.
- **No DaisyUI colour fill on a button.** `.btn-outline.btn-<colour>:hover`
  fills with that role's colour and flips the label to its content colour —
  near-black on this palette, unreadable (the Connect page's chips). brand.css
  carries same-specificity rules keeping outline buttons raised grey with a
  `--text` label; the brand has no accent fill.
- **An address is machine data**: IP fields and their placeholders are
  `.brand-data` (IBM Plex Mono, upright). The placeholder is an example
  address, never a sentence, and never italic.
- **The Settings drawer speaks the control surface's language**: mono overline
  section rows like PROGRAM / T-BAR / KEYS, `--surface` panel, `--border`
  hairlines (`.settings-panel` rules in brand.css). Address those rows by
  their shape (`button.w-full.text-left`) — the panel's last child is the
  overlay scrollbar, so a `:last-child` path matches nothing.
- **This package never uses a host's vocabulary.** The Switcher Name section
  offers `use “<name>”` from `hooks.name_for_ip`, not "use equipment name" —
  there is no inventory here.

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
  from its own machine, seen live). Startup: **under the window, nothing
  listens until the user has chosen an interface — at every launch UNLESS
  the user asked for it to come up on its own** (`_listen_host(..., resume)`:
  Run at login is on, this IS the login start `--autostart`, or Start
  minimized is set — then the saved interface is used, because a machine
  that boots into a working server is the whole point of those switches,
  Lucas 2026-09-14). Otherwise
  (`_listen_host` → `_Supervisor(host=None)`, `decided` event,
  `startup_note`); the window and the tray say so and the choice starts it.
  The saved interface is never applied on its own: 0.5.1 applied it when
  present and a reinstall came up on a months-old VPN choice ("WTF" —
  Lucas; "by default the server should not be running"). Never a silent
  switch to all interfaces either. Without a window nobody could choose, so
  the tray-only / foreground modes start on the saved or default address
  as before. A taken port (8000 is
  Companion's; ours is **8880**) → the next free one with a note.
  `timeout_graceful_shutdown=5`.
- **Tray and window first, Django after**: `main()` binds the window port,
  starts the window process (a "starting…" page that polls the URL), runs
  the tray, and boots Django (migrate + collectstatic into the data dir) on
  a thread. **The window process lives as long as the launcher** (0.5.1):
  started once, hidden if start-minimized / `--autostart`; Show and Hide are
  `show` / `hide` lines on its stdin, its own Hide button answers `hidden`
  on stdout, Quit sends `quit`. Spawning it per Show cost seconds.
- **The window's pages set the CSRF cookie** (`ensure_csrf_cookie` on
  `/launcher/` and the settings GET). 0.5.0 lost it with the connect page's
  dialog and every POST from the window was a 403 shown as "network error";
  pinned by a test with `enforce_csrf_checks`. The page now reports the HTTP
  status when a POST fails, never a generic network error.
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
- **The window is Companion's kind of panel** (0.5.0): `frameless=True`
  — no title bar, no minimise / close buttons on macOS, a borderless form on
  Windows — dragged by its body (`easy_drag`), shown and hidden only from
  the menu bar / tray and its own Hide button. The connect page has no
  settings gear any more: the address, port and start-at-login live in this
  window (the `/server/settings/` view stays for it).
- macOS menu-bar icon: `_mac_menu_bar_icon` swaps in the 44 px COLOUR mark
  (`tray/mac-44.png`, size 22, NOT a template image — the logo in colour,
  Lucas 2026-09-13) after pystray's own `_assert_image`; Windows picks
  `win-16`/`win-32` by `SM_CXSMICON`; Linux `linux-24`. The Windows build
  was run on real hardware and behaved (Lucas, 2026-09-14) — tray icon,
  borderless window, no taskbar button; macOS and Linux likewise. No
  platform in this launcher is unverified now.
- Unsigned build: macOS says "could not verify" → Privacy & Security → Open
  Anyway. Notarization needs an Apple Developer ID (declined so far).
- **Uninstalling removes the user's data; updating keeps it.** The installer's
  `[UninstallDelete]` takes the whole `{localappdata}\WebATEM` folder
  (settings, connection history, key, log) and a `[Registry]` entry drops the
  app's own HKCU Run value, which nothing else would clear — Windows would
  otherwise try to launch a missing program at every login. An UPDATE never
  runs the uninstaller (Inno installs over the existing copy), so it keeps
  everything. Lucas's rule, 2026-09-14: "if a user wants to uninstall that
  does mean everything should go away; if we update the app, the data should
  stay". CI plants a data folder and proves the uninstall removes it.
- **Windows ships an Inno Setup installer** (`build/desktop/webatem.iss`,
  built in the Package step): per-user into `{autopf}` with
  `PrivilegesRequired=lowest` (no admin), Start menu entry, an uninstaller in
  Settings > Installed apps, fixed `AppId` so upgrades replace. It went
  onefile exe (blocked as a virus) → zip (Lucas: "not a very clean option…
  I want this to be like a professional app") → installer. Keep the
  architecture identifier `x64`, not `x64compatible`: the newer spelling
  errors on Inno 6.2.
- **Every desktop build runs what it froze** before packaging (the Smoke test
  step: tray and browser off, a throwaway data dir, must serve `/atem/`).
  0.6.4/0.6.5 shipped a Windows build nobody had ever started.
- The frozen build is PyInstaller **onedir on macOS and Windows** (a normal
  app bundle / a folder shipped as `webatem-windows-x64.zip`); **Linux stays
  onefile**. Two reasons, both paid for: a onefile executable unpacks ~35 MB
  into a temp dir at EVERY start and the launcher window is this program
  started again (0.5.1, macOS), and on Windows an unsigned self-extracting
  exe is what heuristic scanners block — Chrome refused 0.6.3's download as
  a virus (0.6.4). The Windows exe also carries a VERSIONINFO resource
  (`_win_version_file` in the spec). **None of this is a substitute for code
  signing**, which is the only real fix and costs money (Azure Trusted
  Signing, or an OV certificate); Lucas has declined paid signing so far, on
  both platforms.

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
- README screenshots are FRAMED: `tools/screenshot_frame.py` wraps a capture
  in a browser window (traffic lights, address bar) in the brand's colours —
  a bare page screenshot reads as a mock-up. Capture at deviceScaleFactor 2,
  then frame with the address it should appear to be served from. **Never
  publish a picture of the bench switcher's own labels**: the capture script
  rewrites `state.sources[].short/.long`, the macro names and the audio
  strips' text to CAM1…CAM10 / Open, Lower third, … and the header to
  "Studio A / 192.168.1.240", and runs the container on a BRIDGE network so
  mDNS finds none of the real switchers.
- Screenshots (Lucas reviews through a published artifact): run the image on
  the host network on a spare port (`-e PORT=8895 -e DATA_DIR=/tmp/wd`), then
  Playwright from the host (`~/.local/node20/bin/node`, playwright out of the
  npx cache, `channel: 'chrome'`, deviceScaleFactor 2); the control page
  needs a switcher on the network — a bench unit, never a live room;
  wait for `Alpine.store('atem').stateReady && state.sources`.
- CSS is compiled (`npm run build:css` → `atem_control/static/vendor/webatem.css`,
  not committed; CI and the image build it). A new utility class needs the
  build. `[hidden]` is made to win at the end of `brand.css` — keep it there, last.
- Commit messages say why; the standing attribution trailer applies.
- Never touch a downstream checkout from here; what a host needs arrives as
  a hook method or a template slot with a working default (see Ownership).
