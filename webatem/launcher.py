#!/usr/bin/env python3
"""WebATEM native launcher — run the web app locally, no Docker.

This is how a desktop user (Mac / Windows / Linux) runs WebATEM: the SAME
browser-based app, served by a local process that lives on your REAL
network — so ATEM mDNS discovery works. (A Docker container on Mac/Windows
sits behind a VM+NAT and can't see the LAN's multicast; a native process
can, exactly like ATEM Software Control.) Packaged into one self-contained
executable per OS via PyInstaller — no Python, no Docker required by the
user: download, double-click, browser opens, switchers appear.

Like Bitfocus Companion: a menu bar / system tray icon with three items
(Show/Hide window, Launch GUI, Quit) and a small window — Running, the
address, the interface and port, Start minimized, Run at login, Launch GUI
/ Hide / Quit. The window is the app's own /launcher/ page in a native
webview, run as a SEPARATE PROCESS: a native window and a tray icon each
want the main thread and the event loop, and sharing one loop between them
lost the tray on macOS (0.2.2). The tray process is the server; Show spawns
the window process, Hide ends it. The window's page comes from a second,
loopback-only server on its own port that never restarts, so it keeps
working while the public server moves (0.2.3 lost it that way). Where the window library is missing it
is the tray alone (its menu then opens the launcher page in the browser);
where no tray is possible either — a headless Linux server — it runs in the
foreground as a plain process (Ctrl+C to quit).

If the port is taken at startup (Companion lives on 8000, hence 8880 here)
the next free port is used and the window says so.

The Docker image stays the path for a shared studio server (a Linux box
with a dedicated IP), where mDNS works natively too.

Env knobs (all optional): PORT (default 8880), HOST (default 0.0.0.0 so
other devices on the LAN can reach it), WEBATEM_DATA_DIR (override the
per-user data location), WEBATEM_NO_BROWSER=1 (don't auto-open a browser),
WEBATEM_NO_TRAY=1 (foreground mode even on a desktop), WEBATEM_NO_WINDOW=1
(tray only, no window). ``--autostart`` on the command line is what the
login entry passes: no browser at boot, window hidden.
"""
import errno
import os
import plistlib
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_NAME = 'WebATEM'
BUNDLE_ID = 'com.webatem.app'


def _data_dir() -> Path:
    """A per-user, writable location for the SQLite DB, generated secret,
    and upload scratch. The frozen bundle itself is read-only, so runtime
    state must live outside it."""
    env = os.environ.get('WEBATEM_DATA_DIR') or os.environ.get('DATA_DIR')
    if env:
        path = Path(env)
    elif sys.platform == 'darwin':
        path = Path.home() / 'Library' / 'Application Support' / APP_NAME
    elif os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or str(Path.home())
        path = Path(base) / APP_NAME
    else:
        xdg = os.environ.get('XDG_DATA_HOME') or str(Path.home() / '.local' / 'share')
        path = Path(xdg) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _lan_ip():
    """This machine's primary LAN IP (no packet sent — connect() just picks
    the source address). None if it can't be determined."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _has_display() -> bool:
    """True if a browser (and a tray) could plausibly exist here. Desktop OSes
    always qualify; on Linux it needs a display (a headless server has none)."""
    if sys.platform == 'darwin' or os.name == 'nt':
        return True
    return bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))


def _wait_for_port(port: int, timeout: float = 30, host: str = '127.0.0.1') -> bool:
    """True once something accepts connections on host:port (loopback when
    the server listens on every interface)."""
    if host in ('0.0.0.0', ''):
        host = '127.0.0.1'
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _urls(host: str, port: int):
    """(local_url, lan_url) for a listen address. Loopback always answers
    (the supervisor listens there beside a specific interface), so the local
    address is 127.0.0.1 whatever the interface; the one to hand out is the
    machine's LAN address on all interfaces, else the interface itself."""
    local = f'http://127.0.0.1:{port}/atem/'
    if host in ('0.0.0.0', ''):
        lan = _lan_ip()
        return (local, f'http://{lan}:{port}/atem/' if lan else None)
    return (local, f'http://{host}:{port}/atem/')


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass


def _launch_target(url: str, fallback=None, timeout: float = 3.0):
    """(address to open, note). Launch GUI opens the address the window
    shows — the real interface and port, what other devices use — when this
    machine can connect to it; otherwise the fallback (loopback, always
    served) with a note saying so. A VPN address is not always reachable
    from the machine that owns it; the note and the log say which it was."""
    if not fallback:
        return url, None
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    try:
        with socket.create_connection((parts.hostname, parts.port or 80), timeout=timeout):
            return url, None
    except (OSError, ValueError, TypeError) as e:
        reason = getattr(e, 'strerror', None) or str(e) or 'no answer'
        note = (f'Opened {fallback} — this computer cannot reach {parts.hostname}:{parts.port} itself '
                f'({reason}). Other devices use {url}.')
        print(f'Launch GUI: {note}', flush=True)
        return fallback, note


def _reachable_url(url: str, fallback=None, timeout: float = 3.0) -> str:
    return _launch_target(url, fallback, timeout)[0]


# ---------------------------------------------------------------------------
# Start at login — the native mechanism per OS: a file (or a registry value)
# the OS reads at the next login. Nothing is started now, the app is already
# running; disabling is the reverse.
# ---------------------------------------------------------------------------

def _launch_command() -> list:
    """argv that starts THIS WebATEM again: the frozen executable, or the
    interpreter plus this script when run from a checkout."""
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--autostart']
    return [sys.executable, '-m', 'webatem', '--autostart']


def _autostart_path() -> Path:
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'LaunchAgents' / f'{BUNDLE_ID}.plist'
    cfg = os.environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')
    return Path(cfg) / 'autostart' / 'webatem.desktop'


_WIN_RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'


def autostart_enabled() -> bool:
    if os.name == 'nt':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY) as key:
                winreg.QueryValueEx(key, APP_NAME)
                return True
        except OSError:
            return False
    return _autostart_path().exists()


def set_autostart(enabled: bool) -> None:
    cmd = _launch_command()
    if os.name == 'nt':
        import subprocess
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return
    path = _autostart_path()
    if not enabled:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == 'darwin':
        plist = {
            'Label': BUNDLE_ID,
            'ProgramArguments': cmd,
            'RunAtLoad': True,
            'ProcessType': 'Interactive',
        }
        with open(path, 'wb') as f:
            plistlib.dump(plist, f)
    else:
        import shlex
        path.write_text(
            '[Desktop Entry]\n'
            'Type=Application\n'
            f'Name={APP_NAME}\n'
            f'Exec={shlex.join(cmd)}\n'
            'X-GNOME-Autostart-enabled=true\n'
            'Terminal=false\n'
        )


# ---------------------------------------------------------------------------
# The server, supervised: uvicorn in a worker thread, restarted on a new
# address when the launcher window asks (Companion's "run on interface X,
# port Y"), the process — and the tray — staying up throughout.
# ---------------------------------------------------------------------------

def _bind(host: str, port: int) -> socket.socket:
    """A listening socket, bound now — so a taken port or a gone interface is
    an OSError here and not a SystemExit inside uvicorn's thread."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        s.listen(2048)
    except OSError:
        s.close()
        raise
    return s


def _close_all(sockets) -> None:
    for s in sockets:
        try:
            s.close()
        except OSError:
            pass


class _Supervisor:
    """Two uvicorn servers on one app, each in its own thread:

    * the PUBLIC one on the chosen interface and port — restarted when the
      launcher window asks (Companion's "run on interface X, port Y"). When
      the interface is one specific address it ALSO listens on 127.0.0.1, so
      this computer can always reach its own server (a VPN address is not
      necessarily reachable from the machine that owns it — 0.2.3 sent
      Launch GUI to a dead address that way);
    * the LAUNCHER one on 127.0.0.1 and a free port, never restarted — the
      window's own address. 0.2.3 served the window from the public server,
      so moving that server killed the page that was steering it (stuck on
      Restarting, Quit refused, a white window on the next Show).
    """
    PORT_TRIES = 20     # when the configured port is taken, walk up this far
    ANY = ('0.0.0.0', '127.0.0.1', '', 'localhost')

    def __init__(self, application, host: str, port: int, launcher_socket=None):
        self.application = application      # set by the boot thread when None here
        self.host, self.port = host, port
        self._launcher_socket = launcher_socket   # bound early so the window can open at once
        self.running = False                # a public server is up on host:port
        self.restarting = False
        self.last_error = None
        self.startup_note = None            # "port X was in use; using Y", "interface gone"
        self.launcher_port = None           # the window's own server
        self.launcher_ready = threading.Event()
        self.ready = threading.Event()      # set once the public server first answers
        self.on_change = None               # hook after a restart
        self._wake = threading.Event()
        self._pending = None
        self._stopping = False
        self._thread = None
        self._launcher = (None, None, [])

    @property
    def launcher_url(self):
        return f'http://127.0.0.1:{self.launcher_port}/launcher/' if self.launcher_port else None

    def boot_failed(self, error: str) -> None:
        """The boot thread could not bring Django up: nothing will serve."""
        self.last_error = error
        self.launcher_ready.set()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name='webatem-supervisor', daemon=True)
        self._thread.start()

    def restart(self, host: str, port: int) -> None:
        """Called from a request handler: hand the change to the supervisor
        thread and return, so the HTTP response gets out first."""
        self._pending = (host, port)
        self._wake.set()

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()

    def join(self, timeout=None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _listen(self, host: str, port: int):
        sockets = [_bind(host, port)]
        if host not in self.ANY:
            try:
                sockets.append(_bind('127.0.0.1', port))
            except OSError:
                _close_all(sockets)
                raise
        return sockets

    def _serve(self, host: str, port: int, sockets):
        import uvicorn
        # host/port are for the log line only: the sockets are bound already.
        server = uvicorn.Server(uvicorn.Config(self.application, host=host, port=port, log_level='info',
                                               timeout_graceful_shutdown=5))

        def run():
            try:
                server.run(sockets=sockets)
            except SystemExit:
                pass
            except Exception as e:  # noqa: BLE001
                print(f'server thread ended: {e}', flush=True)

        worker = threading.Thread(target=run, name=f'webatem-uvicorn-{port}', daemon=True)
        worker.start()
        return server, worker

    @staticmethod
    def _came_up(server, worker, timeout: float = 15) -> bool:
        """uvicorn's own word: ``started`` flips once it is serving; a failure
        ends the thread instead."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if getattr(server, 'started', False):
                return True
            if not worker.is_alive():
                return False
            time.sleep(0.05)
        return False

    @staticmethod
    def _shutdown(server, worker, sockets) -> None:
        if server is not None:
            server.should_exit = True
            worker.join(15)
        _close_all(sockets)

    def _start_public(self, host: str, port: int):
        """Bind + serve; (server, worker, sockets), or OSError from the bind /
        RuntimeError if uvicorn did not come up."""
        sockets = self._listen(host, port)
        server, worker = self._serve(host, port, sockets)
        if not self._came_up(server, worker):
            self._shutdown(server, worker, sockets)
            raise RuntimeError(f'the server did not start on {host}:{port}')
        return server, worker, sockets

    @staticmethod
    def _reason(e) -> str:
        return getattr(e, 'strerror', None) or str(e)

    def _loop(self) -> None:
        # 1. The window's own server: loopback, any free port, for the life
        #    of the process. (HTTP only — the window never opens a WebSocket
        #    here; those go to the public server, whose event loop owns the
        #    channel layer.)
        try:
            sock = self._launcher_socket or _bind('127.0.0.1', 0)
            self.launcher_port = sock.getsockname()[1]
            server, worker = self._serve('127.0.0.1', self.launcher_port, [sock])
            self._launcher = (server, worker, [sock])
            if not self._came_up(server, worker):
                raise RuntimeError('did not come up')
            print(f'{APP_NAME} window server on 127.0.0.1:{self.launcher_port}', flush=True)
        except Exception as e:  # noqa: BLE001
            print(f'The launcher window server could not start: {e}', flush=True)
            self._shutdown(*self._launcher)
            self._launcher, self.launcher_port = (None, None, []), None
        finally:
            self.launcher_ready.set()

        # 2. The public server: the configured address; a gone interface
        #    (the VPN is off today) falls back to all interfaces, a taken
        #    port (Companion, an earlier WebATEM) to the next free one.
        host, wanted = self.host, self.port
        port, tries, notes = wanted, 0, []
        current = (None, None, [])
        while True:
            try:
                current = self._start_public(host, port)
                break
            except OSError as e:
                if e.errno == errno.EADDRNOTAVAIL and host not in self.ANY:
                    notes.append(f'{host} is not an address of this computer right now, so WebATEM is listening on all interfaces.')
                    host = '0.0.0.0'
                    continue
                tries += 1
                if tries > self.PORT_TRIES:
                    self.last_error = f'could not listen on {host}:{port} ({self._reason(e)})'
                    break
                port = wanted + tries
            except RuntimeError as e:
                self.last_error = str(e)
                break
        if current[0] is not None:
            if port != wanted:
                notes.append(f'Port {wanted} was in use, so WebATEM is on port {port} this time.')
            self.host, self.port = host, port
            print(f'{APP_NAME} listening on {host}:{port}' + (' (and on 127.0.0.1)' if host not in self.ANY else ''), flush=True)
            self.startup_note = ' '.join(notes) or None
            if self.startup_note:
                print(self.startup_note, flush=True)
            self.running, self.last_error = True, None
            self.ready.set()
        else:
            print(f'{APP_NAME} is not running: {self.last_error}', flush=True)

        # 3. Restarts from the window / the settings page.
        while True:
            self._wake.wait()
            self._wake.clear()
            if self._stopping:
                break
            host, port = self._pending
            self._pending = None
            self.restarting = True
            time.sleep(0.5)                     # let the settings response reach the page
            previous = (self.host, self.port) if self.running else None
            self._shutdown(*current)
            current, self.running = (None, None, []), False
            try:
                current = self._start_public(host, port)
                self.host, self.port, self.last_error, self.startup_note = host, port, None, None
                print(f'{APP_NAME} now listening on {host}:{port}', flush=True)
            except (OSError, RuntimeError) as e:
                self.last_error = f'could not listen on {host}:{port} ({self._reason(e)})'
                print(self.last_error + (f'; back to {previous[0]}:{previous[1]}' if previous else ''), flush=True)
                if previous:
                    try:
                        current = self._start_public(*previous)
                    except (OSError, RuntimeError) as e2:
                        print(f'and could not go back either: {self._reason(e2)}', flush=True)
            self.running = current[0] is not None
            self.restarting = False
            if self.on_change:
                try:
                    self.on_change()
                except Exception:
                    pass
        self._shutdown(*current)
        self._shutdown(*self._launcher)


class _Controller:
    """What the web app sees (webatem.server.runtime): the live address, a
    restart, the start-at-login switch, and Quit."""

    def __init__(self, supervisor):
        self._s = supervisor
        self.on_quit = None

    @property
    def host(self):
        return self._s.host

    @property
    def port(self):
        return self._s.port

    @property
    def last_error(self):
        return self._s.last_error

    @property
    def startup_note(self):
        return self._s.startup_note

    @property
    def running(self):
        return self._s.running

    @property
    def restarting(self):
        return self._s.restarting

    def restart(self, host, port):
        self._s.restart(host, port)

    def quit(self):
        # From a request handler: let the response out, then stop the tray.
        if self.on_quit:
            threading.Timer(0.3, self.on_quit).start()

    autostart_enabled = staticmethod(autostart_enabled)
    set_autostart = staticmethod(set_autostart)


# ---------------------------------------------------------------------------
# The launcher window: this same program run again with --window <url>,
# showing the page in a native webview (pywebview). Its own process, so its
# event loop never competes with the tray's.
# ---------------------------------------------------------------------------

def _window_support() -> bool:
    """Is pywebview there? Checked WITHOUT importing it: importing its macOS
    backend switches this process to a regular (Dock) application at import
    time, which is exactly what took the menu-bar icon away in 0.2.2. The
    window process imports it; this one never does."""
    if os.environ.get('WEBATEM_NO_WINDOW') == '1':
        return False
    try:
        import importlib.util
        return importlib.util.find_spec('webview') is not None
    except Exception:  # noqa: BLE001
        return False


def _on_ui_thread(fn) -> None:
    """Run fn on the tray's event loop thread where that matters (macOS: the
    NSApplication is stopped from the main thread only)."""
    if sys.platform == 'darwin':
        try:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(fn)
            return
        except Exception:  # noqa: BLE001
            pass
    fn()


_LOADING_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>WebATEM</title>
<style>html,body{height:100%;margin:0;background:#111;color:#bbb;font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.c{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px}
.s{width:26px;height:26px;border:3px solid #333;border-top-color:#f68b2a;border-radius:50%;animation:r 1s linear infinite}
@keyframes r{to{transform:rotate(360deg)}}</style></head>
<body><div class="c"><div class="s"></div><div>WebATEM is starting…</div></div></body></html>"""


def _window_process(url: str) -> None:
    """The child: a native window showing "Starting…" until the launcher
    page answers, then the page itself. Spawned before Django boots so the
    window is on screen while the server comes up."""
    import webview
    if sys.platform == 'darwin':
        # pywebview's Cocoa backend turns the process into a regular (Dock)
        # application the moment it is imported. This is a menu-bar app's
        # window, like Companion's: load that backend now (start() would),
        # then back to an accessory app — no Dock icon, the window still
        # shows and takes focus.
        try:
            import webview.platforms.cocoa  # noqa: F401
            import AppKit
            AppKit.NSApplication.sharedApplication().setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        except Exception as e:  # noqa: BLE001
            print(f'could not hide the Dock icon: {e}', flush=True)

    class Api:
        def launch(self, target, fallback=None):
            opened, note = _launch_target(target, fallback)
            _open_browser(opened)
            return {'opened': opened, 'note': note}

        def hide(self):
            window.destroy()

    window = webview.create_window(APP_NAME, html=_LOADING_HTML, js_api=Api(), width=520, height=700, resizable=False)
    if os.name == 'nt':
        # No taskbar button: the app lives in the notification area. Set
        # before the form is shown (before_show fires on the UI thread with
        # the form built and not yet shown), so no window handle is rebuilt.
        def no_taskbar(*_):
            try:
                from webview.platforms import winforms
                winforms.BrowserView.instances[window.uid].ShowInTaskbar = False
            except Exception as e:  # noqa: BLE001
                print(f'could not hide the taskbar button: {e}', flush=True)
        window.events.before_show += no_taskbar

    def follow():
        import urllib.request
        deadline = time.time() + 300
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=1) as r:
                    if r.status == 200:
                        break
            except Exception:  # noqa: BLE001 — not up yet
                pass
            time.sleep(0.4)
        window.load_url(url)

    webview.start(follow)


class _WindowChild:
    """Show spawns the window process, Hide ends it; the close button ends
    it too (the page's Hide and Quit go through the bridge / the server)."""

    def __init__(self, supervisor):
        self._s = supervisor
        self._proc = None

    def _command(self):
        url = self._s.launcher_url
        if getattr(sys, 'frozen', False):
            return [sys.executable, '--window', url]
        return [sys.executable, '-m', 'webatem', '--window', url]

    def visible(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def show(self) -> None:
        if self.visible():
            return
        if not self._s.launcher_url:
            print('The launcher window has no server to show.', flush=True)
            return
        import subprocess
        try:
            self._proc = subprocess.Popen(self._command(), stdin=subprocess.DEVNULL,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:  # noqa: BLE001
            print(f'The launcher window could not open: {e}', flush=True)
            self._proc = None

    def hide(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()

            def reap():
                try:
                    proc.wait(3)
                except Exception:  # noqa: BLE001
                    proc.kill()
            threading.Thread(target=reap, daemon=True).start()

    def toggle(self) -> None:
        self.hide() if self.visible() else self.show()


# ---------------------------------------------------------------------------
# The tray (pystray) — on the main thread, as macOS insists (0.2.1's proven
# shape). The server runs supervised in threads; Quit stops everything.
# ---------------------------------------------------------------------------

def _tray_image():
    """The app icon for the tray, from the package's static files; a plain
    orange square if that ever goes missing."""
    from PIL import Image
    try:
        import atem_control
        icon = Path(atem_control.__file__).parent / 'static' / 'icon.png'
        return Image.open(icon).convert('RGBA')
    except Exception:
        return Image.new('RGBA', (64, 64), (246, 139, 42, 255))


def _run_tray(supervisor, controller, window) -> None:
    """Menu bar / system tray icon; returns when the user quits."""
    import pystray
    from pystray import Menu, MenuItem

    def local_url():
        return _urls(supervisor.host, supervisor.port)[0]

    def gui_url():
        local, lan = _urls(supervisor.host, supervisor.port)
        return _reachable_url(lan or local, local)

    def shown_address(item=None):
        local, lan = _urls(supervisor.host, supervisor.port)
        return f'{APP_NAME} is running at {lan or local}'

    def toggle_autostart(icon, item):
        try:
            set_autostart(not autostart_enabled())
        except Exception as e:  # noqa: BLE001 — a failed toggle must not kill the tray
            print(f'Start at login could not be changed: {e}', flush=True)

    def quit_app(icon, item=None):
        if window is not None:
            window.hide()
        _on_ui_thread(icon.stop)

    if window is not None:
        # Companion's three: the window carries the settings.
        menu = Menu(
            MenuItem('Show/Hide window', lambda icon, item: window.toggle(), default=True),
            MenuItem('Launch GUI', lambda icon, item: _open_browser(gui_url())),
            Menu.SEPARATOR,
            MenuItem('Quit', quit_app),
        )
    else:
        menu = Menu(
            MenuItem(shown_address, None, enabled=False),
            MenuItem('Open in browser', lambda icon, item: _open_browser(gui_url()), default=True),
            MenuItem('Server settings…', lambda icon, item: _open_browser(local_url().replace('/atem/', '/launcher/'))),
            MenuItem('Start at login', toggle_autostart, checked=lambda item: autostart_enabled()),
            Menu.SEPARATOR,
            MenuItem('Quit', quit_app),
        )
    icon = pystray.Icon(APP_NAME, _tray_image(), APP_NAME, menu)
    supervisor.on_change = icon.update_menu
    controller.on_quit = lambda: quit_app(icon)

    def after_start():
        if not supervisor.ready.wait(90):
            try:
                if getattr(icon, 'HAS_NOTIFICATION', False):
                    icon.notify(f'{APP_NAME} is not running: {supervisor.last_error}', APP_NAME)
            except Exception:
                pass

    threading.Thread(target=after_start, daemon=True).start()
    icon.run()


def main() -> None:
    argv = sys.argv[1:]
    if '--window' in argv:
        i = argv.index('--window')
        _window_process(argv[i + 1] if i + 1 < len(argv) else 'http://127.0.0.1:8880/launcher/')
        return
    autostarted = '--autostart' in argv
    data_dir = _data_dir()
    # A windowed build has no console: send output to a log in the data dir
    # so a failure is diagnosable instead of silent.
    if sys.stdout is None or sys.stderr is None:
        log = open(data_dir / 'webatem.log', 'a', buffering=1)
        sys.stdout = sys.stderr = log

    # settings.py reads DATA_DIR at import time — set it before django.setup().
    os.environ['DATA_DIR'] = str(data_dir)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'webatem.settings')
    os.environ.setdefault('DEBUG', 'False')  # static via WhiteNoise
    # The package's own directory is read-only (site-packages, a frozen
    # bundle): WhiteNoise serves from a collected copy in the data dir,
    # refreshed at every start so an upgrade never serves stale assets.
    os.environ.setdefault('STATIC_ROOT', str(data_dir / 'staticfiles'))

    from webatem import server as srv       # no Django needed for the config
    cfg = srv.load()                        # HOST/PORT env > server.json > defaults
    host, port = cfg['host'], cfg['port']

    want_tray = _has_display() and os.environ.get('WEBATEM_NO_TRAY') != '1'
    if want_tray:
        try:
            import pystray  # noqa: F401 — probe only; imported for real in _run_tray
        except Exception as e:  # noqa: BLE001
            print(f'No tray ({e}); running in the foreground.', flush=True)
            want_tray = False
    has_window = want_tray and _window_support()
    # With the window there is no browser at start: the window is the first
    # thing (Launch GUI opens the browser). Without it, the browser opens as
    # before — unless this is the login-time start.
    want_browser = _has_display() and not has_window and os.environ.get('WEBATEM_NO_BROWSER') != '1' and not autostarted

    # The window's own port is known before anything else runs, so the
    # window (a second process) can be opening while Django boots here.
    launcher_socket = None
    if has_window:
        try:
            launcher_socket = _bind('127.0.0.1', 0)
        except OSError as e:
            print(f'No loopback port for the launcher window ({e}); the window is off.', flush=True)
            has_window = False
    supervisor = _Supervisor(None, host, port, launcher_socket=launcher_socket)
    if launcher_socket is not None:
        supervisor.launcher_port = launcher_socket.getsockname()[1]
    controller = _Controller(supervisor)
    srv.runtime.register(controller)
    window = _WindowChild(supervisor) if has_window else None
    show_window = window is not None and not (bool(cfg.get('start_minimized')) or autostarted)
    if show_window:
        window.show()

    def boot():
        try:
            import django
            django.setup()
            from django.core.management import call_command
            call_command('migrate', '--noinput', verbosity=0)
            call_command('collectstatic', '--noinput', '--clear', verbosity=0)
            from webatem.asgi import application
        except Exception as e:  # noqa: BLE001
            print(f'{APP_NAME} could not start: {e}', flush=True)
            supervisor.boot_failed(f'could not start: {e}')
            return
        supervisor.application = application
        supervisor.start()
        local_url, lan_url = _urls(host, port)
        lines = ['', f'  {APP_NAME} is starting on {host}:{port}.']
        lines.append(f'  On this machine:      {local_url}')
        if lan_url and lan_url != local_url:
            lines.append(f'  From another device:  {lan_url}')
        lines.append('  Address and port: the launcher window, or /launcher/ in a browser.')
        lines.append('  (Quit from the tray icon.)' if want_tray else '  (Press Ctrl+C to quit.)')
        lines.append('')
        print('\n'.join(lines), flush=True)
        if want_browser and supervisor.ready.wait(60):
            local, lan = _urls(supervisor.host, supervisor.port)
            _open_browser(_reachable_url(lan or local, local))

    booter = threading.Thread(target=boot, name='webatem-boot', daemon=True)
    booter.start()

    try:
        if want_tray:
            _run_tray(supervisor, controller, window)
        else:
            booter.join()
            while supervisor._thread is not None and supervisor._thread.is_alive():
                supervisor._thread.join(1)
    except KeyboardInterrupt:
        pass
    finally:
        if window is not None:
            window.hide()
        supervisor.stop()
        supervisor.join(20)


if __name__ == '__main__':
    main()
