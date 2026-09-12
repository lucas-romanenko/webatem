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
    assert srv.load() == {'host': '0.0.0.0', 'port': 8000, 'source': 'default', 'start_minimized': False}
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
    assert d['host'] == '0.0.0.0' and d['port'] == 8000 and d['source'] == 'default'
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
    host, port = '0.0.0.0', 8000
    last_error = None

    def __init__(self):
        self.restarts = []
        self._auto = False

    def restart(self, host, port):
        self.restarts.append((host, port))

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
    r = client.post('/server/settings/', json.dumps({'host': '0.0.0.0', 'port': 8000}), content_type='application/json')
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
    finally:
        sup.stop()
        sup.join(20)
    assert not _answers(p2)


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
    srv.save('0.0.0.0', 8000, start_minimized=True)
    assert srv.load()['start_minimized'] is True
    srv.save('0.0.0.0', 8001)                      # an address change leaves the flag alone
    assert srv.load() == {'host': '0.0.0.0', 'port': 8001, 'source': 'file', 'start_minimized': True}


def test_post_start_minimized(client, data_dir):
    r = client.post('/server/settings/', json.dumps({'host': '0.0.0.0', 'port': 8000, 'start_minimized': True}), content_type='application/json')
    assert r.status_code == 200 and srv.load()['start_minimized'] is True
    d = client.get('/server/settings/').json()
    assert d['start_minimized'] is True and d['url'].endswith(':8000/atem/') and d['local_url'] == 'http://127.0.0.1:8000/atem/'


def test_launcher_page_renders(client, data_dir):
    r = client.get('/launcher/')
    body = r.content.decode()
    assert r.status_code == 200
    for needle in ('Launch GUI', 'Start minimized', 'Run at login', 'lwHost', 'lwPort', '/server/settings/'):
        assert needle in body, needle
