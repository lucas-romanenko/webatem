"""Uninstalling WebATEM: everything it put on this machine goes.

Lucas's rule (2026-09-14): uninstalling means everything goes, updating
keeps everything. Updates never come through here.

Each kind of install has its own owner for the program itself:

* **Windows installer.** Windows' own uninstaller (Settings > Installed apps,
  ``unins000.exe``) removes the program, the data folder and the Run value
  (``webatem.iss``). The app's Uninstall button starts that uninstaller and
  quits, so there is still exactly one way it happens.
* **macOS app.** macOS has no uninstall step: a Trash drag leaves the data
  folder, the login item (a LaunchAgent that would try to start a missing
  program at every login) and WebKit's folders for the launcher window.
  This removes all of them and the ``.app`` itself.
* **Linux binary.** The binary, the data folder, the autostart entry.
* **A package** (pip, pipx, a checkout). The data folder and the autostart
  entry; the program belongs to pip or pipx, so it says what to run.

What counts as the data folder is guarded: the per-user default folder
(named WebATEM, in the OS's place for app data) goes whole. A folder that
DATA_DIR points at loses only the files WebATEM writes there, because that
path is the operator's and could hold anything else.

Reached from the launcher window's Uninstall button through the native
window's bridge (never over HTTP: the settings endpoints answer the whole
network) and from ``webatem --uninstall``.
"""
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

APP_NAME = 'WebATEM'
BUNDLE_ID = 'com.webatem.app'

# What WebATEM writes in its data folder: the database (and SQLite's side
# files), the generated key, the listen address, the windowed build's log,
# the collected static files, upload scratch, the running launcher's PID.
DATA_ENTRIES = ('db.sqlite3', 'db.sqlite3-journal', 'db.sqlite3-wal', 'db.sqlite3-shm', '.secret_key',
                'server.json', 'webatem.log', 'webatem.pid', 'staticfiles', 'uploads', 'updates')

# WebKit keeps the macOS launcher window's website data under the bundle id
# (pywebview uses the default data store, private mode only clears it).
MAC_BUNDLE_LEFTOVERS = ('Library/WebKit/{id}', 'Library/Caches/{id}', 'Library/HTTPStorages/{id}',
                        'Library/HTTPStorages/{id}.binarycookies', 'Library/Saved Application State/{id}.savedState',
                        'Library/Preferences/{id}.plist')


def default_data_dir() -> Path:
    """The per-user data folder when DATA_DIR does not say otherwise."""
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / APP_NAME
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA') or str(Path.home())) / APP_NAME
    return Path(os.environ.get('XDG_DATA_HOME') or str(Path.home() / '.local' / 'share')) / APP_NAME


def data_dir() -> Path:
    """Where this WebATEM keeps its data (the launcher's own rule), without
    creating it."""
    env = os.environ.get('WEBATEM_DATA_DIR') or os.environ.get('DATA_DIR')
    return Path(env) if env else default_data_dir()


def _frozen() -> bool:
    return bool(getattr(sys, 'frozen', False))


def mac_app_bundle():
    """The WebATEM.app this frozen process runs from, or None. Only a bundle
    whose Info.plist names our bundle id: never a folder that merely sits
    above the executable."""
    if not (_frozen() and sys.platform == 'darwin'):
        return None
    for parent in Path(sys.executable).resolve().parents:
        if parent.suffix == '.app':
            try:
                with open(parent / 'Contents' / 'Info.plist', 'rb') as f:
                    if plistlib.load(f).get('CFBundleIdentifier') == BUNDLE_ID:
                        return parent
            except (OSError, ValueError):
                return None
            return None
    return None


def windows_uninstaller():
    """The installer's own uninstaller beside the frozen exe, or None."""
    if not (_frozen() and os.name == 'nt'):
        return None
    path = Path(sys.executable).resolve().parent / 'unins000.exe'
    return path if path.is_file() else None


def linux_binary():
    if not (_frozen() and sys.platform.startswith('linux')):
        return None
    path = Path(sys.executable).resolve()
    return path if path.is_file() else None


def _package_command() -> str:
    return 'pipx uninstall webatem' if 'pipx' in sys.prefix.replace('\\', '/').split('/') else 'pip uninstall webatem'


def plan() -> dict:
    """What an uninstall here removes. ``kind`` names the install;
    ``items`` are (label, path) pairs that exist now; ``uninstaller`` is the
    program that does it instead (Windows); ``then`` is what is left for the
    user to run (a package)."""
    from webatem import launcher

    kind, items, uninstaller, then = 'package', [], None, None
    if windows_uninstaller():
        kind, uninstaller = 'windows-installer', str(windows_uninstaller())
    elif mac_app_bundle():
        kind = 'mac-app'
        items.append(('The app', mac_app_bundle()))
    elif linux_binary():
        kind = 'linux-binary'
        items.append(('The program', linux_binary()))
    else:
        then = _package_command()

    data = data_dir()
    if data.exists():
        items.append(('Settings and connection history', data))
    if launcher.autostart_enabled():
        # On Windows it is a registry value, shown by name; run() clears it
        # through set_autostart like every other platform's.
        items.append(('Start at login', r'HKCU\Software\Microsoft\Windows\CurrentVersion\Run\WebATEM'
                      if os.name == 'nt' else launcher._autostart_path()))
    if sys.platform == 'darwin':
        for rel in MAC_BUNDLE_LEFTOVERS:
            path = Path.home() / rel.format(id=BUNDLE_ID)
            if path.exists():
                items.append(('Launcher window data', path))
    return {'kind': kind, 'items': [(label, str(path)) for label, path in items],
            'uninstaller': uninstaller, 'then': then}


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _remove_data(path: Path):
    """The data folder whole when it is WebATEM's own default folder;
    otherwise only WebATEM's entries in it, and the folder if that leaves it
    empty."""
    if path.resolve() == default_data_dir().resolve():
        _remove(path)
        return
    for name in DATA_ENTRIES:
        entry = path / name
        if entry.exists() or entry.is_symlink():
            _remove(entry)
    try:
        path.rmdir()
    except OSError:
        pass                    # something of the operator's is still in it: theirs


def run(the_plan=None) -> list:
    """Remove what ``plan()`` lists (not the Windows case: its uninstaller
    does that). Start at login first, so a failure later cannot leave a
    login item pointing at a half-removed app. Returns [(path, error or
    None)]."""
    from webatem import launcher

    the_plan = the_plan or plan()
    results = []
    try:
        launcher.set_autostart(False)
    except Exception as e:  # noqa: BLE001 — reported, the rest still goes
        results.append(('start at login', str(e)))
    order = sorted(the_plan['items'], key=lambda item: item[0] == 'The app' or item[0] == 'The program')
    for label, raw in order:
        path = Path(raw)
        if not (path.exists() or path.is_symlink()):
            continue
        try:
            if label == 'Settings and connection history':
                _remove_data(path)
            else:
                _remove(path)
            results.append((raw, None))
        except OSError as e:
            results.append((raw, str(e)))
    return results


# ---- the running launcher ---------------------------------------------------

def pid_file() -> Path:
    return data_dir() / 'webatem.pid'


def write_pid() -> None:
    """The launcher records itself, so ``--uninstall`` from a terminal can
    stop a running copy before taking its files."""
    try:
        pid_file().write_text(str(os.getpid()))
    except OSError:
        pass


def clear_pid() -> None:
    try:
        if pid_file().read_text().strip() == str(os.getpid()):
            pid_file().unlink()
    except (OSError, ValueError):
        pass


def _is_webatem(pid: int) -> bool:
    """Is ``pid`` a WebATEM launcher (and not a recycled number)?"""
    try:
        if os.name == 'nt':
            out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'],
                                 capture_output=True, text=True, timeout=10).stdout.lower()
            return 'webatem' in out or 'python' in out
        os.kill(pid, 0)
        out = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True, timeout=10).stdout
        return 'webatem' in out.lower()
    except (OSError, subprocess.SubprocessError):
        return False


def stop_running(timeout: float = 15) -> bool:
    """Stop the WebATEM this data folder belongs to, if one is running.
    True when one was stopped."""
    try:
        pid = int(pid_file().read_text().strip())
    except (OSError, ValueError):
        return False
    if pid == os.getpid() or not _is_webatem(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline and _is_webatem(pid):
        time.sleep(0.25)
    return True


def start_windows_uninstaller(path: str) -> None:
    """Hand over to Windows' uninstaller and let this process quit; the
    uninstaller ends any webatem.exe still running before it removes files
    (webatem.iss, InitializeUninstall). /SILENT: the user already confirmed
    in the window, so no second "Are you sure"; the progress still shows."""
    flags = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    subprocess.Popen([path, '/SILENT'], creationflags=flags, close_fds=True)


# ---- webatem --uninstall ------------------------------------------------------

def cli(argv) -> int:
    """``webatem --uninstall [--yes]``: say what goes, ask (or take
    ``--yes``), stop a running copy, remove it all."""
    the_plan = plan()
    if the_plan['kind'] == 'windows-installer':
        # Windows' own uninstaller asks and does the rest.
        subprocess.Popen([the_plan['uninstaller']])
        return 0
    if not the_plan['items']:
        print('Nothing of WebATEM is left on this machine.')
        if the_plan['then']:
            print(f'The program itself: run  {the_plan["then"]}')
        return 0
    print('Uninstalling WebATEM removes:')
    for label, path in the_plan['items']:
        print(f'  {label}: {path}')
    if '--yes' not in argv:
        if not sys.stdin or not sys.stdin.isatty():
            print('Nothing removed: run it in a terminal to confirm, or add --yes.')
            return 2
        if input('Remove all of this? [y/N] ').strip().lower() not in ('y', 'yes'):
            print('Nothing removed.')
            return 1
    if stop_running():
        print('Stopped the running WebATEM.')
    failed = [(path, err) for path, err in run(the_plan) if err]
    for path, err in failed:
        print(f'Could not remove {path}: {err}')
    print('WebATEM is uninstalled.' if not failed else 'WebATEM is partly uninstalled (see above).')
    if the_plan['then']:
        print(f'The program itself is still installed: run  {the_plan["then"]}')
    if the_plan['kind'] in ('mac-app', 'linux-binary'):
        # This process ran from what was just removed (on macOS the whole
        # bundle, its Python included): an ordinary exit may import again.
        sys.stdout.flush()
        os._exit(1 if failed else 0)
    return 1 if failed else 0
