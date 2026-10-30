"""Unit tests for the shared tally cycle-wait (``atem_control.tally``).

``wait_for_safe_cycle`` is the single cold->hot->cold loop both the media-pool
upload path and the HyperDeck path wrap, so it's the one place the gate logic
lives — and the one place a regression would silently break BOTH paths.

These pin all three phases + cancel + timeout, and what a session that drops
inside the cycle does to each, with zero hardware and zero real waiting: a fake
clock makes ``sleep()`` advance virtual time and ``time()`` read it, and
``is_live`` is a function of that virtual time (the source is "live" only
inside given windows).
"""
import types

import pytest

import atem_control.uploader as U
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


# --- A session that drops and comes back inside the cycle --------------------
#
# Since bmdwire 1.4.0 the transport re-handshakes 5 s after the switcher's last
# packet. Until the new state dump is complete the session has no state, so a
# rotation that goes on and off air in that gap is never seen. The cycle used
# to keep its clock running through the gap: the still predicate waits the gap
# out and answers from the new dump, so the window ended "safe" with a rotation
# inside it; a host's predicate that answers "live" when it cannot tell turned
# the gap into a rotation and took the first off-air reading after it as the
# trailing cold. Both said "safe" at a point the cycle exists to avoid.
#
# What "safe" promises: at that moment the source is off air, and either it was
# off air for the whole observation window before it, or it has only just gone
# off air (the trailing cold of a rotation the cycle watched). ``_really_safe``
# checks that against what the source actually did, gap or no gap.

SLOT = 5
MP0_FILL = 3010     # media player 1 (index 0) as a program source
OBSERVATION = tally.TALLY_OBSERVATION_SECONDS


def _ns(**kw):
    return types.SimpleNamespace(**kw)


def _inside(t, windows):
    return any(s <= t < e for s, e in windows)


class _Session:
    """A bare protocol on the virtual clock, as bmdwire leaves one across a
    drop: inside each ``gaps`` window ``initialized`` is False and the state is
    empty; after it the state is a NEW dict from the new dump. Media player 1
    shows SLOT, and its program tally follows ``on_air``."""

    def __init__(self, clock, on_air, gaps):
        self._clock, self._on_air, self._gaps = clock, on_air, gaps
        self._dumps = {}
        self.loops = 0

    def loop(self):
        self.loops += 1

    @property
    def initialized(self):
        return not _inside(self._clock.time(), self._gaps)

    @property
    def mixerstate(self):
        t = self._clock.time()
        if _inside(t, self._gaps):
            return {}
        dump = sum(1 for _s, e in self._gaps if e <= t)
        state = self._dumps.setdefault(dump, {
            'video-mode': _ns(name='1080p50'),
            'mediaplayer-selected': {0: _ns(source_type=1, slot=SLOT)},
        })
        state['tally-source'] = _ns(
            tally={MP0_FILL: (_inside(t, self._on_air), False)})
        return state


def _on_air_in(state):
    tally = getattr(state.get('tally-source'), 'tally', None) or {}
    pv = tally.get(MP0_FILL)
    return bool(pv and pv[0])


def _unknown_is_live(session):
    """A host's HyperDeck predicate as written for 2.3.0 and earlier: "cannot
    tell" (no state) is answered as live."""
    return lambda: (not session.initialized) or _on_air_in(session.mixerstate)


def _plain(session):
    """A predicate that only reads the state: an empty one reads as off air."""
    return lambda: _on_air_in(session.mixerstate)


def _drive(monkeypatch, style, *, on_air, gaps, **kwargs):
    """Run the cycle against a session that follows ``on_air`` and ``gaps``.
    ``style`` 'still' is the real still-upload path (``_is_slot_safe``, which
    waits the gap out and answers from the new dump); the others are the
    predicates above. Returns (result, virtual time at return, log lines)."""
    clock = _Clock()
    monkeypatch.setattr(
        tally, 'time', types.SimpleNamespace(time=clock.time, sleep=clock.sleep))
    session, logs = _Session(clock, on_air, gaps), []
    if style == 'still':
        ok = U._wait_for_safe_upload_window(
            session, SLOT, U._Canceller(None), logs.append, **kwargs)
    else:
        predicate = {'unknown_is_live': _unknown_is_live, 'plain': _plain}[style]
        ok = tally.wait_for_safe_cycle(
            session, predicate(session), log=logs.append, **kwargs)
    return ok, clock.time(), logs


def _really_safe(t, on_air, observation=OBSERVATION):
    if _inside(t, on_air):
        return False
    if any(e <= t < e + 0.5 for _s, e in on_air):
        return True                                  # the trailing cold, just now
    return not any(s < t and e > t - observation for s, e in on_air)


def _restarts(logs):
    return [i for i, line in enumerate(logs) if 'starting the cycle again' in line]


ALL_STYLES = ['still', 'unknown_is_live', 'plain']


@pytest.mark.parametrize('style', ALL_STYLES)
def test_a_rotation_hidden_in_a_gap_while_observing_is_not_called_safe(monkeypatch, style):
    # Observing from 0. The session drops at 30 and is back at 42; the source
    # went on air at 32 and off at 38 inside that, and comes back every 45 s.
    on_air = [(32, 38), (77, 83)]
    ok, t, logs = _drive(monkeypatch, style, on_air=on_air, gaps=[(30, 42)])
    assert ok is True
    assert _really_safe(t, on_air), (t, logs)
    # The window started again when the state came back, saw the next
    # rotation and waited for its trailing cold.
    assert _restarts(logs)
    assert 83 <= t < 84
    assert any('rotation detected' in line for line in logs)


@pytest.mark.parametrize('style', ['unknown_is_live', 'plain'])
def test_the_first_off_air_reading_after_a_long_gap_is_not_a_trailing_cold(monkeypatch, style):
    # A host's deck predicate can wait out a gap of any length (up to the
    # timeout). Here the session is gone from 5 to 50; a rotation went on and
    # off air at 8-14 and the next starts at 53. Reading 50 as "cycle complete"
    # would mutate 3 s before the source goes back on air.
    on_air = [(8, 14), (53, 59)]
    ok, t, logs = _drive(monkeypatch, style, on_air=on_air, gaps=[(5, 50)])
    assert ok is True
    assert _really_safe(t, on_air), (t, logs)
    assert 59 <= t < 60


@pytest.mark.parametrize('style', ALL_STYLES)
def test_a_gap_while_waiting_for_the_trailing_cold_starts_the_cycle_again(monkeypatch, style):
    # The cycle sees the rotation go live at 10. The session drops at 12 and
    # is back at 24; the source went off air at 16, unseen. Off air at 24 is
    # not the trailing cold: how much of the off-air time is left is unknown.
    on_air = [(10, 16), (55, 61)]
    ok, t, logs = _drive(monkeypatch, style, on_air=on_air, gaps=[(12, 24)])
    assert ok is True
    assert _really_safe(t, on_air), (t, logs)
    assert _restarts(logs)
    assert 61 <= t < 62


@pytest.mark.parametrize('style', ALL_STYLES)
def test_a_gap_while_waiting_for_cold_still_ends_in_a_full_window(monkeypatch, style):
    # On air at the start, off at 10; the session is gone from 5 to 20. Nothing
    # was claimed before the gap, so the cycle goes on from the state that came
    # back: off air, then a full window from there.
    on_air = [(0, 10)]
    ok, t, logs = _drive(monkeypatch, style, on_air=on_air, gaps=[(5, 20)])
    assert ok is True
    assert _really_safe(t, on_air), (t, logs)
    assert 20 + OBSERVATION <= t < 21 + OBSERVATION
    assert any('no activity' in line for line in logs)


@pytest.mark.parametrize('style', ALL_STYLES)
def test_on_air_after_a_gap_is_treated_as_on_air_at_the_start(monkeypatch, style):
    # The cold before it was not seen, so a source found on air when the state
    # comes back is not taken as a rotation: wait for cold, then watch a full
    # window, as when the cycle starts with the source live.
    on_air = [(15, 25), (60, 70)]
    ok, t, logs = _drive(monkeypatch, style, on_air=on_air, gaps=[(5, 20)])
    assert ok is True
    assert _really_safe(t, on_air), (t, logs)
    restart = _restarts(logs)[-1]
    assert any('currently live' in line for line in logs[restart:])
    assert 70 <= t < 71


def test_a_session_that_keeps_dropping_times_out_from_the_first_call(monkeypatch):
    # Never on air, but the session drops for 2 s every 30 s, so no full window
    # is ever watched. The cycle gives up at the timeout, counted from the
    # first call: a restart never extends it.
    gaps = [(s, s + 2) for s in range(20, 400, 30)]
    ok, t, logs = _drive(monkeypatch, 'unknown_is_live', on_air=[], gaps=gaps)
    assert ok is False
    assert any('exceeded' in line for line in logs)
    assert t < tally.TALLY_TIMEOUT + 0.2


def test_a_cancel_during_a_gap_ends_the_wait(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(
        tally, 'time', types.SimpleNamespace(time=clock.time, sleep=clock.sleep))
    session = _Session(clock, on_air=[], gaps=[(5, 400)])
    logs = []
    ok = tally.wait_for_safe_cycle(
        session, _unknown_is_live(session), log=logs.append,
        is_cancelled=lambda: clock.time() > 10)
    assert ok is False
    assert any('cancelled' in line for line in logs)
    assert clock.time() < 11


def test_a_drop_inside_one_reading_is_seen_by_the_state_being_replaced(monkeypatch):
    # A predicate that takes a while (as the still check does while it waits
    # for the state) can start on a ready session and return on a ready one,
    # with a whole drop between. The new dump is a new dict, and that is how
    # the cycle knows its reading spans a gap.
    clock = _Clock()
    monkeypatch.setattr(
        tally, 'time', types.SimpleNamespace(time=clock.time, sleep=clock.sleep))
    on_air = [(8, 14), (60, 66)]
    session = _Session(clock, on_air=on_air, gaps=[(6, 16)])
    slow_once = {'done': False}

    def is_live():
        if clock.time() >= 5 and not slow_once['done']:
            slow_once['done'] = True
            clock.sleep(12)                 # 5 -> 17: the drop comes and goes
        return _on_air_in(session.mixerstate)

    logs = []
    ok = tally.wait_for_safe_cycle(session, is_live, log=logs.append)
    assert ok is True
    assert _really_safe(clock.time(), on_air), (clock.time(), logs)
    assert _restarts(logs)
