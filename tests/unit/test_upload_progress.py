"""What a drag-and-drop upload's ring shows comes from the switcher.

The slot's ring used to ease towards 90 % on a timer between the POST and
the end of the upload, whatever the switcher did: a stalled upload looked
like one about to finish, and on failure the page said only that the image
was not changed, which was not always known. The uploader now reports each
real step (connecting, preparing, sending with the percent of the still the
switcher has granted, verifying), the drag-and-drop thread relays them to
the page as ``mediapool.upload_progress``, and a failure says whether the
switcher's slot was changed.
"""
import asyncio
import pathlib
import types

import pytest

import atem_control.uploader as U
from atem_control.control import consumer as C
from atem_control.media_pool import views

from tests.unit.test_slow_link_upload import _SlowLink

ROOT = pathlib.Path(__file__).resolve().parents[2]
SWITCHER = 'switcher.test'           # a name, never an address
GROUP = 'atem_mediapool_switcher_test'


@pytest.fixture
def no_prep(monkeypatch):
    monkeypatch.setattr(U, '_prepare_frame', lambda path, w, h: b'x' * 16)
    monkeypatch.setattr(U, '_validate_image_fast', lambda p: None)
    monkeypatch.setattr(U, 'HASH_SETTLE_TIMEOUT', 0.05)


def _upload(protocol, progress):
    return U._upload_one(protocol, 0, '/tmp/still.png', 16, 9,
                         U._Canceller(None), lambda m: None, True, progress=progress)


# --- the uploader says what really happened ---------------------------------

def test_the_upload_reports_each_step_and_the_switchers_percent(no_prep):
    seen = []
    p = _SlowLink(every=0.01, budgets=4)
    _upload(p, lambda slot, stage, pct: seen.append((slot, stage, pct)))
    assert seen == [(0, 'preparing', None), (0, 'sending', 0.0),
                    (0, 'sending', 25.0), (0, 'sending', 50.0),
                    (0, 'sending', 75.0), (0, 'sending', 100.0),
                    (0, 'verifying', None)]


def test_a_percent_past_100_is_reported_as_100(no_prep):
    # bmdwire counts what it has queued, which can pass 100.
    seen = []
    p = _SlowLink(every=0.01, budgets=1)
    p.upload = lambda **kw: (p._raise('upload-progress', 0, 0, 104.0, 5, 4),
                             p._raise('upload-done', 0, 0))
    _upload(p, lambda slot, stage, pct: seen.append(pct))
    assert max(x for x in seen if x is not None) == 100.0


def test_a_stalled_upload_reports_nothing_more_and_changed_nothing(no_prep, monkeypatch):
    monkeypatch.setattr(U, 'UPLOAD_TIMEOUT', 0.15)
    seen = []
    p = _SlowLink(every=0.02, budgets=10)
    loop = p.loop

    def stalls():
        if p._sent < 3:
            loop()
    p.loop = stalls
    result = _upload(p, lambda slot, stage, pct: seen.append((stage, pct)))
    assert result.success is False and 'nothing for' in result.error
    assert seen[-1] == ('sending', 30.0), 'the ring stays where the switcher left it'
    assert result.transfer_complete is False, 'the slot keeps its old still'


def test_a_failure_after_the_transfer_may_have_changed_the_slot(no_prep):
    # The switcher took the whole still (upload-done), then the slot's hash
    # never matched: what is on the switcher now is not known.
    p = _SlowLink(every=0.01, budgets=2)
    result = _upload(p, None)
    assert result.success is False and 'silent failure' in result.error
    assert result.transfer_complete is True


def test_a_progress_report_that_raises_does_not_fail_the_upload(no_prep):
    def broken(*a):
        raise RuntimeError('page gone')
    p = _SlowLink(every=0.01, budgets=2)
    result = _upload(p, broken)
    assert p.aborts == 0 and 'silent failure' in result.error


def test_each_connect_attempt_is_reported_for_each_slot(monkeypatch):
    class _Protocol:
        def __init__(self, ip=None, aggressive_drain=False):
            self.ip = ip
            self.transport = types.SimpleNamespace(
                close_session=lambda: None, sock=types.SimpleNamespace(close=lambda: None))

        def connect(self):
            pass

    def no_answer(p, **kw):
        raise TimeoutError()

    monkeypatch.setattr(U, 'AtemProtocol', _Protocol)
    monkeypatch.setattr(U, 'wait_ready', no_answer)
    monkeypatch.setattr(U.time, 'sleep', lambda s: None)
    seen = []
    U.execute_upload([(SWITCHER, 2, '/a.png'), (SWITCHER, 5, '/b.png')], skip_tally=True,
                     progress=lambda *a: seen.append(a))
    assert seen == [(SWITCHER, 2, 'connecting', None), (SWITCHER, 5, 'connecting', None)] \
        * U.CONNECT_ATTEMPTS


# --- the drag-and-drop thread tells the page --------------------------------

def test_the_relay_sends_each_step_and_each_whole_percent_once():
    sent = []
    relay = views._ProgressRelay(SWITCHER, lambda ip, m: sent.append((ip, m)))
    for stage, pct in [('connecting', None), ('connecting', None), ('preparing', None),
                       ('sending', 0.0), ('sending', 0.4), ('sending', 1.2), ('sending', 1.9),
                       ('sending', 57.0), ('sending', 57.0), ('verifying', None)]:
        relay(SWITCHER, 4, stage, pct)
    assert [m['payload'] for _, m in sent] == [
        {'slot': 4, 'stage': 'connecting', 'percent': None},
        {'slot': 4, 'stage': 'preparing', 'percent': None},
        {'slot': 4, 'stage': 'sending', 'percent': 0},
        {'slot': 4, 'stage': 'sending', 'percent': 1},
        {'slot': 4, 'stage': 'sending', 'percent': 57},
        {'slot': 4, 'stage': 'verifying', 'percent': None},
    ]
    assert {m['type'] for _, m in sent} == {'mediapool.upload_progress'}
    assert {ip for ip, _ in sent} == {SWITCHER}


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


def test_the_page_hears_the_upload_move_then_end(layer, monkeypatch, tmp_path):
    def upload(items, progress=None, **kw):
        progress(SWITCHER, 3, 'connecting', None)
        progress(SWITCHER, 3, 'sending', 42.7)
        return [U.ItemResult(slot=3, image_path='a.png', success=True, transfer_complete=True)]
    monkeypatch.setattr(U, 'execute_upload', upload)
    views._run_upload(SWITCHER, 3, str(tmp_path / 'a.png'), str(tmp_path))
    assert layer.sent == [
        (GROUP, {'type': 'mediapool.upload_progress',
                 'payload': {'slot': 3, 'stage': 'connecting', 'percent': None}}),
        (GROUP, {'type': 'mediapool.upload_progress',
                 'payload': {'slot': 3, 'stage': 'sending', 'percent': 42}}),
        (GROUP, {'type': 'mediapool.slot_uploaded', 'payload': {'slot': 3}}),
    ]


@pytest.mark.parametrize('transfer_complete, changed', [(False, False), (True, None)])
def test_a_failure_says_whether_the_switcher_was_changed(layer, monkeypatch, tmp_path,
                                                         transfer_complete, changed):
    failed = U.ItemResult(slot=3, image_path='a.png', success=False, error='it broke',
                          transfer_complete=transfer_complete)
    monkeypatch.setattr(U, 'execute_upload', lambda items, **kw: [failed])
    views._run_upload(SWITCHER, 3, str(tmp_path / 'a.png'), str(tmp_path))
    payload = layer.sent[-1][1]['payload']
    assert payload['reason'] == 'it broke' and payload['changed'] is changed


def test_a_crash_says_it_is_not_known_what_changed(layer, monkeypatch, tmp_path):
    def boom(items, **kw):
        raise RuntimeError('socket gone')
    monkeypatch.setattr(U, 'execute_upload', boom)
    views._run_upload(SWITCHER, 3, str(tmp_path / 'a.png'), str(tmp_path))
    payload = layer.sent[-1][1]['payload']
    assert payload['changed'] is None and 'crashed' in payload['reason']


def test_the_consumer_passes_progress_to_the_page():
    sent = []

    async def send(msg):
        sent.append(msg)
    fake = types.SimpleNamespace(_safe_send_json=send)
    event = {'payload': {'slot': 1, 'stage': 'sending', 'percent': 10}}
    asyncio.run(C.ATEMConsumer.mediapool_upload_progress(fake, event))
    assert sent == [{'type': 'media_pool_upload_progress', 'data': event['payload']}]


# --- the page draws only that ------------------------------------------------

def test_the_page_has_no_timer_driven_ring_and_hears_progress():
    for path in ('atem_control/js/media_pool.js', 'atem_control/static/js/atem_control.js'):
        src = (ROOT / path).read_text()
        assert '_RING_TARGET_PCT' not in src, f'{path} still eases the ring on a timer'
        assert '_armUploadSafetyTimer' not in src, f'{path} still clears uploads silently'
    bundle = (ROOT / 'atem_control/static/js/atem_control.js').read_text()
    assert 'media_pool_upload_progress' in bundle and 'noteUploadProgress' in bundle
    assert "the switcher isn't acknowledging" in bundle
