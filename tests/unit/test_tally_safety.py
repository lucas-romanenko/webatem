"""Unit tests for the media-pool upload tally guard (``_is_slot_safe``).

The guard refuses to overwrite a slot while a media player showing that slot is
live on PROGRAM output. The source of truth is the ATEM's own program tally
(``tally-source``) — the same on-air state ATEM Software Control shows, already
accounting for the full signal path (M/E chaining, USK/DSK, aux). Only when
tally hasn't arrived yet does it fall back to an ME1 program-path
reconstruction, so it never overwrites blind nor hangs.

And it only answers from a state that is THERE. A session reconnecting has an
empty one, then a dump arriving field by field; both have no media players and
no tally in them, which used to read as "nothing on air". The guard waits,
bounded, for the dump to finish and refuses the upload if it does not.
"""
import time
import types

import pytest

import atem_control.uploader as U
from atem_control.uploader import _is_slot_safe

MP0_FILL = 3010   # media player 1 (index 0): fill 3010 / key 3011
MP0_KEY = 3011
MP1_FILL = 3020   # media player 2 (index 1)
SLOT = 5


def _ns(**kw):
    return types.SimpleNamespace(**kw)


class _Proto:
    """A session whose state dump has finished."""

    def __init__(self, mixerstate):
        self.mixerstate = mixerstate
        self.initialized = True


def _mx(*, players=None, tally=None, program=None, usk=None, dsk=None):
    """Build a fake mixerstate.

    players: {mp_idx: (source_type, slot)}    source_type 1 == still
    tally:   {source_id: (program, preview)}  -> mixerstate['tally-source']
    program/usk/dsk: drive the ME1 reconstruction FALLBACK (only consulted
        when ``tally`` is None).

    Always carries a video mode: with ``initialized`` that is a finished dump.
    """
    mx = {'video-mode': _ns(name='1080p50')}
    if players is not None:
        mx['mediaplayer-selected'] = {
            i: _ns(source_type=st, slot=sl) for i, (st, sl) in players.items()
        }
    if tally is not None:
        mx['tally-source'] = _ns(tally=dict(tally))
    if program is not None:
        mx['program-bus-input'] = {me: _ns(source=s) for me, s in program.items()}
    if usk is not None:
        mx['key-on-air'] = {
            me: {k: _ns(enabled=en) for k, (en, f, ky) in ks.items()}
            for me, ks in usk.items()
        }
        mx['key-properties-base'] = {
            me: {k: _ns(fill_source=f, key_source=ky) for k, (en, f, ky) in ks.items()}
            for me, ks in usk.items()
        }
    if dsk is not None:
        mx['dkey-state'] = {d: _ns(on_air=oa) for d, (oa, f, ky) in dsk.items()}
        mx['dkey-properties-base'] = {
            d: _ns(fill_source=f, key_source=ky) for d, (oa, f, ky) in dsk.items()
        }
    return mx


def _safe(mx, slot=SLOT):
    return _is_slot_safe(_Proto(mx), slot)


# --- authoritative path: the ATEM's tally-source -----------------------------

def test_program_tally_on_slots_player_blocks():
    # MP0 shows SLOT and is program-tallied → live, must block.
    mx = _mx(players={0: (1, SLOT)}, tally={MP0_FILL: (True, False)})
    assert _safe(mx) is False


def test_program_tally_via_key_id_blocks():
    # Self-keyed source: tally reported against the key id (3011).
    mx = _mx(players={0: (1, SLOT)}, tally={MP0_KEY: (True, False)})
    assert _safe(mx) is False


def test_preview_only_is_safe():
    mx = _mx(players={0: (1, SLOT)}, tally={MP0_FILL: (False, True)})
    assert _safe(mx) is True


def test_no_program_tally_is_safe():
    mx = _mx(players={0: (1, SLOT)}, tally={MP0_FILL: (False, False)})
    assert _safe(mx) is True


def test_slots_player_absent_from_tally_is_safe():
    mx = _mx(players={0: (1, SLOT)}, tally={})
    assert _safe(mx) is True


def test_different_player_tallied_does_not_block():
    # MP1 (3020) is program-tallied, but our slot lives in MP0.
    mx = _mx(players={0: (1, SLOT)}, tally={MP1_FILL: (True, False)})
    assert _safe(mx) is True


def test_slot_not_loaded_in_tallied_player_is_safe():
    # MP0 is program-tallied but holds slot 9, not the slot 5 we're uploading.
    mx = _mx(players={0: (1, 9)}, tally={MP0_FILL: (True, False)})
    assert _safe(mx) is True


def test_clip_mode_player_is_safe():
    # source_type 2 (clip) is not a still — overwriting the still slot is fine.
    mx = _mx(players={0: (2, SLOT)}, tally={MP0_FILL: (True, False)})
    assert _safe(mx) is True


def test_no_player_holds_slot_is_safe():
    mx = _mx(players={}, tally={MP0_FILL: (True, False)})
    assert _safe(mx) is True


def test_ready_state_without_media_players_is_safe():
    # A finished dump with nothing that could be showing the slot.
    assert _safe(_mx()) is True


# --- fallback path: tally-source absent → ME1 program-path reconstruction -----

def test_fallback_me1_program_blocks():
    mx = _mx(players={0: (1, SLOT)}, program={0: MP0_FILL})
    assert _safe(mx) is False


def test_fallback_me1_onair_usk_blocks():
    mx = _mx(players={0: (1, SLOT)}, program={0: 1},
             usk={0: {0: (True, MP0_FILL, 0)}})
    assert _safe(mx) is False


def test_fallback_onair_dsk_blocks():
    mx = _mx(players={0: (1, SLOT)}, program={0: 1},
             dsk={0: (True, MP0_FILL, 0)})
    assert _safe(mx) is False


def test_fallback_idle_me2_is_safe():
    # No tally; reconstruction is ME1-only, so an idle ME2 PGM does not block.
    mx = _mx(players={0: (1, SLOT)}, program={0: 1, 1: MP0_FILL})
    assert _safe(mx) is True


def test_fallback_offair_usk_is_safe():
    mx = _mx(players={0: (1, SLOT)}, program={0: 1},
             usk={0: {0: (False, MP0_FILL, 0)}})
    assert _safe(mx) is True


# --- the state has to be there: a session reconnecting has none ---------------

_READY = object()


class _Reconnecting:
    """A session re-handshaking after a drop, as bmdwire leaves it: the state
    emptied and ``initialized`` cleared, then the new dump arriving one piece
    per ``loop()``, with ``initialized`` set only at its end (ConnectionReady).
    """

    def __init__(self, *pieces):
        self.mixerstate = {}
        self.initialized = False
        self._pieces = list(pieces)
        self.loops = 0

    def loop(self):
        self.loops += 1
        if self._pieces:
            piece = self._pieces.pop(0)
            if piece is _READY:
                self.initialized = True
            else:
                self.mixerstate.update(piece)


def _part(mx, *keys):
    return {k: mx[k] for k in keys}


def test_empty_state_waits_for_the_dump_and_answers_from_it():
    # The bug: an empty state has no media players in it, so it read as safe
    # while the slot was on air. The answer has to come from the dump.
    full = _mx(players={0: (1, SLOT)}, tally={MP0_FILL: (True, False)})
    proto = _Reconnecting(
        # Read here, mid-dump, the slot looks off air: its player is known,
        # its tally is not yet, and the fallback has no program bus to read.
        _part(full, 'video-mode', 'mediaplayer-selected'),
        _part(full, 'tally-source'),
        _READY,
    )
    assert _is_slot_safe(proto, SLOT) is False
    assert proto.loops == 3          # pumped through the dump, read at its end


def test_a_dump_that_lands_off_air_is_safe():
    # A reconnect that finishes in time costs a wait, not the upload.
    full = _mx(players={0: (1, SLOT)}, tally={MP0_FILL: (False, True)})
    proto = _Reconnecting(full, _READY)
    assert _is_slot_safe(proto, SLOT) is True


def test_a_state_that_never_comes_back_is_refused_within_the_bound(monkeypatch):
    monkeypatch.setattr(U, 'STATE_READY_TIMEOUT', 0.2)
    proto = _Reconnecting()
    t0 = time.monotonic()
    with pytest.raises(U._StateNotReady, match='not ready'):
        _is_slot_safe(proto, SLOT)
    assert time.monotonic() - t0 < 2.0
    assert proto.loops > 0           # waited by pumping, as the dump needs


def test_upload_is_refused_when_the_state_does_not_come_back(monkeypatch):
    # Refused at once with the reason, not after the 300 s tally timeout as a
    # slot that "stayed live".
    monkeypatch.setattr(U, 'STATE_READY_TIMEOUT', 0.2)
    monkeypatch.setattr(U, '_validate_image_fast', lambda p: None)
    logs = []
    t0 = time.monotonic()
    result = U._upload_one(_Reconnecting(), SLOT, '/nonexistent.png', 16, 9,
                           U._Canceller(None), logs.append, skip_tally=False)
    assert result.success is False
    assert 'not ready' in result.error
    assert time.monotonic() - t0 < 5.0
    assert any('session reconnecting' in line for line in logs)


def test_cancel_during_the_wait_is_reported_as_the_cancel(monkeypatch):
    monkeypatch.setattr(U, 'STATE_READY_TIMEOUT', 30.0)
    monkeypatch.setattr(U, '_validate_image_fast', lambda p: None)
    polls = {'n': 0}

    def cancelled():
        polls['n'] += 1
        return polls['n'] > 3        # past the pre-tally check, inside the wait

    t0 = time.monotonic()
    result = U._upload_one(_Reconnecting(), SLOT, '/nonexistent.png', 16, 9,
                           U._Canceller(cancelled), lambda m: None, skip_tally=False)
    assert result.error == 'cancelled during tally wait'
    assert time.monotonic() - t0 < 5.0   # not the 30 s bound


class _Pooled:
    """A pooled ATEMConnection, as a host passes it: its own worker pumps the
    loop, so there is no ``loop()`` here to call. ``is_connected`` holds
    through the re-handshake; ``is_ready`` and the state come back together
    once the new dump is in."""

    is_connected = True

    def __init__(self, full, ready_after=0.1):
        self._full = full
        self._ready_at = time.monotonic() + ready_after

    @property
    def is_ready(self):
        return time.monotonic() >= self._ready_at

    @property
    def mixerstate(self):
        return self._full if self.is_ready else {}


def test_a_pooled_connection_is_waited_for_not_pumped():
    full = _mx(players={0: (1, SLOT)}, tally={MP0_FILL: (True, False)})
    assert _is_slot_safe(_Pooled(full), SLOT) is False


def test_a_pooled_connection_that_stays_down_is_refused(monkeypatch):
    monkeypatch.setattr(U, 'STATE_READY_TIMEOUT', 0.2)
    with pytest.raises(U._StateNotReady):
        _is_slot_safe(_Pooled(_mx(), ready_after=60), SLOT)
