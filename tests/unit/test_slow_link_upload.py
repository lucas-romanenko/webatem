"""A drag-and-drop upload over a slow link (#63: an ATEM Mini Pro over a VPN
with an 80 ms round trip).

The uploader gave every upload 90 s in all, so a still that kept moving over
a slow link was cut off mid-transfer. It now fails after UPLOAD_TIMEOUT with
nothing new from the upload (bmdwire raises upload-progress as the switcher
grants each budget of chunks), up to UPLOAD_MAX.

The page learnt that an upload had ended only from the watcher's new
thumbnail, one more transfer over the same link, and never that it had
failed: the ring held at its 90 % estimate until a two-minute safety timer
cleared it without a word. The upload thread now says either way.
"""
import time

import pytest

import atem_control.uploader as U
from atem_control.media_pool import views


class _SlowLink:
    """Grants a budget every ``every`` seconds; done after ``budgets``."""

    def __init__(self, every, budgets):
        self.transport = type('T', (), {'thread_recv_queue': None})()
        self.mixerstate = {'mediaplayer-file-info': {}}
        self.aborts = 0
        self.handlers = {}
        self.every = every
        self.budgets = budgets
        self._t0 = None
        self._sent = 0

    def on(self, event, cb):
        self.handlers[len(self.handlers) + 1] = (event, cb)
        return len(self.handlers)

    def off(self, event, cb_id):
        self.handlers.pop(cb_id, None)

    def _raise(self, event, *args):
        for name, cb in list(self.handlers.values()):
            if name == event:
                cb(*args)

    def upload(self, **kw):
        self._t0 = time.time()

    def loop(self):
        if self._t0 is None:
            return
        due = int((time.time() - self._t0) / self.every)
        while self._sent < min(due, self.budgets):
            self._sent += 1
            self._raise('upload-progress', 0, 0, 100 * self._sent / self.budgets, self._sent, self.budgets)
        if self._sent >= self.budgets:
            self._raise('upload-done', 0, 0)
            self._t0 = None

    def abort_transfers(self):
        self.aborts += 1


@pytest.fixture
def no_prep(monkeypatch):
    monkeypatch.setattr(U, '_prepare_frame', lambda path, w, h: b'x' * 16)
    monkeypatch.setattr(U, '_validate_image_fast', lambda p: None)
    monkeypatch.setattr(U, 'HASH_SETTLE_TIMEOUT', 0.05)


def test_a_slow_upload_that_keeps_moving_is_waited_for(no_prep, monkeypatch):
    # Nine budgets 0.05 s apart: 0.45 s in all, three times UPLOAD_TIMEOUT.
    monkeypatch.setattr(U, 'UPLOAD_TIMEOUT', 0.15)
    p = _SlowLink(every=0.05, budgets=9)
    result = U._upload_one(p, 0, '/tmp/slow.png', 16, 9,
                           U._Canceller(None), lambda m: None, True)
    assert p.aborts == 0, result.error
    assert result.upload_seconds >= 0.4
    assert p.handlers == {}, 'its handlers are removed'


def test_an_upload_that_stops_moving_fails(no_prep, monkeypatch):
    monkeypatch.setattr(U, 'UPLOAD_TIMEOUT', 0.15)
    p = _SlowLink(every=0.05, budgets=9)
    loop = p.loop

    def stalls():
        if p._sent < 3:
            loop()
    p.loop = stalls
    t = time.time()
    result = U._upload_one(p, 0, '/tmp/slow.png', 16, 9,
                           U._Canceller(None), lambda m: None, True)
    assert result.success is False
    assert 'nothing for' in result.error
    assert p.aborts == 1
    assert time.time() - t < 0.5, 'UPLOAD_TIMEOUT after the last progress'


def test_an_upload_still_ends_at_upload_max(no_prep, monkeypatch):
    monkeypatch.setattr(U, 'UPLOAD_TIMEOUT', 0.15)
    monkeypatch.setattr(U, 'UPLOAD_MAX', 0.3)
    p = _SlowLink(every=0.05, budgets=1000)
    result = U._upload_one(p, 0, '/tmp/slow.png', 16, 9,
                           U._Canceller(None), lambda m: None, True)
    assert result.success is False
    assert 'after' in result.error and p.aborts == 1


class _Layer:
    def __init__(self):
        self.sent = []

    async def group_send(self, group, message):
        self.sent.append((group, message))


@pytest.fixture
def layer(monkeypatch):
    layer = _Layer()
    monkeypatch.setattr('channels.layers.get_channel_layer', lambda: layer)
    monkeypatch.setattr(views.media_pool_service, 'note_upload_finished', lambda ip, slot: None)
    return layer


def test_a_failed_drag_and_drop_upload_is_told_to_the_page(layer, monkeypatch, tmp_path):
    failed = U.ItemResult(slot=3, image_path='a.png', success=False,
                          error='upload timeout: nothing for 90s')
    monkeypatch.setattr(U, 'execute_upload', lambda items, **kw: [failed])
    views._run_upload('10.0.0.5', 3, str(tmp_path / 'a.png'), str(tmp_path))
    assert layer.sent == [('atem_mediapool_10_0_0_5', {
        'type': 'mediapool.upload_failed',
        'payload': {'slot': 3, 'status': 'failed (upload timeout: nothing for 90s)',
                    'reason': 'upload timeout: nothing for 90s', 'changed': False}})]


def test_a_finished_drag_and_drop_upload_is_told_to_the_page(layer, monkeypatch, tmp_path):
    ok = U.ItemResult(slot=3, image_path='a.png', success=True)
    monkeypatch.setattr(U, 'execute_upload', lambda items, **kw: [ok])
    views._run_upload('10.0.0.5', 3, str(tmp_path / 'a.png'), str(tmp_path))
    assert layer.sent == [('atem_mediapool_10_0_0_5', {
        'type': 'mediapool.slot_uploaded', 'payload': {'slot': 3}})]


def test_a_crashed_upload_is_told_to_the_page(layer, monkeypatch, tmp_path):
    def boom(items, **kw):
        raise RuntimeError('socket gone')
    monkeypatch.setattr(U, 'execute_upload', boom)
    views._run_upload('10.0.0.5', 3, str(tmp_path / 'a.png'), str(tmp_path))
    assert layer.sent[0][1]['type'] == 'mediapool.upload_failed'
