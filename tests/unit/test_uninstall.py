"""Uninstalling (2026-10-06): everything WebATEM put on the machine goes.

Lucas: "if a user wants to uninstall that does mean everything should go
away; if we update the app, the data should stay". Windows has had that
through its installer since 0.6.5; a Mac Trash drag left the data folder and
a login item pointing at a deleted app, and Linux and pipx left both too.
``webatem.uninstall`` knows per install what goes, the window asks for it
over its native bridge only, and ``webatem --uninstall`` does it from a
terminal.
"""
import plistlib
import sys
from pathlib import Path

import pytest

from webatem import launcher, uninstall

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A machine of our own: HOME, the XDG folders, no DATA_DIR."""
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / '.local' / 'share'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / '.config'))
    monkeypatch.delenv('DATA_DIR', raising=False)
    monkeypatch.delenv('WEBATEM_DATA_DIR', raising=False)
    monkeypatch.setattr(sys, 'frozen', False, raising=False)
    return tmp_path


def _used(data: Path):
    """A data folder the app has been running in."""
    data.mkdir(parents=True, exist_ok=True)
    for name in ('db.sqlite3', '.secret_key', 'server.json', 'webatem.log'):
        (data / name).write_text('x')
    (data / 'staticfiles').mkdir()
    (data / 'staticfiles' / 'app.css').write_text('x')


def _mac_bundle(at: Path, bundle_id=uninstall.BUNDLE_ID) -> Path:
    app = at / 'WebATEM.app'
    (app / 'Contents' / 'MacOS').mkdir(parents=True)
    (app / 'Contents' / 'MacOS' / 'webatem').write_text('#!')
    with open(app / 'Contents' / 'Info.plist', 'wb') as f:
        plistlib.dump({'CFBundleIdentifier': bundle_id}, f)
    return app


def test_a_package_install_removes_its_data_and_login_entry_and_names_the_rest(home):
    _used(uninstall.data_dir())
    launcher.set_autostart(True)
    plan = uninstall.plan()
    assert plan['kind'] == 'package' and plan['then'] in ('pip uninstall webatem', 'pipx uninstall webatem')
    assert [label for label, _ in plan['items']] == ['Settings and connection history', 'Start at login']
    assert all(err is None for _, err in uninstall.run(plan))
    assert not uninstall.data_dir().exists() and not launcher.autostart_enabled()


def test_the_mac_app_goes_with_everything_a_trash_drag_leaves(home, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    app = _mac_bundle(home / 'Applications')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(app / 'Contents' / 'MacOS' / 'webatem'))
    data = home / 'Library' / 'Application Support' / 'WebATEM'
    _used(data)
    launcher.set_autostart(True)                                   # the LaunchAgent
    webkit = home / 'Library' / 'WebKit' / uninstall.BUNDLE_ID
    webkit.mkdir(parents=True)

    plan = uninstall.plan()
    assert plan['kind'] == 'mac-app' and plan['then'] is None
    assert dict((path, label) for label, path in plan['items']) == {
        str(app.resolve()): 'The app', str(data): 'Settings and connection history',
        str(home / 'Library' / 'LaunchAgents' / 'com.webatem.app.plist'): 'Start at login',
        str(webkit): 'Launcher window data'}
    assert all(err is None for _, err in uninstall.run(plan))
    assert not app.exists() and not data.exists() and not webkit.exists() and not launcher.autostart_enabled()


def test_a_bundle_that_is_not_ours_is_never_taken(home, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    app = _mac_bundle(home / 'Applications', bundle_id='com.example.other')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(app / 'Contents' / 'MacOS' / 'webatem'))
    assert uninstall.mac_app_bundle() is None and uninstall.plan()['kind'] == 'package'


def test_the_linux_binary_goes_last(home, monkeypatch):
    binary = home / 'bin' / 'webatem-linux-x64'
    binary.parent.mkdir()
    binary.write_text('ELF')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(binary))
    _used(uninstall.data_dir())
    plan = uninstall.plan()
    assert plan['kind'] == 'linux-binary' and plan['items'][0] == ('The program', str(binary.resolve()))
    removed = [path for path, err in uninstall.run(plan) if err is None]
    assert removed[-1] == str(binary.resolve())                   # data first: a failure never strands it
    assert not binary.exists() and not uninstall.data_dir().exists()


def test_a_data_dir_set_by_the_operator_loses_only_webatems_files(home, monkeypatch):
    """DATA_DIR is the operator's path and could be anything: only what
    WebATEM writes there goes, never the folder's other contents."""
    shared = home / 'srv'
    _used(shared)
    (shared / 'notes.txt').write_text('mine')
    monkeypatch.setenv('DATA_DIR', str(shared))
    uninstall.run(uninstall.plan())
    assert sorted(p.name for p in shared.iterdir()) == ['notes.txt']
    (shared / 'notes.txt').unlink()
    _used(shared)
    uninstall.run(uninstall.plan())
    assert not shared.exists()                                      # nothing else in it: the folder goes too


def test_windows_hands_over_to_its_own_uninstaller(home, monkeypatch):
    monkeypatch.setattr(uninstall, 'windows_uninstaller', lambda: Path(r'C:\Programs\WebATEM\unins000.exe'))
    started = []
    monkeypatch.setattr(uninstall.subprocess, 'Popen', lambda args, **kw: started.append(args))
    plan = uninstall.plan()
    assert plan['kind'] == 'windows-installer' and plan['uninstaller'].endswith('unins000.exe')
    assert uninstall.cli(['--uninstall']) == 0 and started == [[plan['uninstaller']]]


def test_cli_asks_first_and_refuses_without_a_terminal(home, monkeypatch, capsys):
    _used(uninstall.data_dir())
    monkeypatch.setattr(sys, 'stdin', None)
    assert uninstall.cli(['--uninstall']) == 2 and uninstall.data_dir().exists()
    assert 'Nothing removed' in capsys.readouterr().out
    assert uninstall.cli(['--uninstall', '--yes']) == 0 and not uninstall.data_dir().exists()
    out = capsys.readouterr().out
    assert 'WebATEM is uninstalled.' in out and 'uninstall webatem' in out     # the package is pip's / pipx's


def test_the_launcher_uninstalls_before_it_makes_anything(home, monkeypatch):
    """`webatem --uninstall` must not first create the data folder it is
    about to remove (main makes it, writes a log and a PID into it)."""
    monkeypatch.setattr(sys, 'argv', ['webatem', '--uninstall', '--yes'])
    with pytest.raises(SystemExit) as done:
        launcher.main()
    assert done.value.code == 0 and not uninstall.data_dir().exists()


def test_a_terminal_uninstall_never_stops_itself(home):
    uninstall.data_dir().mkdir(parents=True)
    uninstall.write_pid()
    assert uninstall.stop_running(timeout=1) is False              # the PID is this process
    uninstall.clear_pid()
    assert not uninstall.pid_file().exists()


def test_the_windows_uninstall_line_reaches_the_tray():
    child = launcher._WindowChild(supervisor=None)
    asked = []
    child.on_uninstall = lambda: asked.append(True)

    class Proc:
        stdout = iter(['hidden\n', 'uninstall\n'])
    child._proc = Proc()
    child._listen(child._proc)
    assert asked == [True]


def test_after_the_tray_stops_the_launcher_removes_it_all_and_leaves(home, monkeypatch):
    """The window's Uninstall quits the tray; main then removes everything
    and exits at once (on macOS nothing may import from the gone bundle)."""
    _used(uninstall.data_dir())
    exited = []
    monkeypatch.setattr(launcher.os, '_exit', lambda code: exited.append(code))
    launcher._uninstall_after_quit()
    assert exited == [0] and not uninstall.data_dir().exists()


def test_after_the_tray_stops_windows_starts_its_uninstaller(home, monkeypatch):
    monkeypatch.setattr(uninstall, 'windows_uninstaller', lambda: Path(r'C:\Programs\WebATEM\unins000.exe'))
    started, exited = [], []
    monkeypatch.setattr(uninstall.subprocess, 'Popen', lambda args, **kw: started.append(args))
    monkeypatch.setattr(launcher.os, '_exit', lambda code: exited.append(code))
    _used(uninstall.data_dir())
    launcher._uninstall_after_quit()
    assert started == [[r'C:\Programs\WebATEM\unins000.exe', '/SILENT']]
    assert exited == [] and uninstall.data_dir().exists()            # the uninstaller's job, not ours


@pytest.mark.django_db
def test_uninstall_is_never_offered_over_http(client, home, monkeypatch):
    """The settings and Quit endpoints answer the whole network. Uninstall is
    the window's bridge only: no route, nothing in the settings JSON, the
    button hidden until the native window says it is there."""
    from django.urls import get_resolver
    from webatem import server as srv
    monkeypatch.setattr(srv.runtime, 'controller', None)
    routes = [str(p.pattern) for p in get_resolver().url_patterns]
    assert not any('uninstall' in r for r in routes)
    assert 'uninstall' not in client.get('/server/settings/').content.decode()
    page = client.get('/launcher/').content.decode()
    assert 'id="lwUninstall" class="btn btn-ghost btn-xs" hidden' in page
    assert 'id="lwUninstallPanel" hidden' in page and 'pywebview.api.uninstall()' in page


def test_the_installers_close_a_running_webatem_and_check_whose_bundle_it_is():
    iss = (ROOT / 'build/desktop/webatem.iss').read_text()
    step = iss.split('procedure CurUninstallStepChanged')[1]
    assert 'usUninstall' in step and 'taskkill.exe' in step and '/F /IM {#MyAppExeName}' in step
    assert '/T' not in step.split('Exec(')[1].split(')')[0]            # a tree kill would end the uninstaller too
    mac = (ROOT / 'build/desktop/install-mac.sh').read_text()
    block = mac.split('if [ "${1:-}" = "--uninstall" ]; then')[1].split('\nfi\n')[0]
    assert 'CFBundleIdentifier' in block and '"$bundle_id"' in block
    assert 'Library/LaunchAgents/$bundle_id.plist' in block and 'Application Support/WebATEM' in block
