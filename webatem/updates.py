"""One-click updates for the desktop launcher (1.7.0).

Lucas, 2026-10-06: it checks on its own (a box in the launcher window turns
that off), and one click installs.

**Checking.** GitHub's latest release, 20 seconds after the launcher starts
and every 12 hours after, while "Check for updates automatically" is on;
"Check now" asks at once either way. An offline or air-gapped machine just
finds nothing and says so quietly.

**Installing** (the desktop builds). The download for this OS and chip goes
into the data folder and is checked against the SHA-256 GitHub publishes for
it: no checksum, no install; a mismatch, no install. Then a separate process
takes over, waits for this one to exit, and puts the new version where the
old one is:

* Windows: the new installer, silently, over the existing copy. That is an
  update, not an uninstall, so it keeps everything. It starts the app again
  (``/relaunch=1``, webatem.iss).
* macOS: the bundle's own install-mac.sh on the downloaded .dmg, into the
  folder the app is in (copied out first: it replaces the bundle it lives
  in).
* Linux: the new binary renamed over the old one (same folder, atomic).

Every relaunch passes ``--resume``: the server comes back on the interface
the user already chose, instead of waiting for a choice at the next launch.
pip, pipx and Docker update through their own tools; the window says what to
run there. The settings file, the history and the key are never touched.

The status is part of the settings JSON (anyone can see that an update
exists); checking and installing go through the launcher window's native
bridge and the tray menu only, like Uninstall.
"""
import hashlib
import json
import os
import platform as _platform
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

FEED = 'https://api.github.com/repos/lucas-romanenko/webatem/releases/latest'
FIRST_CHECK_AFTER = 20            # seconds after start: let the launcher come up first
CHECK_EVERY = 12 * 3600


class UpdateError(Exception):
    """Something the window shows as it is."""


def feed_url() -> str:
    # The build workflow points this at a local copy of a release to prove
    # the whole path on each OS.
    return os.environ.get('WEBATEM_UPDATE_FEED') or FEED


def current_version() -> str:
    try:
        from importlib.metadata import version
        return version('webatem')
    except Exception:  # noqa: BLE001 — a checkout without metadata
        return '0'


def parse(version):
    """``'v1.7.0'`` → ``(1, 7, 0)``; None when it is not a release number."""
    parts = str(version or '').strip().lstrip('vV').split('.')
    try:
        nums = [int(p) for p in parts[:3]]
    except ValueError:
        return None
    return tuple(nums + [0] * (3 - len(nums))) if nums else None


def is_newer(latest, current) -> bool:
    a, b = parse(latest), parse(current)
    return bool(a and b and a > b)


def asset_name(platform=None, machine=None):
    """The release download that fits this machine, or None. A Mac answers
    by the chip of the build that is running (an Intel build under Rosetta
    reports x86_64), so an update never swaps one for the other."""
    platform = platform or sys.platform
    machine = (machine or _platform.machine()).lower()
    if platform == 'darwin':
        return 'webatem-macos-arm64.dmg' if machine in ('arm64', 'aarch64') else 'webatem-macos-intel.dmg'
    if platform.startswith('win'):
        return 'webatem-windows-x64-setup.exe'
    if platform.startswith('linux') and machine in ('x86_64', 'amd64'):
        return 'webatem-linux-x64'
    return None


def how() -> dict:
    """How this install updates: ``{'by': 'app'}`` for the desktop builds,
    ``{'by': 'package', 'command': …}`` for pip and pipx."""
    from webatem import uninstall
    if uninstall.windows_uninstaller() or uninstall.mac_app_bundle() or uninstall.linux_binary():
        return {'by': 'app'}
    pipx = 'pipx' in sys.prefix.replace('\\', '/').split('/')
    return {'by': 'package', 'command': 'pipx upgrade webatem' if pipx else 'pip install --upgrade webatem'}


def _tls():
    """The HTTPS context for GitHub. A frozen Python on macOS has no CA store
    of its own (a python.org build looks for one beside its framework, which
    is not in the app): 1.7.0's first check on a Mac said
    CERTIFICATE_VERIFY_FAILED. So: the operating system's own trust store
    first (truststore: the Keychain, Windows' store, the distro's bundle),
    which also trusts a studio's own CA where IT runs one; then Mozilla's list
    (certifi); then whatever this Python has. Verification is never off."""
    import ssl
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # noqa: BLE001 — not installed, or this OS's backend refused
        pass
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


def _open(url, timeout):
    return urllib.request.urlopen(_request(url), timeout=timeout, context=_tls())


def _request(url):
    return urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json',
                                                'User-Agent': f'WebATEM/{current_version()}'})


def check(timeout: float = 10) -> dict:
    """Ask the feed. Raises on no answer (the caller says it quietly)."""
    with _open(feed_url(), timeout) as r:
        release = json.load(r)
    latest = str(release.get('tag_name') or '').lstrip('vV')
    name = asset_name()
    asset = next((a for a in release.get('assets') or [] if a.get('name') == name), None)
    return {
        'latest': latest,
        'current': current_version(),
        'available': is_newer(latest, current_version()),
        'page': release.get('html_url'),
        'asset': {'name': asset['name'], 'url': asset['browser_download_url'],
                  'digest': asset.get('digest'), 'size': asset.get('size')} if asset else None,
    }


def updates_dir() -> Path:
    from webatem import uninstall
    return uninstall.data_dir() / 'updates'


def download(asset: dict, progress=None) -> Path:
    """Fetch ``asset`` into the updates folder and prove it is the file
    GitHub published (SHA-256). ``progress(done, total)`` as it goes."""
    digest = str(asset.get('digest') or '')
    if not digest.startswith('sha256:'):
        raise UpdateError('GitHub published no checksum for this download, so it was not installed.')
    folder = updates_dir()
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / asset['name']
    part = dest.with_name(dest.name + '.part')
    h = hashlib.sha256()
    done = 0
    with _open(asset['url'], 30) as r, open(part, 'wb') as f:
        total = int(r.headers.get('Content-Length') or asset.get('size') or 0)
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    if h.hexdigest() != digest.split(':', 1)[1].strip().lower():
        part.unlink(missing_ok=True)
        raise UpdateError('The download did not match its published checksum, so nothing was installed.')
    os.replace(part, dest)
    return dest


# What WebATEM itself reads from its environment: carried into a macOS
# relaunch, which `open` would otherwise start with the login session's
# environment (the build workflow's update test runs the app on a port and
# data folder of its own).
_CARRIED = ('HOST', 'PORT', 'DATA_DIR', 'WEBATEM_DATA_DIR', 'WEBATEM_NO_TRAY', 'WEBATEM_NO_BROWSER',
            'WEBATEM_NO_WINDOW', 'WEBATEM_UPDATE_FEED')

# Waits for this process to exit, then runs the rest. $0 is the PID.
_WAIT = 'while kill -0 "$0" 2>/dev/null; do sleep 0.2; done; '


def _fresh_env() -> dict:
    """This environment minus what the PyInstaller bootloader put in it.
    Whatever this hands over to ends by starting the NEW app, which must not
    take itself for a child of this one: on Linux (onefile) it then looks
    for this process's unpacked copy, gone by then, and dies at once ("Failed
    to load Python shared library", the first CI run of 1.7.0).
    PYINSTALLER_RESET_ENVIRONMENT is the bootloader's own switch for that
    (6.10+); the rest covers an older one."""
    env = {k: v for k, v in os.environ.items() if not (k.startswith('_PYI_') or k.startswith('_MEIPASS'))}
    for var in ('LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH', 'LIBPATH'):
        if f'{var}_ORIG' in env:
            env[var] = env.pop(f'{var}_ORIG')
        elif getattr(sys, 'frozen', False):
            env.pop(var, None)
    env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    return env


def _detached(args, log: Path) -> None:
    out = open(log, 'ab')
    kw = {'stdin': subprocess.DEVNULL, 'stdout': out, 'stderr': out, 'close_fds': True, 'env': _fresh_env()}
    if os.name == 'nt':
        kw['creationflags'] = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    else:
        kw['start_new_session'] = True     # outlives this process (and a login item's process group)
    subprocess.Popen(args, **kw)


def windows_installer_args(path, relaunch: bool) -> list:
    """The new installer, silent, over the existing copy (an update, so the
    data stays), its log beside the download; /relaunch=1 starts the app
    again (webatem.iss, RelaunchAfterUpdate)."""
    args = [str(path), '/SILENT', '/SUPPRESSMSGBOXES', '/NORESTART', f'/LOG={updates_dir() / "install.log"}']
    if relaunch:
        args.append('/relaunch=1')
    return args


def apply(path: Path, relaunch: bool = True) -> None:
    """Hand ``path`` (a verified download) to whatever installs it once this
    process is gone. The caller quits right after."""
    from webatem import uninstall
    path = Path(path)
    updates_dir().mkdir(parents=True, exist_ok=True)
    log = updates_dir() / 'update.log'
    if os.name == 'nt':
        _detached(windows_installer_args(path, relaunch), log)
        return
    if sys.platform == 'darwin':
        bundle = uninstall.mac_app_bundle()
        if bundle is None:
            raise UpdateError('This copy of WebATEM is not an installed app, so it cannot replace itself.')
        script = Path(getattr(sys, '_MEIPASS', '')) / 'desktop' / 'install-mac.sh'
        if not script.is_file():
            raise UpdateError('The installer that came with this app is missing; install the update from the download page.')
        local = updates_dir() / 'install-mac.sh'
        shutil.copyfile(script, local)          # the script replaces the bundle it lives in
        dest = bundle.parent
        if not os.access(dest, os.W_OK):
            raise UpdateError(f'WebATEM cannot replace itself in {dest} (no permission); '
                              'install the update from the download page.')
        # Start the new version the way Finder does, through LaunchServices
        # (`open`). 1.7.2 exec'd the binary from this detached script, and
        # that copy did not behave like the app: with a VPN connected, its
        # page would not open on any interface, while the same build started
        # from Finder worked (Lucas, 2026-10-06). `open` hands the app the
        # login session's environment, not this one, so the few variables
        # WebATEM reads go across explicitly; a user's install sets none.
        carried = []
        for name in _CARRIED:
            if name in os.environ:
                carried += ['--env', f'{name}={os.environ[name]}']
        run = (_WAIT + 'WEBATEM_DMG="$1" WEBATEM_DEST="$2" WEBATEM_NO_OPEN=1 sh "$3" || exit 1; '
               + ('app="$2/WebATEM.app"; shift 3; exec /usr/bin/open "$@" "$app" --args --resume'
                  if relaunch else 'exit 0'))
        _detached(['/bin/sh', '-c', run, str(os.getpid()), str(path), str(dest), str(local)] + carried, log)
        return
    binary = uninstall.linux_binary()
    if binary is None:
        raise UpdateError('This copy of WebATEM is not the downloaded program, so it cannot replace itself.')
    new = binary.with_name(binary.name + '.new')
    try:
        shutil.copyfile(path, new)
        new.chmod(0o755)
        os.replace(new, binary)                 # the running one keeps its open file; the next start is the new one
    except OSError as e:
        new.unlink(missing_ok=True)
        raise UpdateError(f'WebATEM cannot write to {binary.parent} ({e.strerror or e}).')
    if relaunch:
        _detached(['/bin/sh', '-c', _WAIT + 'exec "$1" --resume', str(os.getpid()), str(binary)], log)


def clear_downloads() -> None:
    """At start: last time's download has done its job. The logs stay, so a
    failed update can still be read about afterwards."""
    try:
        entries = list(updates_dir().iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.suffix == '.log':
            continue
        try:
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
        except OSError:
            pass


class Updater:
    """The launcher's update state: the timer, ``check_now``, ``install``.
    ``on_change`` is called whenever ``status()`` would answer differently
    (the tray redraws its menu)."""

    def __init__(self, enabled, on_change=None):
        self._enabled = enabled            # callable: the "check automatically" setting
        self.on_change = on_change
        self.state = 'idle'                # idle checking current available error downloading installing
        self.result = None
        self.error = None
        self.progress = None
        self._wake = threading.Event()
        self._thread = None

    def status(self) -> dict:
        r = self.result or {}
        out = {'state': self.state, 'current': current_version(), 'latest': r.get('latest'),
               'available': bool(r.get('available')), 'page': r.get('page'), 'error': self.error,
               'progress': self.progress, 'auto': bool(self._enabled())}
        out.update(how())
        if out['by'] == 'app' and r.get('available') and not r.get('asset'):
            out['by'] = 'page'             # no download for this machine: the page has the rest
        return out

    def _set(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self, k, v)
        if self.on_change:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001 — a redraw must not stop an update
                pass

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name='webatem-updates', daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        delay = FIRST_CHECK_AFTER
        while True:
            asked = self._wake.wait(delay)
            self._wake.clear()
            if asked or self._enabled():
                self.check()
            delay = CHECK_EVERY

    def check_now(self) -> None:
        self._wake.set()

    def check(self) -> None:
        if self.state in ('downloading', 'installing'):
            return
        self._set(state='checking', error=None)
        try:
            result = check()
        except Exception as e:  # noqa: BLE001 — offline is ordinary here
            self._set(state='error', error=_reason(e))
            print(f'update check: could not check: {self.error}', flush=True)
            return
        self._set(result=result, state='available' if result['available'] else 'current')
        print(f'update check: this is {result["current"]}, latest is {result["latest"]}'
              + (' (available)' if result['available'] else ''), flush=True)

    def install(self, quit_app) -> None:
        """Download, verify, hand over, quit. In a thread: the tray and the
        window keep answering while it downloads."""
        if self.state != 'available' or how()['by'] != 'app':
            return

        def run():
            asset = (self.result or {}).get('asset')
            latest = (self.result or {}).get('latest')
            try:
                print(f'update: downloading {latest} ({(asset or {}).get("name")})', flush=True)
                self._set(state='downloading', progress=0, error=None)
                path = download(asset, progress=lambda done, total: self._progress(done, total))
                print(f'update: {path.name} matches its checksum; installing {latest}', flush=True)
                self._set(state='installing', progress=None)
                apply(path)
            except Exception as e:  # noqa: BLE001
                self._set(state='available', error=_reason(e), progress=None)
                print(f'update: not installed: {self.error}', flush=True)
                return
            time.sleep(0.5)                # the window shows "Installing" before it goes
            quit_app()

        threading.Thread(target=run, name='webatem-update-install', daemon=True).start()

    def _progress(self, done, total) -> None:
        pct = int(done * 100 / total) if total else None
        if pct != self.progress:
            self._set(progress=pct)


def _reason(e) -> str:
    if isinstance(e, UpdateError):
        return str(e)
    reason = getattr(e, 'reason', None)
    return f'no answer from GitHub ({reason or e})'


def cli(argv) -> int:
    """``webatem --update [--yes]``: check, say, install. A copy that was
    running is stopped first and started again afterwards; one that was not
    running is not started. Exit: 0 up to date or installing, 1 could not
    check (no network, TLS) or install, 2 an update waits for --yes or a
    terminal, 3 GitHub answered with an HTTP error (HTTPS itself works)."""
    from webatem import uninstall
    import urllib.error
    try:
        result = check()
    except urllib.error.HTTPError as e:
        # GitHub answered, so HTTPS itself works (a rate limit, say). Its own
        # exit code: the build workflow tells this from a TLS failure (1).
        print(f'Could not check for updates: GitHub answered HTTP {e.code} ({e.reason}).')
        return 3
    except Exception as e:  # noqa: BLE001
        print(f'Could not check for updates: {_reason(e)}')
        return 1
    if not result['available']:
        print(f'WebATEM {result["current"]} is up to date.')
        return 0
    print(f'WebATEM {result["latest"]} is available (this is {result["current"]}).')
    way = how()
    if way['by'] != 'app':
        print(f'Update it with:  {way["command"]}')
        return 0
    if not result['asset']:
        print(f'There is no download for this machine; see {result["page"]}')
        return 1
    if '--yes' not in argv:
        if not sys.stdin or not sys.stdin.isatty():
            print('Nothing installed: run it in a terminal to confirm, or add --yes.')
            return 2
        if input(f'Install {result["latest"]} now? [y/N] ').strip().lower() not in ('y', 'yes'):
            print('Nothing installed.')
            return 1
    try:
        path = download(result['asset'])
        was_running = uninstall.stop_running()
        apply(path, relaunch=was_running)
    except Exception as e:  # noqa: BLE001
        print(f'Not installed: {_reason(e)}')
        return 1
    print(f'Installing WebATEM {result["latest"]}' + (' and starting it again.' if was_running else '.'))
    return 0
