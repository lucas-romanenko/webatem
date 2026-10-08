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
    print('WebATEM listening on 192.168.1.20:8000', flush=True)
    sys.stderr.write('partial ')
    sys.stderr.write('line\n')
    logging.getLogger('atemwire.pool').handlers = []
    sys.stderr.write('2026-10-07 14:03:12,345 INFO atemwire.pool: connected\n')
    sys.stdout.flush()
    text = path.read_text()
    assert 'WebATEM listening on 192.168.1.20:8000' in console.getvalue()           # the console as before
    lines = text.splitlines()
    assert lines[0].split(' ', 2)[2].startswith('--- WebATEM ') and 'data ' in lines[0]   # what started, where
    assert any(l.endswith('WebATEM listening on 192.168.1.20:8000') and l[:4].isdigit() for l in lines)
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


def test_a_client_that_hung_up_is_not_logged_as_a_crash():
    """Lucas's first 1.7.6 log opened with a CancelledError traceback: asyncio
    reporting that a client went away mid-request. Dropped; every other
    asyncio record still logs."""
    f = applog.DropClientGone()

    def record(name, msg):
        return logging.LogRecord(name, logging.ERROR, __file__, 1, msg, None, None)
    assert f.filter(record('asyncio', 'CancelledError exception in shielded future')) is False
    assert f.filter(record('asyncio', 'Task exception was never retrieved')) is True     # a real one
    assert f.filter(record('django.request', 'CancelledError exception in shielded future')) is True


def test_the_settings_wire_the_quieting_in(tmp_path):
    """In a process of its own, as the app runs: pytest manages warning
    filters itself during a test, so the settings' filter is checked where
    it actually applies."""
    import os
    import subprocess
    from django.conf import settings
    assert settings.LOGGING['filters']['client_gone']['()'] == 'webatem.applog.DropClientGone'
    assert 'client_gone' in settings.LOGGING['handlers']['console']['filters']
    probe = ("import os, warnings, django; os.environ['DJANGO_SETTINGS_MODULE'] = 'webatem.settings'; "
             "django.setup(); warnings.warn('StreamingHttpResponse must consume synchronous iterators in "
             "order to serve them asynchronously. Use an asynchronous iterator instead.', Warning); "
             "warnings.warn('some other warning', Warning)")
    env = dict(os.environ, DATA_DIR=str(tmp_path))
    out = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True, env=env, cwd=str(ROOT))
    assert 'StreamingHttpResponse' not in out.stderr          # silenced, by its exact text
    assert 'some other warning' in out.stderr                 # and nothing else


def test_the_window_waits_for_its_page_instead_of_hanging_up():
    src = (ROOT / 'webatem/launcher.py').read_text()
    assert 'urlopen(url, timeout=10)' in src and 'r.read()' in src and 'urlopen(url, timeout=1)' not in src


def test_the_windows_status_refresh_does_not_fill_the_log():
    """Lucas's 1.7.7 log: a "GET /server/settings/" line every 4-5 s from the
    launcher window's refresh. Only that line is dropped."""
    f = applog.DropStatusPolls()

    def access(method, path, status):
        return logging.LogRecord('uvicorn.access', logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                                 ('127.0.0.1:55310', method, path, '1.1', status), None)
    assert f.filter(access('GET', '/server/settings/', 200)) is False
    assert f.filter(access('POST', '/server/settings/', 200)) is True       # a change the user made
    assert f.filter(access('GET', '/server/settings/', 500)) is True        # a failure
    assert f.filter(access('GET', '/atem/', 200)) is True                   # a browser on WebATEM
    assert f.filter(logging.LogRecord('uvicorn.access', logging.INFO, __file__, 1, 'plain', None, None)) is True
