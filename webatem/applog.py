"""WebATEM's own log: ``webatem.log`` in the data folder, for every install
the launcher runs (macOS, Windows, Linux, pipx).

Until 1.7.6 only Windows wrote one: the launcher opened the file when the
process had no stdout at all, which is a Windows windowed build. A macOS app
started from Finder has stdout too, pointed at /dev/null, so it logged
nowhere, and a whole day of "it will not open", "the VPN is not listed" and
"it cannot reach a switcher" (Lucas, 2026-10-06/07) left no trace to read.

Now stdout and stderr are teed: the console keeps what it always showed
(a terminal, a service manager), and every line also goes, timestamped,
into the file. Django's logging (connections, requests, errors) writes to
stderr, so it lands there too. The file rotates at 2 MB and keeps three old
ones, so it never fills a disk (the Windows log used to grow forever).
A crash of the interpreter itself, which prints no traceback, is written to
``webatem-crash.log`` by faulthandler.

The Docker image is not the launcher: its output stays on stdout, where
``docker compose logs`` reads it.
"""
import logging
import logging.handlers
import platform
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

LOG_NAME = 'webatem.log'
CRASH_NAME = 'webatem-crash.log'
MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 3
# Lines from Django's logging carry their own time ("2026-10-07 14:03:12,345 INFO ...").
_STAMPED = re.compile(r'^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}')


class _Tee:
    """A text stream: writes go to the console as before (when there is
    one) and, a whole line at a time, into the rotating log file."""

    encoding = 'utf-8'
    errors = 'replace'

    def __init__(self, console, handler, lock):
        self._console = console
        self._handler = handler
        self._lock = lock
        self._pending = ''

    def write(self, text):
        if self._console is not None:
            try:
                self._console.write(text)
            except Exception:  # noqa: BLE001 — a closed console must not stop the app
                pass
        with self._lock:
            self._pending += text
            while '\n' in self._pending:
                line, self._pending = self._pending.split('\n', 1)
                self._emit(line)
        return len(text)

    def _emit(self, line):
        if not _STAMPED.match(line):
            line = time.strftime('%Y-%m-%d %H:%M:%S ') + line
        try:
            self._handler.emit(logging.makeLogRecord({'msg': line}))
        except Exception:  # noqa: BLE001
            pass

    def flush(self):
        if self._console is not None:
            try:
                self._console.flush()
            except Exception:  # noqa: BLE001
                pass
        try:
            self._handler.flush()
        except Exception:  # noqa: BLE001
            pass

    def isatty(self):
        try:
            return bool(self._console is not None and self._console.isatty())
        except Exception:  # noqa: BLE001
            return False

    def fileno(self):
        if self._console is None:
            import io
            raise io.UnsupportedOperation('no console')
        return self._console.fileno()

    def writable(self):
        return True


def start(data_dir) -> Path:
    """Tee stdout and stderr into ``webatem.log`` in ``data_dir``; arm the
    crash log; say what is starting. Returns the log's path."""
    data_dir = Path(data_dir)
    path = data_dir / LOG_NAME
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS,
                                                   encoding='utf-8', delay=False)
    handler.setFormatter(logging.Formatter('%(message)s'))
    lock = threading.Lock()
    sys.stdout = _Tee(sys.stdout, handler, lock)
    sys.stderr = _Tee(sys.stderr, handler, lock)
    try:
        import faulthandler
        faulthandler.enable(file=open(data_dir / CRASH_NAME, 'a'), all_threads=True)
    except Exception:  # noqa: BLE001
        pass
    print(f'--- WebATEM {_version()} starting: {platform.platform()}, Python {platform.python_version()}, '
          f'{"frozen" if getattr(sys, "frozen", False) else "package"}, data {data_dir}', flush=True)
    return path


def _version() -> str:
    try:
        from importlib.metadata import version
        return version('webatem')
    except Exception:  # noqa: BLE001
        return 'dev'


def relay(stream, prefix: str) -> None:
    """Copy another process's output into this log, line by line, until it
    ends (the launcher window's stderr, which used to go to /dev/null)."""
    def run():
        try:
            for line in stream:
                print(f'{prefix} {line.rstrip()}', flush=True)
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=run, name='webatem-log-relay', daemon=True).start()


def reveal(path) -> None:
    """Show the log in Finder / Explorer / the file manager (the window's
    Show log button)."""
    path = Path(path)
    if sys.platform == 'darwin':
        subprocess.Popen(['open', '-R', str(path)] if path.exists() else ['open', str(path.parent)])
    elif sys.platform.startswith('win'):
        subprocess.Popen(['explorer', f'/select,{path}'] if path.exists() else ['explorer', str(path.parent)])
    else:
        subprocess.Popen(['xdg-open', str(path.parent)])
