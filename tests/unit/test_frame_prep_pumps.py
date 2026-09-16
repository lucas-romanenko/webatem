"""The protocol loop must keep running while a frame is being prepared.

Event dispatch and the switcher's keepalives only happen inside
``protocol.loop()``. The upload busy-wait pumps it; nothing pumped it during
``_prepare_frame``. That was invisible while every image arrived already at
frame size and prep took milliseconds. It became a switcher-killer in 1.3.0,
when the default stopped resizing on the way in: a large source is seconds of
Lanczos with the loop silent, the switcher times the session out mid-upload,
and the abandoned session takes other clients down with it, ATEM Software
Control included.

Measured, not asserted by reading: a 0.6s prep goes from 0 pumps to dozens.
"""
import queue
import time

import pytest

import atem_control.uploader as U


class _FakeTransport:
    def __init__(self):
        self.thread_recv_queue = queue.Queue()


class _FakeProtocol:
    """Counts how often the loop was pumped."""

    def __init__(self):
        self.loops = 0
        self.transport = _FakeTransport()
        self.mixerstate = {}

    def loop(self):
        self.loops += 1
        time.sleep(0.01)

    def on(self, *a, **k):
        return 1

    def off(self, *a, **k):
        pass

    def upload(self, **k):
        # Stop the run here; prep is what this test measures.
        raise RuntimeError('upload not exercised by this test')

    def abort_transfers(self):
        pass


class _NeverCancelled:
    def is_set(self):
        return False


@pytest.fixture
def slow_prep(monkeypatch):
    """Stand in for Lanczos on a large source."""
    def _slow(path, w, h):
        time.sleep(0.6)
        return b'x' * 16
    monkeypatch.setattr(U, '_prepare_frame', _slow)
    monkeypatch.setattr(U, '_validate_image_fast', lambda p: None)
    monkeypatch.setattr(U, '_wait_for_safe_upload_window', lambda *a, **k: True)


def test_loop_is_pumped_while_the_frame_is_prepared(slow_prep):
    proto = _FakeProtocol()
    U._upload_one(proto, 0, '/tmp/whatever.png', 1920, 1080,
                  _NeverCancelled(), lambda m: None, skip_tally=True)

    # Unpumped this is exactly 0. Pumped at ~10ms a turn over 0.6s it is dozens;
    # 5 is a floor that a slow CI box still clears.
    assert proto.loops > 5, (
        f'the loop was pumped {proto.loops} times during a 0.6s frame prep — '
        'an unpumped prep lets the switcher time the session out mid-upload'
    )


def test_a_prep_failure_still_returns_cleanly(monkeypatch):
    """The worker carries the exception back rather than dying silently."""
    def _boom(path, w, h):
        raise ValueError('image is too large to decode safely')
    monkeypatch.setattr(U, '_prepare_frame', _boom)
    monkeypatch.setattr(U, '_validate_image_fast', lambda p: None)
    monkeypatch.setattr(U, '_wait_for_safe_upload_window', lambda *a, **k: True)

    proto = _FakeProtocol()
    logs = []
    r = U._upload_one(proto, 0, '/tmp/whatever.png', 1920, 1080,
                      _NeverCancelled(), logs.append, skip_tally=True)
    assert not r.success
    assert 'frame prep failed' in r.error
    assert 'too large to decode safely' in r.error
