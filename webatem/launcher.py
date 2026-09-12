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
the window process, Hide ends it. Where the window library is missing it
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
    """(local_url, lan_url) for a listen address: on all interfaces the
    machine's LAN address is the one to hand out; on a specific address
    that address is both."""
    if host in ('0.0.0.0', ''):
        lan = _lan_ip()
        return (f'http://127.0.0.1:{port}/atem/', f'http://{lan}:{port}/atem/' if lan else None)
    return (f'http://{host}:{port}/atem/', f'http://{host}:{port}/atem/')


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass


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

class _Supervisor:
    PORT_TRIES = 20     # when the configured port is taken, walk up this far

    def __init__(self, application, host: str, port: int):
        self.application = application
        self.host, self.port = host, port
        self.last_error = None
        self.startup_note = None            # "port X was in use; using Y"
        self.ready = threading.Event()      # set once a server answers
        self.on_change = None               # hook after a restart
        self._wake = threading.Event()
        self._pending = None
        self._stopping = False
        self._thread = None

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

    def _serve(self, host: str, port: int):
        import uvicorn
        server = uvicorn.Server(uvicorn.Config(self.application, host=host, port=port, log_level='info'))

        def run():
            try:
                server.run()
            except SystemExit:
                pass            # uvicorn's exit on a failed bind; _came_up reports it
            except Exception as e:  # noqa: BLE001
                print(f'server thread ended: {e}', flush=True)

        worker = threading.Thread(target=run, name='webatem-uvicorn', daemon=True)
        worker.start()
        return server, worker

    @staticmethod
    def _came_up(server, worker, timeout: float = 15) -> bool:
        """uvicorn's own word: ``started`` flips once it is bound and serving;
        a failed bind ends the thread instead. (Probing the port would be
        fooled by whatever else holds it.)"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if getattr(server, 'started', False):
                return True
            if not worker.is_alive():
                return False
            time.sleep(0.05)
        return False

    @staticmethod
    def _shutdown(server, worker) -> None:
        server.should_exit = True
        worker.join(15)

    def _loop(self) -> None:
        wanted = self.port
        server, worker = self._serve(self.host, self.port)
        up = self._came_up(server, worker)
        tries = 0
        while not up and tries < self.PORT_TRIES:
            # The port is taken (Companion on 8000, an earlier WebATEM, …):
            # take the next one rather than failing.
            self._shutdown(server, worker)
            tries += 1
            self.port = wanted + tries
            server, worker = self._serve(self.host, self.port)
            up = self._came_up(server, worker)
        if up:
            if self.port != wanted:
                self.startup_note = f'Port {wanted} was in use, so WebATEM is on port {self.port} this time.'
                print(self.startup_note, flush=True)
            self.ready.set()
        while True:
            self._wake.wait()
            self._wake.clear()
            if self._stopping:
                break
            host, port = self._pending
            self._pending = None
            time.sleep(0.5)                     # let the settings response reach the browser
            self._shutdown(server, worker)
            server, worker = self._serve(host, port)
            if self._came_up(server, worker):
                self.host, self.port, self.last_error = host, port, None
                self.startup_note = None
                print(f'{APP_NAME} now listening on {host}:{port}', flush=True)
            else:
                self.last_error = f'could not listen on {host}:{port} (is the port in use?)'
                print(self.last_error + '; back to ' + f'{self.host}:{self.port}', flush=True)
                self._shutdown(server, worker)
                server, worker = self._serve(self.host, self.port)
                self._came_up(server, worker)
            if self.on_change:
                try:
                    self.on_change()
                except Exception:
                    pass
        self._shutdown(server, worker)


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


def _window_process(url: str) -> None:
    import webview

    class Api:
        def launch(self, target):
            _open_browser(target)

        def hide(self):
            window.destroy()

    window = webview.create_window(APP_NAME, url, js_api=Api(), width=520, height=700, resizable=False)
    webview.start()


class _WindowChild:
    """Show spawns the window process, Hide ends it; the close button ends
    it too (the page's Hide and Quit go through the bridge / the server)."""

    def __init__(self, supervisor):
        self._s = supervisor
        self._proc = None

    def _command(self):
        url = f'http://127.0.0.1:{self._s.port}/launcher/'
        if getattr(sys, 'frozen', False):
            return [sys.executable, '--window', url]
        return [sys.executable, '-m', 'webatem', '--window', url]

    def visible(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def show(self) -> None:
        if self.visible():
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


def _run_tray(supervisor, controller, window, show_window: bool) -> None:
    """Menu bar / system tray icon; returns when the user quits."""
    import pystray
    from pystray import Menu, MenuItem

    def local_url():
        return _urls(supervisor.host, supervisor.port)[0]

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
            MenuItem('Launch GUI', lambda icon, item: _open_browser(local_url())),
            Menu.SEPARATOR,
            MenuItem('Quit', quit_app),
        )
    else:
        menu = Menu(
            MenuItem(shown_address, None, enabled=False),
            MenuItem('Open in browser', lambda icon, item: _open_browser(local_url()), default=True),
            MenuItem('Server settings…', lambda icon, item: _open_browser(local_url().replace('/atem/', '/launcher/'))),
            MenuItem('Start at login', toggle_autostart, checked=lambda item: autostart_enabled()),
            Menu.SEPARATOR,
            MenuItem('Quit', quit_app),
        )
    icon = pystray.Icon(APP_NAME, _tray_image(), APP_NAME, menu)
    supervisor.on_change = icon.update_menu
    controller.on_quit = lambda: quit_app(icon)

    def after_start():
        if not supervisor.ready.wait(60):
            try:
                if getattr(icon, 'HAS_NOTIFICATION', False):
                    icon.notify(f'{APP_NAME} could not find a free port to start on.', APP_NAME)
            except Exception:
                pass
            time.sleep(3)
            icon.stop()
            return
        if window is not None and show_window:
            window.show()

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

    import django
    django.setup()

    from django.core.management import call_command
    call_command('migrate', '--noinput', verbosity=0)
    call_command('collectstatic', '--noinput', '--clear', verbosity=0)

    from webatem import server as srv
    cfg = srv.load()                      # HOST/PORT env > server.json > defaults
    host, port = cfg['host'], cfg['port']

    from webatem.asgi import application

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

    supervisor = _Supervisor(application, host, port)
    controller = _Controller(supervisor)
    srv.runtime.register(controller)
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

    if want_browser:
        threading.Thread(target=lambda: supervisor.ready.wait(60) and _open_browser(_urls(supervisor.host, supervisor.port)[0]), daemon=True).start()

    window = _WindowChild(supervisor) if has_window else None
    try:
        if want_tray:
            _run_tray(supervisor, controller, window, show_window=not (bool(cfg.get('start_minimized')) or autostarted))
        else:
            while supervisor._thread.is_alive():
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
