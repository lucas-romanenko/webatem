"""webatem.log for every launcher install (2026-10-07). Lucas: "Do we write
a log at all? We should probably write a log for all builds". Only a Windows
windowed build did (no stdout at all); a Mac app from Finder logged nowhere.
"""
import io
import logging
import sys
import time
from pathlib import Path

import pytest

from webatem import applog, launcher, uninstall, updates

ROOT = Path(__file__).resolve().parents[2]


def _start(tmp_path, monkeypatch):
    """applog.start with a stand-in console; sys.stdout/stderr come back
    afterwards (monkeypatch), and faulthandler is left to pytest. Called in
    the test body: pytest puts its own capture back on sys.stdout between a
    fixture's setup and the test, which would undo the tee."""
    console = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', console)
    monkeypatch.setattr(sys, 'stderr', console)
    armed = []
    import faulthandler
    monkeypatch.setattr(faulthandler, 'enable', lambda **kw: armed.append(kw))
    path = applog.start(tmp_path)
    return path, console, armed


def test_every_line_reaches_the_console_and_the_file(tmp_path, monkeypatch):
    path, console, armed = _start(tmp_path, monkeypatch)
    print('WebATEM listening on 192.168.81.54:8000', flush=True)
    sys.stderr.write('partial ')
    sys.stderr.write('line\n')
    logging.getLogger('atemwire.pool').handlers = []
    sys.stderr.write('2026-10-07 14:03:12,345 INFO atemwire.pool: connected\n')
    sys.stdout.flush()
    text = path.read_text()
    assert 'WebATEM listening on 192.168.81.54:8000' in console.getvalue()           # the console as before
    lines = text.splitlines()
    assert lines[0].split(' ', 2)[2].startswith('--- WebATEM ') and 'data ' in lines[0]   # what started, where
    assert any(l.endswith('WebATEM listening on 192.168.81.54:8000') and l[:4].isdigit() for l in lines)
    assert any(l.endswith(' partial line') for l in lines)                          # whole lines only
    assert '2026-10-07 14:03:12,345 INFO atemwire.pool: connected' in lines           # not stamped twice
    assert armed and armed[0]['file'].name.endswith(applog.CRASH_NAME)              # the crash log is armed


def test_no_console_at_all_still_logs(tmp_path, monkeypatch):
    """A Windows windowed build: stdout and stderr are None."""
    monkeypatch.setattr(sys, 'stdout', None)
    monkeypatch.setattr(sys, 'stderr', None)
    import faulthandler
    monkeypatch.setattr(faulthandler, 'enable', lambda **kw: None)
    path = applog.start(tmp_path)
    print('server started', flush=True)
    assert 'server started' in path.read_text() and not sys.stdout.isatty()


def test_the_log_rotates_instead_of_filling_the_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(applog, 'MAX_BYTES', 2000)
    monkeypatch.setattr(sys, 'stdout', io.StringIO())
    monkeypatch.setattr(sys, 'stderr', io.StringIO())
    import faulthandler
    monkeypatch.setattr(faulthandler, 'enable', lambda **kw: None)
    path = applog.start(tmp_path)
    for i in range(400):
        print(f'line {i} ' + 'x' * 40)
    names = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith('webatem.log'))
    assert names == ['webatem.log', 'webatem.log.1', 'webatem.log.2', 'webatem.log.3']   # three old ones, no more
    assert path.stat().st_size <= 2100


def test_the_window_process_errors_land_in_the_log(tmp_path, monkeypatch):
    path, console, _ = _start(tmp_path, monkeypatch)
    applog.relay(io.StringIO('pywebview: could not load the page\n'), '[window]')
    for _ in range(50):
        if '[window] pywebview: could not load the page' in path.read_text():
            break
        time.sleep(0.02)
    assert '[window] pywebview: could not load the page' in path.read_text()


def test_update_checks_say_what_they_found(monkeypatch, capsys):
    monkeypatch.setattr(updates, 'check', lambda: {'latest': '1.7.6', 'current': '1.7.5', 'available': True,
                                                   'page': 'p', 'asset': None})
    updates.Updater(enabled=lambda: True).check()
    assert 'update check: this is 1.7.5, latest is 1.7.6 (available)' in capsys.readouterr().out

    def offline():
        raise OSError('Network is unreachable')
    monkeypatch.setattr(updates, 'check', offline)
    updates.Updater(enabled=lambda: True).check()
    assert 'update check: could not check: no answer from GitHub' in capsys.readouterr().out


def test_the_window_shows_the_log_and_uninstall_takes_every_copy(client, tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    page = client.get('/launcher/').content.decode()
    assert 'id="lwShowLog" class="btn btn-ghost btn-xs" hidden' in page and 'pywebview.api.show_log()' in page
    for name in ('webatem.log', 'webatem.log.1', 'webatem.log.3', 'webatem-crash.log'):
        assert name in uninstall.DATA_ENTRIES
    src = (ROOT / 'webatem/launcher.py').read_text()
    assert 'applog.start(data_dir)' in src and 'sys.stdout is None or sys.stderr is None' not in src
    assert "stderr=subprocess.PIPE" in src and "applog.relay(self._proc.stderr, '[window]')" in src
