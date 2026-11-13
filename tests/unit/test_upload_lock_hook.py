"""One upload per switcher, across a host's processes, and a goodbye for every
session the uploader opened.

A profile load with images opens the uploader's own socket. Standalone,
WebATEM's per-IP lock in this process serializes it; a host whose uploads
also run elsewhere (a pool of uploader processes) supplies its own lock
through ``Hooks.hold_upload_lock``, and may refuse when the switcher stayed
busy. Separately: a session the uploader's connect loop started must get the
protocol goodbye whatever stops the connect, not only "no room" and a timeout.
"""
import contextlib
import io
import json

import pytest

from atem_control import hooks
from atem_control import uploader as U
from atem_control.ip_upload_lock import UploadBusy
from atem_control.profile import views

from tests.unit.test_clean_close_guarantees import _FakeUploadProtocol

XML = (b'<?xml version="1.0" encoding="UTF-8"?>'
       b'<Profile majorVersion="1" minorVersion="5" product="ATEM Mini Pro">'
       b'<MediaPool><Stills>'
       b'<Still index="2" name="Logo" path="ATEM Media Pool/Logo.png"/>'
       b'</Stills></MediaPool></Profile>')

class BusyHost(hooks.Hooks):
    """A host whose shared lock stayed held: it refuses."""
    def __init__(self):
        self.calls = []

    def hold_upload_lock(self, ip):
        self.calls.append(ip)
        raise UploadBusy('An upload to Room 1 is running; try again in a moment.')


class SharedLockHost(hooks.Hooks):
    """A host that serializes with its own lock and lets the upload run."""
    def __init__(self):
        self.calls = []

    @contextlib.contextmanager
    def hold_upload_lock(self, ip):
        self.calls.append(('held', ip))
        yield True
        self.calls.append(('released', ip))


def _load(client, monkeypatch, uploads):
    class _Atem:
        connected = True

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Result:
        applied, skipped, errors = [], [], []

        def summary(self):
            return 'ok'

    class _Done:
        def __init__(self, slot):
            self.slot, self.success, self.error = slot, True, None

    def fake_execute_upload(items, **kw):
        uploads.extend(items)
        hooks.get().calls.append('upload')
        return [_Done(slot) for _ip, slot, _ in items]

    monkeypatch.setattr(views, 'ATEM', lambda ip: _Atem())
    monkeypatch.setattr(views.Profile, 'apply', lambda self, atem, options: _Result())
    monkeypatch.setattr(views, 'execute_upload', fake_execute_upload)
    picture = io.BytesIO(b'png')
    picture.name = 'Logo.png'
    profile = io.BytesIO(XML)
    profile.name = 'show.xml'
    r = client.post('/atem/profile/load/', {
        'ip': '10.9.9.6', 'sections': json.dumps({}), 'profile': profile, 'image_0': picture})
    assert r.status_code == 200, r.content
    return r.json()['images']


@pytest.fixture
def host(settings):
    def use(cls):
        settings.WEBATEM_HOOKS = f'tests.unit.test_upload_lock_hook.{cls.__name__}'
        hooks.reset()
        return hooks.get()
    yield use
    hooks.reset()


def test_the_default_lock_is_the_in_process_one(settings):
    settings.WEBATEM_HOOKS = None
    hooks.reset()
    with hooks.get().hold_upload_lock('10.9.9.6') as ok:
        assert ok is True
        from atem_control.ip_upload_lock import _lock_for
        assert _lock_for('10.9.9.6').locked()
    assert not _lock_for('10.9.9.6').locked()
    hooks.reset()


def test_a_profile_load_uploads_under_the_host_s_lock(client, monkeypatch, host):
    h = host(SharedLockHost)
    uploads = []
    images = _load(client, monkeypatch, uploads)
    assert images['uploaded'] == 1 and images['upload_errors'] == []
    assert h.calls == [('held', '10.9.9.6'), 'upload', ('released', '10.9.9.6')]


def test_a_host_that_refuses_gets_its_message_shown_and_nothing_is_sent(client, monkeypatch, host):
    h = host(BusyHost)
    uploads = []
    images = _load(client, monkeypatch, uploads)
    assert uploads == [] and h.calls == ['10.9.9.6']
    assert images['uploaded'] == 0
    assert images['upload_errors'] == ['An upload to Room 1 is running; try again in a moment.']


@pytest.mark.parametrize('stop', [SystemExit(143), OSError('network is unreachable'),
                                  RuntimeError('a bug')])
def test_a_connect_stopped_by_anything_still_says_goodbye(monkeypatch, stop):
    """SIGTERM during the handshake (run_uploader turns it into SystemExit),
    a socket error, a bug: the candidate is closed with the goodbye before
    the exception goes on."""
    made = []

    class _P(_FakeUploadProtocol):
        def __init__(self, ip=None, aggressive_drain=False):
            super().__init__(ip, aggressive_drain)
            made.append(self)

    def stopped(p, **kw):
        raise stop

    monkeypatch.setattr(U, 'AtemProtocol', _P)
    monkeypatch.setattr(U, 'wait_ready', stopped)
    with pytest.raises(type(stop)):
        U._run_for_single_ip('10.9.9.7', [(0, '/x.png')], True, U._Canceller(None), lambda m: None)
    assert len(made) == 1 and made[0].transport.goodbyes == 1


def test_a_connect_that_raises_before_the_handshake_is_closed_too(monkeypatch):
    made = []

    class _P(_FakeUploadProtocol):
        def __init__(self, ip=None, aggressive_drain=False):
            super().__init__(ip, aggressive_drain)
            made.append(self)

        def connect(self):
            raise OSError('no route to host')

    monkeypatch.setattr(U, 'AtemProtocol', _P)
    with pytest.raises(OSError):
        U._run_for_single_ip('10.9.9.7', [(0, '/x.png')], True, U._Canceller(None), lambda m: None)
    assert made[0].transport.goodbyes == 1
