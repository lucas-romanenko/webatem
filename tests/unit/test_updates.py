"""One-click updates (2026-10-06): the launcher checks GitHub on its own
(Lucas: on, can be turned off) and one click installs (Lucas: "One-click
Update"). Never without the published checksum; the data folder is never
touched; checking and installing are the window's bridge and the tray,
never the settings JSON, which answers the network.
"""
import hashlib
import http.server
import json
import plistlib
import sys
import threading
import time
from pathlib import Path

import pytest

from webatem import launcher, uninstall, updates

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / '.local' / 'share'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    monkeypatch.delenv('DATA_DIR', raising=False)
    monkeypatch.delenv('WEBATEM_DATA_DIR', raising=False)
    monkeypatch.setattr(sys, 'frozen', False, raising=False)
    monkeypatch.setattr(updates, 'current_version', lambda: '1.6.0')
    return tmp_path


@pytest.fixture
def release(tmp_path, monkeypatch):
    """A release feed and its download, served on loopback the way GitHub
    serves them. ``release(payload, digest=…)`` publishes one."""
    root = tmp_path / 'www'
    root.mkdir()

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(root), **kw)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_address[1]}'
    monkeypatch.setenv('WEBATEM_UPDATE_FEED', f'{base}/latest.json')

    def publish(payload=b'new build', tag='v1.7.0', digest='right', name=None):
        name = name or updates.asset_name()
        (root / name).write_bytes(payload)
        sha = {'right': 'sha256:' + hashlib.sha256(payload).hexdigest(), 'wrong': 'sha256:' + '0' * 64,
               'none': None}[digest]
        (root / 'latest.json').write_text(json.dumps({
            'tag_name': tag, 'html_url': f'{base}/release',
            'assets': [{'name': name, 'browser_download_url': f'{base}/{name}', 'digest': sha, 'size': len(payload)}]}))
        return base
    yield publish
    server.shutdown()


def test_release_numbers_compare_as_numbers():
    assert updates.is_newer('v1.10.0', '1.9.3') and updates.is_newer('1.7.0', '1.6')
    assert not updates.is_newer('1.6.0', '1.6.0') and not updates.is_newer('v1.5.9', '1.6.0')
    assert not updates.is_newer('nightly', '1.6.0') and not updates.is_newer('1.7.0', 'dev')


def test_each_machine_gets_its_own_download():
    assert updates.asset_name('darwin', 'arm64') == 'webatem-macos-arm64.dmg'
    assert updates.asset_name('darwin', 'x86_64') == 'webatem-macos-intel.dmg'   # an Intel build stays Intel
    assert updates.asset_name('win32', 'AMD64') == 'webatem-windows-x64-setup.exe'
    assert updates.asset_name('linux', 'x86_64') == 'webatem-linux-x64'
    assert updates.asset_name('linux', 'aarch64') is None                         # no build: never a wrong one


def test_a_package_install_is_told_its_own_command(home):
    way = updates.how()
    assert way['by'] == 'package' and way['command'] in ('pipx upgrade webatem', 'pip install --upgrade webatem')


def test_check_reads_the_feed(home, release):
    base = release(tag='v1.7.0')
    found = updates.check()
    assert found['available'] and found['latest'] == '1.7.0' and found['current'] == '1.6.0'
    assert found['asset']['url'] == f'{base}/{updates.asset_name()}' and found['asset']['digest'].startswith('sha256:')
    release(tag='v1.6.0')
    assert updates.check()['available'] is False


def test_a_download_is_installed_only_with_its_published_checksum(home, release):
    release(payload=b'the real build')
    path = updates.download(updates.check()['asset'])
    assert path.read_bytes() == b'the real build' and path.parent == uninstall.data_dir() / 'updates'
    release(payload=b'tampered', digest='wrong')
    with pytest.raises(updates.UpdateError, match='did not match'):
        updates.download(updates.check()['asset'])
    assert not any(p.name.endswith('.part') for p in updates.updates_dir().iterdir())
    release(digest='none')
    with pytest.raises(updates.UpdateError, match='no checksum'):
        updates.download(updates.check()['asset'])


def test_the_linux_binary_is_swapped_and_started_again(home, monkeypatch):
    binary = home / 'bin' / 'webatem'
    binary.parent.mkdir()
    binary.write_bytes(b'old')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(binary))
    monkeypatch.setattr(sys, 'platform', 'linux')
    spawned = []
    monkeypatch.setattr(updates, '_detached', lambda args, log: spawned.append(args))
    new = home / 'download'
    new.write_bytes(b'new')
    updates.apply(new)
    assert binary.read_bytes() == b'new' and binary.stat().st_mode & 0o777 == 0o755
    assert spawned[0][:2] == ['/bin/sh', '-c'] and spawned[0][-1] == str(binary.resolve())
    assert 'kill -0' in spawned[0][2] and '--resume' in spawned[0][2]          # waits for us, comes back serving
    spawned.clear()
    updates.apply(new, relaunch=False)                                          # nothing was running: nothing starts
    assert spawned == []


def test_the_mac_app_runs_its_own_installer_on_the_download(home, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    app = home / 'Applications' / 'WebATEM.app'
    (app / 'Contents' / 'MacOS').mkdir(parents=True)
    with open(app / 'Contents' / 'Info.plist', 'wb') as f:
        plistlib.dump({'CFBundleIdentifier': uninstall.BUNDLE_ID}, f)
    meipass = app / 'Contents' / 'Frameworks'
    (meipass / 'desktop').mkdir(parents=True)
    (meipass / 'desktop' / 'install-mac.sh').write_text('#!/bin/sh\n')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(app / 'Contents' / 'MacOS' / 'webatem'))
    monkeypatch.setattr(sys, '_MEIPASS', str(meipass), raising=False)
    spawned = []
    monkeypatch.setattr(updates, '_detached', lambda args, log: spawned.append(args))
    dmg = home / 'webatem-macos-arm64.dmg'
    dmg.write_bytes(b'dmg')
    updates.apply(dmg)
    script = spawned[0][2]
    assert 'WEBATEM_DMG="$1" WEBATEM_DEST="$2" WEBATEM_NO_OPEN=1 sh "$3"' in script and '--resume' in script
    assert spawned[0][4:] == [str(dmg), str(app.resolve().parent), str(updates.updates_dir() / 'install-mac.sh')]
    assert (updates.updates_dir() / 'install-mac.sh').is_file()      # copied out: the bundle is about to go

    (home / 'Applications').chmod(0o555)
    try:
        with pytest.raises(updates.UpdateError, match='no permission'):
            updates.apply(dmg)
    finally:
        (home / 'Applications').chmod(0o755)


def test_the_new_app_does_not_take_itself_for_a_child_of_the_old(monkeypatch):
    """The first CI run of 1.7.0: the relaunched Linux binary inherited the
    bootloader's variables, looked for the old process's unpacked copy and
    died ("Failed to load Python shared library")."""
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setenv('_PYI_APPLICATION_HOME_DIR', '/tmp/_MEI123')
    monkeypatch.setenv('_PYI_PARENT_PROCESS_LEVEL', '1')
    monkeypatch.setenv('_MEIPASS2', '/tmp/_MEI123')
    monkeypatch.setenv('LD_LIBRARY_PATH', '/tmp/_MEI123:/opt/lib')
    monkeypatch.setenv('LD_LIBRARY_PATH_ORIG', '/opt/lib')
    monkeypatch.setenv('HOST', '127.0.0.1')
    env = updates._fresh_env()
    assert not [k for k in env if k.startswith(('_PYI_', '_MEIPASS'))]
    assert env['LD_LIBRARY_PATH'] == '/opt/lib' and 'LD_LIBRARY_PATH_ORIG' not in env
    assert env['PYINSTALLER_RESET_ENVIRONMENT'] == '1' and env['HOST'] == '127.0.0.1'   # the rest is kept


def test_windows_runs_the_new_installer_over_the_old_one(home):
    args = updates.windows_installer_args('C:/dl/setup.exe', relaunch=True)
    assert args[:4] == ['C:/dl/setup.exe', '/SILENT', '/SUPPRESSMSGBOXES', '/NORESTART']
    assert args[-1] == '/relaunch=1' and any(a.startswith('/LOG=') for a in args)
    assert '/relaunch=1' not in updates.windows_installer_args('C:/dl/setup.exe', relaunch=False)


def test_the_installers_relaunch_and_the_mac_app_carries_its_script():
    iss = (ROOT / 'build/desktop/webatem.iss').read_text()
    assert 'Parameters: "--resume"; Flags: nowait; Check: RelaunchAfterUpdate' in iss
    assert "ExpandConstant('{param:relaunch|0}') = '1'" in iss
    spec = (ROOT / 'build/desktop/webatem.spec').read_text()
    assert "datas += [(rel('build', 'desktop', 'install-mac.sh'), 'desktop')]" in spec


def test_the_updater_checks_installs_and_says_what_failed(home, monkeypatch):
    found = {'latest': '1.7.0', 'current': '1.6.0', 'available': True, 'page': 'p',
             'asset': {'name': 'a', 'url': 'u', 'digest': 'sha256:x', 'size': 1}}
    monkeypatch.setattr(updates, 'check', lambda: found)
    monkeypatch.setattr(updates, 'how', lambda: {'by': 'app'})
    changes = []
    u = updates.Updater(enabled=lambda: True, on_change=lambda: changes.append(u.state))
    u.check()
    assert u.status()['state'] == 'available' and u.status()['latest'] == '1.7.0' and changes == ['checking', 'available']

    def fail(asset, progress=None):
        raise updates.UpdateError('The download did not match its published checksum, so nothing was installed.')
    monkeypatch.setattr(updates, 'download', fail)
    quit_called = []
    u.install(lambda: quit_called.append(True))
    for _ in range(50):
        if u.error:
            break
        time.sleep(0.02)
    assert u.state == 'available' and 'checksum' in u.error and quit_called == []

    applied = []
    monkeypatch.setattr(updates, 'download', lambda asset, progress=None: (progress(5, 10), Path('dl'))[1])
    monkeypatch.setattr(updates, 'apply', lambda path: applied.append(path))
    u.install(lambda: quit_called.append(True))
    for _ in range(100):
        if quit_called:
            break
        time.sleep(0.02)
    assert applied == [Path('dl')] and quit_called == [True] and 'downloading' in changes and 'installing' in changes


def test_offline_is_said_quietly(home, monkeypatch):
    def offline():
        raise OSError('Network is unreachable')
    monkeypatch.setattr(updates, 'check', offline)
    u = updates.Updater(enabled=lambda: True)
    u.check()
    assert u.state == 'error' and 'no answer from GitHub' in u.error


def test_with_the_box_off_it_checks_only_when_asked(home, monkeypatch):
    monkeypatch.setattr(updates, 'FIRST_CHECK_AFTER', 0.05)
    asked = []
    monkeypatch.setattr(updates.Updater, 'check', lambda self: asked.append(True))
    u = updates.Updater(enabled=lambda: False)
    u.start()
    time.sleep(0.3)
    assert asked == []                     # the timer came and went
    u.check_now()
    for _ in range(50):
        if asked:
            break
        time.sleep(0.02)
    assert asked == [True]


def test_last_times_download_goes_and_its_logs_stay(home):
    folder = updates.updates_dir()
    folder.mkdir(parents=True)
    (folder / 'webatem-linux-x64').write_text('x')
    (folder / 'install-mac.sh').write_text('x')
    (folder / 'update.log').write_text('what happened')
    updates.clear_downloads()
    assert sorted(p.name for p in folder.iterdir()) == ['update.log']


def test_cli_says_and_installs(home, monkeypatch, capsys):
    monkeypatch.setattr(updates, 'check', lambda: {'latest': '1.6.0', 'current': '1.6.0', 'available': False,
                                                   'page': 'p', 'asset': None})
    assert updates.cli(['--update']) == 0 and 'up to date' in capsys.readouterr().out
    found = {'latest': '1.7.0', 'current': '1.6.0', 'available': True, 'page': 'p',
             'asset': {'name': 'a', 'url': 'u', 'digest': 'sha256:x', 'size': 1}}
    monkeypatch.setattr(updates, 'check', lambda: found)
    assert updates.cli(['--update']) == 0 and 'pip' in capsys.readouterr().out       # a package: its own command
    monkeypatch.setattr(updates, 'how', lambda: {'by': 'app'})
    monkeypatch.setattr(sys, 'stdin', None)
    assert updates.cli(['--update']) == 2                                         # no terminal, no --yes
    calls = []
    monkeypatch.setattr(updates, 'download', lambda asset: Path('dl'))
    monkeypatch.setattr(uninstall, 'stop_running', lambda: True)
    monkeypatch.setattr(updates, 'apply', lambda path, relaunch: calls.append((path, relaunch)))
    assert updates.cli(['--update', '--yes']) == 0 and calls == [(Path('dl'), True)]
    assert 'starting it again' in capsys.readouterr().out


def test_the_launcher_updates_before_it_makes_anything(home, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['webatem', '--update', '--yes'])
    monkeypatch.setattr(updates, 'cli', lambda argv: 0)
    with pytest.raises(SystemExit) as done:
        launcher.main()
    assert done.value.code == 0 and not uninstall.data_dir().exists()


@pytest.mark.django_db
def test_the_status_is_shown_but_checking_and_installing_are_not_on_http(client, home, monkeypatch):
    from django.urls import get_resolver
    from webatem import server as srv

    class Controller:
        host, port, running, restarting, last_error, startup_note = '0.0.0.0', 8880, True, False, None, None

        def update_status(self):
            return {'state': 'available', 'latest': '1.7.0', 'by': 'app'}
    monkeypatch.setattr(srv.runtime, 'controller', Controller())
    d = client.get('/server/settings/').json()
    assert d['update'] == {'state': 'available', 'latest': '1.7.0', 'by': 'app'} and d['check_updates'] is True
    assert not any(w in str(p.pattern) for p in get_resolver().url_patterns for w in ('update', 'check'))
    monkeypatch.setattr(srv.runtime, 'controller', None)
    assert client.get('/server/settings/').json()['update'] is None      # plain uvicorn: no launcher, no updates


@pytest.mark.django_db
def test_the_window_turns_automatic_checks_off(client, home, monkeypatch):
    from webatem import server as srv
    monkeypatch.setenv('DATA_DIR', str(home / 'data'))
    monkeypatch.setattr(srv.runtime, 'controller', None)
    r = client.post('/server/settings/', json.dumps({'host': '0.0.0.0', 'port': 8880, 'check_updates': False}),
                    content_type='application/json')
    assert r.status_code == 200 and srv.load()['check_updates'] is False
    page = client.get('/launcher/').content.decode()
    assert 'id="lwCheckUpdates"' in page and 'pywebview.api.update()' in page and 'pywebview.api.check_updates()' in page


def test_window_lines_reach_the_updater():
    child = launcher._WindowChild(supervisor=None)
    got = []
    child.on_check = lambda: got.append('check')
    child.on_update = lambda: got.append('update')

    class Proc:
        stdout = iter(['check\n', 'update\n'])
    child._proc = Proc()
    child._listen(child._proc)
    assert got == ['check', 'update']
