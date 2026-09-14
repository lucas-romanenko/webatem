"""Server settings (2026-09-12): where WebATEM listens, and changing it live.

``webatem.server`` resolves the address (HOST/PORT env > server.json in the
data dir > defaults), validates a change against the machine's addresses,
and is the seam the launcher plugs a controller into. ``/server/settings/``
reads and writes it; with a controller the change restarts the server on
the new address, without one it is saved with a note. The supervisor test
runs a real uvicorn on loopback and moves it to another port.
"""
import json
import socket
import threading
import time

import pytest

from webatem import server as srv


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.delenv('HOST', raising=False)
    monkeypatch.delenv('PORT', raising=False)
    monkeypatch.setattr(srv.runtime, 'controller', None)
    return tmp_path


def test_load_precedence_env_over_file_over_defaults(data_dir, monkeypatch):
    assert srv.load() == {'host': '0.0.0.0', 'port': 8880, 'source': 'default', 'start_minimized': False}
    srv.save('127.0.0.1', 9000)
    assert srv.load() == {'host': '127.0.0.1', 'port': 9000, 'source': 'file', 'start_minimized': False}
    monkeypatch.setenv('PORT', '9100')
    assert srv.load() == {'host': '127.0.0.1', 'port': 9100, 'source': 'env', 'start_minimized': False}


def test_validate_rejects_bad_ports_and_unknown_hosts(data_dir):
    with pytest.raises(ValueError):
        srv.validate('0.0.0.0', 'abc')
    with pytest.raises(ValueError):
        srv.validate('0.0.0.0', 70000)
    with pytest.raises(ValueError):
        srv.validate('203.0.113.9', 8000)       # not an address this machine has
    assert srv.validate('0.0.0.0', '8001') == ('0.0.0.0', 8001)
    assert srv.validate('127.0.0.1', 8002) == ('127.0.0.1', 8002)


def test_url_for_keeps_the_browsers_host_on_all_interfaces():
    assert srv.url_for('0.0.0.0', 8001, '192.168.1.5:8000') == 'http://192.168.1.5:8001/atem/'
    assert srv.url_for('192.168.1.7', 8001, '192.168.1.5:8000') == 'http://192.168.1.7:8001/atem/'


def test_get_describes_the_server(client, data_dir):
    d = client.get('/server/settings/').json()
    assert d['host'] == '0.0.0.0' and d['port'] == 8880 and d['source'] == 'default'
    assert d['restart_available'] is False and d['autostart'] == {'available': False, 'enabled': False}
    assert any(e['ip'] == '127.0.0.1' for e in d['interfaces'])


def test_post_without_a_controller_saves_and_says_so(client, data_dir):
    r = client.post('/server/settings/', json.dumps({'host': '127.0.0.1', 'port': 8500}), content_type='application/json')
    assert r.status_code == 200
    d = r.json()
    assert d['saved'] and d['restarting'] is False and 'note' in d
    assert json.loads((data_dir / 'server.json').read_text()) == {'host': '127.0.0.1', 'port': 8500}


def test_post_rejects_a_bad_change(client, data_dir):
    r = client.post('/server/settings/', json.dumps({'host': '0.0.0.0', 'port': 0}), content_type='application/json')
    assert r.status_code == 400 and 'Port' in r.json()['error']


class _FakeController:
    host, port = '0.0.0.0', 8880
    last_error = None

    def __init__(self):
        self.restarts = []
        self._auto = False

    def restart(self, host, port):
        self.restarts.append((host, port))

    quits = 0

    def quit(self):
        self.quits += 1

    def autostart_enabled(self):
        return self._auto

    def set_autostart(self, enabled):
        self._auto = enabled


def test_post_with_a_controller_restarts_and_sets_autostart(client, data_dir):
    ctl = _FakeController()
    srv.runtime.register(ctl)
    r = client.post('/server/settings/', json.dumps({'host': '0.0.0.0', 'port': 8600, 'autostart': True}),
                    content_type='application/json', HTTP_HOST='192.168.1.5:8000')
    d = r.json()
    assert d['restarting'] is True and d['url'] == 'http://192.168.1.5:8600/atem/'
    assert ctl.restarts == [('0.0.0.0', 8600)] and d['autostart'] is True
    # same address again: nothing to restart
    r = client.post('/server/settings/', json.dumps({'host': '0.0.0.0', 'port': 8880}), content_type='application/json')
    assert 'restarting' not in r.json() and ctl.restarts == [('0.0.0.0', 8600)]


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _answers(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.3):
            return True
    except OSError:
        return False


async def _tiny_app(scope, receive, send):
    if scope['type'] == 'lifespan':
        while True:
            msg = await receive()
            if msg['type'] == 'lifespan.startup':
                await send({'type': 'lifespan.startup.complete'})
            elif msg['type'] == 'lifespan.shutdown':
                await send({'type': 'lifespan.shutdown.complete'})
                return
    await send({'type': 'http.response.start', 'status': 200, 'headers': []})
    await send({'type': 'http.response.body', 'body': b'ok'})


def test_supervisor_moves_a_live_server_to_another_port():
    from webatem.launcher import _Supervisor
    p1, p2 = _free_port(), _free_port()
    sup = _Supervisor(_tiny_app, '127.0.0.1', p1)
    sup.start()
    try:
        assert sup.ready.wait(15) and _answers(p1)
        sup.restart('127.0.0.1', p2)
        deadline = time.time() + 20
        while time.time() < deadline and not ((sup.host, sup.port) == ('127.0.0.1', p2) and _answers(p2) and not _answers(p1)):
            time.sleep(0.2)
        assert _answers(p2), 'the server should answer on the new port'
        assert not _answers(p1), 'the old port should be closed'
        assert (sup.host, sup.port) == ('127.0.0.1', p2) and sup.last_error is None
        assert sup.running and not sup.restarting
        assert sup.launcher_port and _answers(sup.launcher_port), "the window's own server rides through the move"
        assert sup.launcher_url == f'http://127.0.0.1:{sup.launcher_port}/launcher/'
    finally:
        sup.stop()
        sup.join(20)
    assert not _answers(p2) and not _answers(sup.launcher_port)


def test_supervisor_falls_back_when_the_new_port_is_taken():
    from webatem.launcher import _Supervisor
    p1, p2 = _free_port(), _free_port()
    blocker = socket.socket(); blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(('127.0.0.1', p2)); blocker.listen(1)
    sup = _Supervisor(_tiny_app, '127.0.0.1', p1)
    sup.start()
    try:
        assert sup.ready.wait(15)
        sup.restart('127.0.0.1', p2)
        deadline = time.time() + 40
        while time.time() < deadline and sup.last_error is None:
            time.sleep(0.3)
        assert sup.last_error and str(p2) in sup.last_error
        assert (sup.host, sup.port) == ('127.0.0.1', p1) and _answers(p1)
    finally:
        blocker.close()
        sup.stop()
        sup.join(20)


def test_start_minimized_is_kept_beside_the_address(data_dir):
    srv.save('0.0.0.0', 8880, start_minimized=True)
    assert srv.load()['start_minimized'] is True
    srv.save('0.0.0.0', 8001)                      # an address change leaves the flag alone
    assert srv.load() == {'host': '0.0.0.0', 'port': 8001, 'source': 'file', 'start_minimized': True}


def test_post_start_minimized(client, data_dir):
    r = client.post('/server/settings/', json.dumps({'host': '0.0.0.0', 'port': 8880, 'start_minimized': True}), content_type='application/json')
    assert r.status_code == 200 and srv.load()['start_minimized'] is True
    d = client.get('/server/settings/').json()
    assert d['start_minimized'] is True and d['url'].endswith(':8880/atem/') and d['local_url'] == 'http://127.0.0.1:8880/atem/'


def test_launcher_page_renders(client, data_dir):
    r = client.get('/launcher/')
    body = r.content.decode()
    assert r.status_code == 200
    for needle in ('Launch GUI', 'Start minimized', 'Run at login', 'lwHost', 'lwPort', '/server/settings/'):
        assert needle in body, needle


def test_quit_endpoint(client, data_dir):
    assert client.post('/server/quit/').status_code == 409          # plain uvicorn: nothing to quit
    ctl = _FakeController()
    srv.runtime.register(ctl)
    assert client.post('/server/quit/').json() == {'ok': True} and ctl.quits == 1
    assert client.get('/server/settings/').json()['quit_available'] is True


def test_supervisor_takes_the_next_free_port_when_the_configured_one_is_taken():
    from webatem.launcher import _Supervisor
    p1 = _free_port()
    blocker = socket.socket(); blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(('127.0.0.1', p1)); blocker.listen(1)
    sup = _Supervisor(_tiny_app, '127.0.0.1', p1)
    sup.start()
    try:
        assert sup.ready.wait(30), 'the supervisor should come up on another port'
        assert sup.port > p1 and _answers(sup.port)
        assert sup.startup_note and str(p1) in sup.startup_note and str(sup.port) in sup.startup_note
    finally:
        blocker.close()
        sup.stop()
        sup.join(20)


def test_window_child_command(monkeypatch):
    from webatem.launcher import _WindowChild
    class S:
        launcher_url = 'http://127.0.0.1:45678/launcher/'
    monkeypatch.setattr('sys.frozen', False, raising=False)
    cmd = _WindowChild(S())._command()
    assert cmd[1:] == ['-m', 'webatem', '--window', 'http://127.0.0.1:45678/launcher/']
    assert _WindowChild(S())._command(hidden=True)[-1] == '--hidden'      # start minimized: the process is warm, unseen
    monkeypatch.setattr('sys.frozen', True, raising=False)
    assert _WindowChild(S())._command()[1:] == ['--window', 'http://127.0.0.1:45678/launcher/']


def test_launcher_never_imports_pywebview_in_the_tray_process():
    """pywebview's macOS backend makes the process a Dock app the moment it is
    imported; the tray process only probes for it."""
    import importlib, sys as _sys
    import webatem.launcher as launcher
    importlib.reload(launcher)
    launcher._window_support()
    assert 'webview' not in _sys.modules
    assert 'import webview' not in ''.join(
        line for line in open(launcher.__file__) if not line.lstrip().startswith(('#', '"'))
    ).split('def _window_process')[0]


def test_supervisor_on_one_interface_also_answers_on_loopback():
    """A VPN address is not always reachable from the machine that owns it:
    the server bound to one interface listens on 127.0.0.1 as well."""
    from webatem.launcher import _Supervisor
    ips = [e['ip'] for e in srv.interfaces() if e['ip'] != '127.0.0.1' and not e['ip'].startswith('169.254.')]
    if not ips:
        pytest.skip('no non-loopback interface here')
    ip, p = ips[0], _free_port()
    sup = _Supervisor(_tiny_app, ip, p)
    sup.start()
    try:
        assert sup.ready.wait(15) and sup.running and (sup.host, sup.port) == (ip, p)
        assert _answers(p), 'loopback should answer too'
        with socket.create_connection((ip, p), timeout=1):
            pass
    finally:
        sup.stop()
        sup.join(20)


def test_supervisor_does_not_start_when_the_saved_interface_is_gone():
    """The VPN was on when the interface was chosen and is off today: no
    server and no silent switch to all interfaces (Lucas, 0.5.1) — the
    window says which address is gone, and a choice starts the server."""
    from webatem.launcher import _Supervisor
    p = _free_port()
    sup = _Supervisor(_tiny_app, '203.0.113.7', p)      # TEST-NET-3: never one of ours
    sup.start()
    try:
        assert sup.decided.wait(15)
        assert sup.running is False and sup.host is None and not sup.ready.is_set() and not _answers(p)
        assert '203.0.113.7' in sup.startup_note and 'Choose a network interface' in sup.startup_note
        sup.restart('127.0.0.1', p)
        assert sup.ready.wait(15), sup.last_error
        assert (sup.host, sup.port) == ('127.0.0.1', p) and _answers(p) and sup.startup_note is None
    finally:
        sup.stop()
        sup.join(20)


def test_the_window_waits_for_a_choice_at_every_launch_the_others_do_not():
    """A saved interface is never applied under the window (0.5.2: a
    reinstall came up on a months-old VPN choice); without a window the
    saved or default address is used, since nobody could choose."""
    from webatem.launcher import _listen_host
    saved = {'host': '192.168.1.44', 'port': 8880, 'source': 'file', 'start_minimized': False}
    fresh = {'host': '0.0.0.0', 'port': 8880, 'source': 'default', 'start_minimized': False}
    assert _listen_host(saved, has_window=True) is None and _listen_host(fresh, has_window=True) is None
    assert _listen_host(saved, has_window=False) == '192.168.1.44' and _listen_host(fresh, has_window=False) == '0.0.0.0'


def test_supervisor_waits_for_a_choice_then_starts_on_it():
    """The first run under the window: nothing listens until an interface is
    chosen (Companion; Lucas: no server by default)."""
    from webatem.launcher import _Supervisor
    p = _free_port()
    sup = _Supervisor(_tiny_app, None, p)
    sup.start()
    try:
        assert sup.decided.wait(15)
        assert sup.running is False and not sup.ready.is_set() and not _answers(p)
        assert 'Choose a network interface' in sup.startup_note
        sup.restart('127.0.0.1', p)
        assert sup.ready.wait(15), sup.last_error
        assert sup.running and _answers(p) and sup.startup_note is None
    finally:
        sup.stop()
        sup.join(20)


def test_supervisor_uses_a_prebound_launcher_socket():
    from webatem.launcher import _Supervisor, _bind
    sock = _bind('127.0.0.1', 0)
    lp = sock.getsockname()[1]
    sup = _Supervisor(_tiny_app, '127.0.0.1', _free_port(), launcher_socket=sock)
    sup.start()
    try:
        assert sup.launcher_ready.wait(15) and sup.launcher_port == lp
        deadline = time.time() + 10
        while time.time() < deadline and not _answers(lp):
            time.sleep(0.1)
        assert _answers(lp)
    finally:
        sup.stop()
        sup.join(20)


def test_describe_reports_running_and_restarting(client, data_dir):
    d = client.get('/server/settings/').json()
    assert d['running'] is True and d['restarting'] is False       # plain uvicorn: serving by definition
    ctl = _FakeController()
    ctl.running, ctl.restarting = False, True
    srv.runtime.register(ctl)
    d = client.get('/server/settings/').json()
    assert d['running'] is False and d['restarting'] is True


def test_launch_gui_opens_the_shown_address_no_fallback():
    """Companion opens what the user chose; so does WebATEM."""
    from webatem.launcher import _gui_url
    assert _gui_url('192.168.1.44', 8000) == 'http://192.168.1.44:8000/atem/'
    assert _gui_url('127.0.0.1', 8880) == 'http://127.0.0.1:8880/atem/'
    assert _gui_url('0.0.0.0', 8880).endswith(':8880/atem/')      # the LAN address, or loopback if unknown


def test_launcher_page_offers_the_interface_placeholder(client, data_dir):
    html = client.get('/launcher/').content.decode()
    assert 'Change network interface' in html and "d.source === 'default'" in html


def test_launcher_page_sets_the_csrf_cookie_its_posts_need(data_dir):
    """0.5.0 removed the connect page's dialog — and with it the only
    ``{% csrf_token %}`` the window's page relied on for its cookie: every
    POST from the window was a 403, shown as a network error."""
    import json as _json
    from django.test import Client
    c = Client(enforce_csrf_checks=True)
    r = c.get('/launcher/')
    assert r.status_code == 200 and 'csrftoken' in r.cookies
    token = r.cookies['csrftoken'].value
    r = c.post('/server/settings/', _json.dumps({'host': '127.0.0.1', 'port': 8881, 'start_minimized': True}),
               content_type='application/json', HTTP_X_CSRFTOKEN=token)
    assert r.status_code == 200 and r.json()['saved'] is True
    assert 'csrftoken' in Client(enforce_csrf_checks=True).get('/server/settings/').cookies


def test_describe_has_no_address_before_a_choice(client, data_dir):
    ctl = _FakeController()
    ctl.host, ctl.running = None, False
    srv.runtime.register(ctl)
    d = client.get('/server/settings/').json()
    assert d['host'] is None and d['url'] is None and d['local_url'] is None and d['running'] is False
