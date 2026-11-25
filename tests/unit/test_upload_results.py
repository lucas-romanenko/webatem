"""What an upload says about how it ended, for a host that writes its own log.

A host used to learn why an item failed by reading the text of ``error`` or
the log lines. Each ``ItemResult`` now carries it in fields: ``reason`` (a
stable code), ``tally`` and ``tally_seconds`` (the off-air wait),
``tally_gaps``, ``retries`` and ``finished_at``; ``on_event`` hears the
connection, each item and the macros as they happen. The tally wait itself is
unchanged: ``CycleReport`` only records what the cycle saw.
"""
import types

import pytest

import atem_control.uploader as U
from atem_control import tally

from tests.unit.test_slow_link_upload import _SlowLink
from tests.unit.test_tally_cycle import _Clock, _FakeProto, _live_during

SWITCHER = 'switcher.test'           # a name, never an address


@pytest.fixture
def no_prep(monkeypatch):
    monkeypatch.setattr(U, '_prepare_frame', lambda path, w, h: b'x' * 16)
    monkeypatch.setattr(U, '_validate_image_fast', lambda p: None)
    monkeypatch.setattr(U, 'HASH_SETTLE_TIMEOUT', 0.05)


def _upload(protocol, skip_tally=True, logs=None, cancelled=None):
    return U._upload_one(protocol, 0, '/tmp/still.png', 16, 9,
                         U._Canceller(cancelled), (logs if logs is not None else []).append,
                         skip_tally)


def _tally(monkeypatch, *, safe, seen_live=False, ended='', gaps=(), raises=None):
    """Stand in for the off-air wait: answer ``safe`` and fill the report the
    way the real cycle would have."""
    def wait(protocol, slot, canceller, log_append, report=None, **kw):
        if report is not None:
            report.seen_live, report.ended = seen_live, ended
            report.gaps.extend([list(g) for g in gaps])
        if raises:
            raise raises
        return safe
    monkeypatch.setattr(U, '_wait_for_safe_upload_window', wait)


class _Done(_SlowLink):
    """Takes the still and holds it under its hash at once."""

    def loop(self):
        super().loop()
        if self._sent >= self.budgets:
            self.mixerstate['mediaplayer-file-info'][0] = types.SimpleNamespace(
                hash=__import__('hashlib').md5(b'x' * 16).digest())


# --- the off-air wait --------------------------------------------------------

def test_a_slot_off_air_for_the_whole_watch_is_clear(no_prep, monkeypatch):
    _tally(monkeypatch, safe=True, ended='no-activity')
    r = _upload(_Done(every=0.001, budgets=1), skip_tally=False)
    assert r.success and r.reason is None
    assert r.tally == 'clear' and r.tally_seconds >= 0


def test_a_slot_seen_on_air_was_waited_for(no_prep, monkeypatch):
    _tally(monkeypatch, safe=True, seen_live=True, ended='cycle-complete')
    r = _upload(_Done(every=0.001, budgets=1), skip_tally=False)
    assert r.success and r.tally == 'waited'


def test_no_tally_check_says_so(no_prep):
    r = _upload(_Done(every=0.001, budgets=1), skip_tally=True)
    assert r.success and r.tally == 'skipped'


def test_a_slot_that_stays_on_air_fails_in_words_and_in_the_log(no_prep, monkeypatch):
    # The old log said only "slot 3: aborted (total wait exceeded 300s)"; no
    # FAIL line, so a five-image job read as five passes.
    _tally(monkeypatch, safe=False, seen_live=True, ended='timeout')
    logs = []
    p = _Done(every=0.001, budgets=1)
    r = _upload(p, skip_tally=False, logs=logs)
    assert not r.success and r.reason == 'tally-timeout' and r.tally == 'tally-timeout'
    assert r.transfer_complete is False and p._t0 is None, 'nothing was sent'
    assert logs[-1].startswith('slot 1: FAIL — tally timeout')


def test_no_switcher_state_to_check_with(no_prep, monkeypatch):
    _tally(monkeypatch, safe=False, gaps=[(1.0, None)],
           raises=U.StateNotReady('switcher state not ready after 15s'))
    logs = []
    r = _upload(_Done(every=0.001, budgets=1), skip_tally=False, logs=logs)
    assert r.reason == 'state-not-ready' and r.tally == 'state-not-ready'
    assert r.tally_gaps == [[1.0, None]]
    assert logs[-1] == 'slot 1: FAIL — switcher state not ready after 15s'


def test_a_cancel_during_the_wait(no_prep, monkeypatch):
    _tally(monkeypatch, safe=False, ended='cancelled')
    logs = []
    r = _upload(_Done(every=0.001, budgets=1), skip_tally=False, logs=logs,
                cancelled=lambda: True)
    # cancelled before the wait even starts: the cancel is checked first
    assert r.reason == 'cancelled' and 'FAIL — cancelled' in logs[-1]


# --- every other way an item can end ----------------------------------------

def test_an_unreadable_image(monkeypatch):
    def bad(path):
        raise ValueError('not a readable image: cannot identify image file')
    monkeypatch.setattr(U, '_validate_image_fast', bad)
    r = _upload(_Done(every=0.001, budgets=1))
    assert r.reason == 'image' and 'cannot identify' in r.error


def test_a_frame_that_cannot_be_prepared(monkeypatch):
    monkeypatch.setattr(U, '_validate_image_fast', lambda p: None)

    def boom(*a):
        raise MemoryError('too big')
    monkeypatch.setattr(U, '_prepare_frame', boom)
    p = _Done(every=0.001, budgets=1)
    p.loop = lambda: None
    r = _upload(p)
    assert r.reason == 'prep' and r.tally == 'skipped'


def test_upload_that_raises(no_prep):
    p = _Done(every=0.001, budgets=1)

    def refuses(**kw):
        raise OSError('socket closed')
    p.upload = refuses
    assert _upload(p).reason == 'upload-error'


def test_nothing_from_the_switcher(no_prep, monkeypatch):
    monkeypatch.setattr(U, 'UPLOAD_TIMEOUT', 0.05)
    p = _Done(every=0.001, budgets=1)
    p.loop = lambda: None
    r = _upload(p)
    assert r.reason == 'upload-stalled' and 'media lock' in r.error


def test_too_long_in_all(no_prep, monkeypatch):
    monkeypatch.setattr(U, 'UPLOAD_MAX', 0.05)
    r = _upload(_SlowLink(every=0.01, budgets=1000))
    assert r.reason == 'upload-too-long'


def test_bmdwire_gave_up_and_its_restarts_are_kept(no_prep):
    p = _SlowLink(every=0.01, budgets=1000)
    loop = p.loop

    def stalls():
        loop()
        if p._sent == 2:
            p._raise('upload-retrying', 0, 0, 1, 3)
            p._raise('upload-retrying', 0, 0, 2, 3)
            p._raise('file-transfer-error', types.SimpleNamespace(status='stalled', message='no grant'))
    p.loop = stalls
    r = _upload(p)
    assert r.reason == 'upload-gave-up'
    assert [(a, o) for _, a, o in r.retries] == [(1, 3), (2, 3)]
    assert all(at > 0 for at, _, _ in r.retries)


def test_the_slot_never_shows_the_new_still(no_prep):
    r = _upload(_SlowLink(every=0.01, budgets=2))
    assert r.reason == 'not-confirmed' and r.transfer_complete is True


def test_success_has_no_reason_and_a_finish_time(no_prep):
    r = _upload(_Done(every=0.001, budgets=1))
    assert r.success and r.reason is None and r.finished_at > 0


# --- what the cycle saw ------------------------------------------------------

def _cycle(monkeypatch, windows, **kw):
    clock = _Clock()
    monkeypatch.setattr(tally, 'time', types.SimpleNamespace(time=clock.time, sleep=clock.sleep))
    report = tally.CycleReport()
    ok = tally.wait_for_safe_cycle(_FakeProto(), _live_during(clock, windows),
                                   observation=0.2, report=report, **kw)
    return ok, report


@pytest.mark.parametrize('windows, ok, seen_live, ended', [
    ([], True, False, 'no-activity'),
    ([(0.0, 0.1)], True, True, 'no-activity'),
    ([(0.1, 0.25)], True, True, 'cycle-complete'),
    ([(0, 999)], False, True, 'timeout'),
])
def test_the_report_says_what_the_cycle_saw(monkeypatch, windows, ok, seen_live, ended):
    got, report = _cycle(monkeypatch, windows, timeout=5)
    assert (got, report.seen_live, report.ended) == (ok, seen_live, ended)


def test_the_report_says_cancelled(monkeypatch):
    _, report = _cycle(monkeypatch, [], is_cancelled=lambda: True)
    assert report.ended == 'cancelled'


# --- events, as they happen --------------------------------------------------

class _Protocol:
    """Connects, carries a 1080p video mode, takes nothing."""

    def __init__(self, ip=None, aggressive_drain=False):
        self.ip = ip
        self.mixerstate = {'video-mode': types.SimpleNamespace(
            get_resolution=lambda: (1920, 1080), get_label=lambda: '1080p50')}
        self.transport = types.SimpleNamespace(
            close_session=lambda: None, sock=types.SimpleNamespace(close=lambda: None))

    def connect(self):
        pass

    def loop(self):
        pass


def test_connect_retries_then_failure_are_events(monkeypatch):
    def no_answer(p, **kw):
        raise TimeoutError()
    monkeypatch.setattr(U, 'AtemProtocol', _Protocol)
    monkeypatch.setattr(U, 'wait_ready', no_answer)
    monkeypatch.setattr(U.time, 'sleep', lambda s: None)
    events = []
    out = U.execute_upload([(SWITCHER, 0, '/a.png'), (SWITCHER, 1, '/b.png')],
                           skip_tally=True, on_event=events.append)
    assert [(e.kind, e.attempt) for e in events] == [
        ('connect-retry', 1), ('connect-retry', 2), ('connect-failed', 3)]
    assert events[-1].reason == 'connect-timeout' and events[-1].of == U.CONNECT_ATTEMPTS
    assert [r.reason for r in out] == ['connect-timeout', 'connect-timeout']


def test_no_free_connection_is_its_own_reason(monkeypatch):
    def full(p, **kw):
        raise U.NoFreeConnection(SWITCHER)
    monkeypatch.setattr(U, 'AtemProtocol', _Protocol)
    monkeypatch.setattr(U, 'wait_ready', full)
    events = []
    out = U.execute_upload([(SWITCHER, 0, '/a.png')], skip_tally=True, on_event=events.append)
    assert events[-1].kind == 'connect-failed' and events[-1].reason == 'no-free-connection'
    assert out[0].reason == 'no-free-connection'


def test_connected_each_item_and_the_skipped_macro(monkeypatch):
    monkeypatch.setattr(U, 'AtemProtocol', _Protocol)
    monkeypatch.setattr(U, 'wait_ready', lambda p, **kw: None)
    monkeypatch.setattr(U, 'close_protocol', lambda p: None)
    monkeypatch.setattr('atem_control.sightings.record_video_mode', lambda *a: None)

    def one(protocol, slot, path, w, h, canceller, log, skip, progress=None):
        return U.ItemResult(slot=slot, image_path=path, success=slot != 1,
                            reason=None if slot != 1 else 'tally-timeout')
    monkeypatch.setattr(U, '_upload_one', one)
    events, logs = [], []
    U.execute_upload([(SWITCHER, 0, '/a.png'), (SWITCHER, 1, '/b.png')], skip_tally=True,
                     macro_xml_path='/m.xml', on_event=events.append, log_append=logs.append)
    assert [e.kind for e in events] == ['connected', 'item', 'item', 'macro-skipped']
    assert (events[0].width, events[0].height, events[0].attempt) == (1920, 1080, 1)
    assert [e.result.slot for e in events[1:3]] == [0, 1]
    assert not any('_run_for_single_ip' in line for line in logs)


def test_items_not_started_after_a_cancel_are_events_too(monkeypatch):
    monkeypatch.setattr(U, 'AtemProtocol', _Protocol)
    monkeypatch.setattr(U, 'wait_ready', lambda p, **kw: None)
    monkeypatch.setattr(U, 'close_protocol', lambda p: None)
    monkeypatch.setattr('atem_control.sightings.record_video_mode', lambda *a: None)
    events = []
    out = U.execute_upload([(SWITCHER, 0, '/a.png')], skip_tally=True,
                           check_cancelled=lambda: True, on_event=events.append)
    assert out[0].reason == 'cancelled'
    assert [e.kind for e in events] == ['item']


def test_an_event_handler_that_raises_does_not_fail_the_upload(monkeypatch):
    def no_answer(p, **kw):
        raise TimeoutError()
    monkeypatch.setattr(U, 'AtemProtocol', _Protocol)
    monkeypatch.setattr(U, 'wait_ready', no_answer)
    monkeypatch.setattr(U.time, 'sleep', lambda s: None)

    def broken(event):
        raise RuntimeError('host gone')
    out = U.execute_upload([(SWITCHER, 0, '/a.png')], skip_tally=True, on_event=broken)
    assert out[0].reason == 'connect-timeout'


def test_the_report_reaches_the_real_cycle(monkeypatch):
    seen = {}

    def cycle(protocol, is_live, **kw):
        seen.update(kw)
        kw['report'].seen_live = True
        return True
    monkeypatch.setattr(U, 'wait_for_safe_cycle', cycle)
    monkeypatch.setattr(U, '_state_ready', lambda p: True)
    p = types.SimpleNamespace(mixerstate={})
    report = tally.CycleReport()
    assert U._wait_for_safe_upload_window(p, 0, U._Canceller(None), lambda m: None,
                                          report=report)
    assert seen['report'] is report and report.seen_live
