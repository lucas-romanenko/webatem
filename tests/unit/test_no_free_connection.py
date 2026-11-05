"""A switcher with every slot taken answers "no room" (bmdwire's
NoFreeConnection). The control page and the uploader say so at once,
in words, instead of trying again until a timeout and calling it offline."""
import asyncio
import types

from atemwire.transport import NoFreeConnection

from atem_control import uploader as U
from atem_control.control import consumer as C


class _Transport:
    def __init__(self):
        self.goodbyes = 0
        self.sock = types.SimpleNamespace(close=lambda: None)

    def close_session(self):
        self.goodbyes += 1


class _Protocol:
    made = []

    def __init__(self, ip=None, aggressive_drain=False):
        self.ip = ip
        self.transport = _Transport()
        self.mixerstate = {}
        _Protocol.made.append(self)

    def connect(self):
        pass


def test_the_uploader_fails_at_once_with_the_reason(monkeypatch):
    _Protocol.made = []
    waits = []

    def refused(p, **kw):
        waits.append(kw)
        raise NoFreeConnection(p.ip)

    monkeypatch.setattr(U, 'AtemProtocol', _Protocol)
    monkeypatch.setattr(U, 'wait_ready', refused)
    monkeypatch.setattr(U.time, 'sleep', lambda s: (_ for _ in ()).throw(AssertionError('no retry wait')))
    logs = []
    out = U._run_for_single_ip('10.9.9.7', [(0, '/a.png'), (1, '/b.png')], True,
                               U._Canceller(None), logs.append)
    assert len(_Protocol.made) == 1, 'a "no room" answer is not asked again'
    assert [r.error for r in out] == [U.NO_FREE_CONNECTION_ERROR] * 2
    assert waits[0]['stall_timeout'] == U.CONNECT_STALL_TIMEOUT
    assert waits[0]['hello_timeout'] == U.CONNECT_HELLO_TIMEOUT
    assert any('No free connection' in m for m in logs)


class _Conn:
    def __init__(self, refused):
        self.refused = refused
        self.attempts = 0
        self.connect_refused = False

    def connect(self, ip):
        self.attempts += 1
        self.connect_refused = self.refused
        return False

    def connect_progress(self):
        return {'stage': 'hello', 'packets': 0}


def _consumer(conn):
    c = C.ATEMConsumer.__new__(C.ATEMConsumer)
    c.current_ip = None
    c.connection = conn
    c.sent = []

    async def send_json(payload):
        c.sent.append(payload)
    c.send_json = send_json
    c._safe_send_json = send_json
    c._acquire_pool_ref = lambda ip: None
    return c


def test_the_control_page_names_a_full_switcher_after_one_try():
    conn = _Conn(refused=True)
    c = _consumer(conn)
    asyncio.run(c.handle_connect({'ip_address': '10.9.9.6'}))
    assert conn.attempts == 1
    last = c.sent[-1]
    assert last['connected'] is False and last['no_free_connection'] is True
    assert last['message'] == C.NO_FREE_CONNECTION_MESSAGE


def test_a_silent_switcher_gets_one_try_inside_the_page_timeout():
    conn = _Conn(refused=False)
    c = _consumer(conn)
    asyncio.run(c.handle_connect({'ip_address': '10.9.9.5'}))
    assert conn.attempts == C.CONNECT_ATTEMPTS == 1       # two hellos in 10 s < the page's 15 s
    assert c.sent[-1]['message'].startswith('Failed to connect')


def test_progress_is_sent_while_a_connect_runs(monkeypatch):
    monkeypatch.setattr(C, 'CONNECT_PROGRESS_EVERY', 0.01)

    class _Slow(_Conn):
        def connect(self, ip):
            import time
            time.sleep(0.1)
            return super().connect(ip)

        def connect_progress(self):
            return {'stage': 'state', 'packets': 40}

    c = _consumer(_Slow(refused=True))
    asyncio.run(c.handle_connect({'ip_address': '10.9.9.4'}))
    progress = [m for m in c.sent if m.get('type') == 'connection_progress']
    assert progress and progress[0]['message'] == 'Connecting… receiving switcher state'
