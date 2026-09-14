"""Unit tests for the shared tally cycle-wait (``content_change.tally``).

``wait_for_safe_cycle`` is the single cold->hot->cold loop both the media-pool
upload path and the HyperDeck path wrap, so it's the one place the gate logic
lives — and the one place a regression would silently break BOTH paths.

These pin all three phases + cancel + timeout with zero hardware and zero real
waiting: a fake clock makes ``sleep()`` advance virtual time and ``time()`` read
it, and ``is_live`` is a function of that virtual time (the source is "live"
only inside given windows). The cycle already takes ``observation`` / ``timeout``
/ ``is_live`` / ``is_cancelled`` / ``log`` as params, so nothing is refactored.
"""
import types

from atem_control import tally


class _Clock:
    """sleep() advances virtual time; time() reads it. No real waiting."""
    def __init__(self):
        self.t = 0.0

    def time(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


class _FakeProto:
    def __init__(self):
        self.loops = 0

    def loop(self):
        self.loops += 1


def _live_during(clock, windows):
    """is_live() True only while the virtual clock is inside one of ``windows``
    (list of ``(start, end)`` half-open intervals)."""
    return lambda: any(s <= clock.time() < e for s, e in windows)


def _run(monkeypatch, windows, **kwargs):
    clock = _Clock()
    # Swap the module-level ``time`` name for our fake — scoped, auto-restored.
    monkeypatch.setattr(
        tally, 'time', types.SimpleNamespace(time=clock.time, sleep=clock.sleep))
    proto, logs = _FakeProto(), []
    ok = tally.wait_for_safe_cycle(
        proto, _live_during(clock, windows),
        log=logs.append, observation=0.2, **kwargs)
    return ok, logs, proto


# --- Phase coverage ---------------------------------------------------------

def test_already_cold_safe_after_observation(monkeypatch):
    # Never live -> phase 2 sees no activity in the observation window -> safe.
    ok, logs, proto = _run(monkeypatch, windows=[])
    assert ok is True
    assert proto.loops > 0                                   # protocol was pumped
    assert any('no activity' in l for l in logs)
    assert not any('currently live' in l for l in logs)      # phase 1 skipped
    assert not any('rotation detected' in l for l in logs)   # phase 3 skipped


def test_currently_live_waits_for_cold_then_safe(monkeypatch):
    # Live at the start -> phase 1 drains -> stable cold window -> safe.
    ok, logs, _ = _run(monkeypatch, windows=[(0.0, 0.1)])
    assert ok is True
    assert any('currently live' in l for l in logs)          # phase 1 fired
    assert any('no activity' in l for l in logs)             # phase 2 found the gap


def test_rotation_waits_for_trailing_cold(monkeypatch):
    # Cold at start, goes live during observation (a rotation), then cold again.
    ok, logs, _ = _run(monkeypatch, windows=[(0.1, 0.25)])
    assert ok is True
    assert any('rotation detected' in l for l in logs)       # phase 3 fired
    assert any('cycle complete' in l for l in logs)


# --- Abort paths ------------------------------------------------------------

def test_cancel_returns_false(monkeypatch):
    # Cancel hook trips immediately -> bail without declaring safe.
    ok, logs, _ = _run(monkeypatch, windows=[(0, 999)], is_cancelled=lambda: True)
    assert ok is False
    assert any('cancelled' in l for l in logs)


def test_timeout_returns_false(monkeypatch):
    # Source never goes cold -> exceed the (tiny) timeout -> bail.
    ok, logs, _ = _run(monkeypatch, windows=[(0, 999)], timeout=0.3)
    assert ok is False
    assert any('exceeded' in l for l in logs)


def test_cancel_check_exception_is_swallowed(monkeypatch):
    # A throwing cancel hook must not crash the cycle (treated as not-cancelled).
    def boom():
        raise RuntimeError('cancel check blew up')
    ok, _, _ = _run(monkeypatch, windows=[], is_cancelled=boom)
    assert ok is True
